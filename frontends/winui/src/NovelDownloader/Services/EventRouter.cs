using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using NovelDownloader.Models;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Services;

/// <summary>
/// 事件路由: BackendClient 的后台线程行 → BackendEventParser → UiThrottle 批量
/// 编组 (DispatcherQueue + ~64ms DispatcherQueueTimer) → VM。
///
/// 线程纪律 (§4.2): 解析可在后台线程做, 但所有 VM/UI 写操作一律经
/// <see cref="DispatcherQueue.TryEnqueue"/> 编组到 UI 线程后再执行 —— WinUI 3 是单线程
/// 单元, 在后台线程直接改 ObservableCollection 会崩。
///
/// 响应性 (并发洪流): 所有消息 (hello/ack/busy/error/event/stderr/exit) 按 FIFO
/// 进 <see cref="UiThrottle"/> 缓冲, UI 线程每个 ~64ms 回合批量应用:
///   - 连续 hit 合并为一次 MainViewModel.AddHitsRange (整批一次计数/占位刷新);
///   - 连续 log 合并为一次 AppendLogsRange;
///   - 同一批内多条 sprog/vprog 只应用最后一条 (最新值覆盖, 中间值不逐条刷 UI);
///   - 其余操作按原序逐条执行。
/// 旁路出口 <see cref="EventParsed"/> 仍对每条 event 逐条、按原序触发 (不丢中间事件)。
///
/// 路由: 搜索链路 (log/sprog/hit/sres/dl*) → MainViewModel;
///       校验链路 (vfile/vprog/vfile_done/vdeep/vdone + log 镜像 + sources.* ack) → SourcesViewModel。
/// dispatcher 为 null (单测) 时批量关闭, 行为与逐条同步完全一致。
/// </summary>
public sealed class EventRouter : IDisposable
{
    /// <summary>批量排空周期 (ms)。hit 洪流下 UI 每秒最多重排 ~16 次, 输入不被饿死。</summary>
    public const int FlushIntervalMs = 64;

    private readonly BackendClient _client;
    private readonly DispatcherQueue? _dispatcher;
    private readonly DispatcherQueueTimer? _timer;
    private readonly UiThrottle _throttle;
    private readonly object _sync = new();

    private MainViewModel? _vm;
    private SourcesViewModel? _sourcesVm;
    private AuthViewModel? _authVm;
    private LogSettingsViewModel? _settingsVm;
    private DownloadsViewModel? _downloadsVm;
    private bool _disposed;

    public EventRouter(BackendClient client, DispatcherQueue? dispatcher)
    {
        _client = client;
        _dispatcher = dispatcher;
        _throttle = new UiThrottle(
            batching: dispatcher is not null,
            onCallbackThread: () => dispatcher is null || dispatcher.HasThreadAccess,
            postToCallbackThread: a => dispatcher?.TryEnqueue(new DispatcherQueueHandler(a)),
            sink: ApplyOps);

        if (dispatcher is not null)
        {
            // 构造发生在 UI 线程 (App.OnLaunched), DispatcherQueueTimer 的创建/启动也在那里。
            _timer = dispatcher.CreateTimer();
            _timer.Interval = TimeSpan.FromMilliseconds(FlushIntervalMs);
            _timer.IsRepeating = true;
            _timer.Tick += OnTimerTick;
            _timer.Start();
        }

        WireClient();
    }

    private void OnTimerTick(object? sender, object e)
    {
        TraceLog.Write("Tick");
        _throttle.FlushNow();
    }
    /// <summary>
    /// 测试/特殊接线入口: 自带节流器 (可控排空节奏), 不依赖 DispatcherQueue。
    /// 典型用法: UiThrottle(batching: true, onCallbackThread: () => true, ...) + 手动 FlushNow。
    /// </summary>
    public EventRouter(BackendClient client, UiThrottle throttle)
    {
        _client = client;
        _dispatcher = null;
        _throttle = throttle;
        WireClient();
    }

    private void WireClient()
    {
        _client.LineReceived += OnLineReceived;
        _client.StderrLineReceived += OnStderrLine;
        _client.DiagnosticMessage += OnDiagnostic;
        _client.ProcessExited += OnProcessExited;
    }

    /// <summary>核心事件出口 (任意 kind), 已在 UI 线程上触发。逐条、按原序。</summary>
    public event Action<BackendEvent>? EventParsed;

    /// <summary>hello 到达 (已在 UI 线程上触发)。</summary>
    public event Action<BackendHello>? HelloReceived;

    /// <summary>状态/诊断文本变化 (已在 UI 线程上触发)。</summary>
    public event Action<string>? StatusChanged;

    /// <summary>协议级错误 (JSON 破损 / 未知 kind), 已在 UI 线程上触发。</summary>
    public event Action<string>? ParseError;

    /// <summary>把搜索页 VM 接上搜索链路 (log/sprog/hit/sres/dl* + hello)。</summary>
    public void Attach(MainViewModel vm)
    {
        lock (_sync)
        {
            _vm = vm;
        }
    }

    /// <summary>把书源页 VM 接上校验链路 (v* + log 镜像 + sources.* ack)。</summary>
    public void Attach(SourcesViewModel vm)
    {
        lock (_sync)
        {
            _sourcesVm = vm;
        }
    }

    /// <summary>把登录头页 VM 接上 auth 链路 (auth.* ack + log 镜像 + 错误/忙拒)。</summary>
    public void Attach(AuthViewModel vm)
    {
        lock (_sync)
        {
            _authVm = vm;
        }
    }

    /// <summary>把设置页 VM 接上 data.* / cache.clear / data.clear ack 链路。</summary>
    public void Attach(LogSettingsViewModel vm)
    {
        lock (_sync)
        {
            _settingsVm = vm;
        }
    }

    /// <summary>把下载页 VM 接上 downloads.* ack 链路。</summary>
    public void Attach(DownloadsViewModel vm)
    {
        lock (_sync)
        {
            _downloadsVm = vm;
        }
    }

    /// <summary>
    /// 测试/复用入口: 直接投喂一行后端输出 (无需真实进程)。
    /// dispatcher 为 null 且节流器批量关闭时同步执行, 便于脱离 UI 线程做状态机单测。
    /// </summary>
    public void RouteRawLine(string line) => OnLineReceived(line);

    // ------------------------------------------------------------ 后台线程 --

    private void OnLineReceived(string line)
    {
        var env = BackendEventParser.ParseEnvelope(line, out var error);
        if (error is not null)
        {
            PostOp(new ActionOp(() =>
            {
                ParseError?.Invoke(error);
                Vm()?.AppendLog("✘ " + error);
            }));
            return;
        }

        if (env is null)
        {
            return;
        }

        if (env.Type == "hello")
        {
            var hello = env.ToHello();
            PostOp(new ActionOp(() =>
            {
                HelloReceived?.Invoke(hello);
                Vm()?.ApplyHello(hello);
            }));
            return;
        }

        if (env.Type == "error")
        {
            var msg = env.Message ?? "未知协议错误";
            var errCmd = env.Cmd ?? "";
            PostOp(new ActionOp(() =>
            {
                StatusChanged?.Invoke("后端错误: " + msg);
                Vm()?.AppendLog("✘ [后端] " + msg);
                // auth 命令族错误路由到登录头页 (auth.fetch 失败要复位抓取态)。
                if (errCmd.StartsWith("auth.", StringComparison.Ordinal))
                {
                    AuthVm()?.OnAuthError(errCmd, msg);
                }

                // data.*/清理命令错误: 设置页解锁 (迁移失败/清理失败都要恢复按钮)。
                if (IsSettingsCmd(errCmd))
                {
                    SettingsVm()?.OnBackendError(errCmd, msg);
                }

                // downloads.* 错误: 下载页解锁。
                if (errCmd.StartsWith("downloads.", StringComparison.Ordinal))
                {
                    DownloadsVm()?.OnBackendError(errCmd, msg);
                }
            }));
            return;
        }

        if (env.Type == "busy")
        {
            var running = env.Running ?? "";
            var busyCmd = env.Cmd ?? "";
            var msg = $"后端忙 (正在跑 {running}), 已拒绝本条命令。";
            PostOp(new ActionOp(() =>
            {
                StatusChanged?.Invoke(msg);
                Vm()?.AbortSearch(msg);
                // 被拒的若是 verify, 书源页同样要解锁, 否则按钮永远停在「停止」。
                if (busyCmd == "verify")
                {
                    SourcesVm()?.AbortVerify(msg);
                }

                // 被拒的若是 auth.fetch, 登录头页复位抓取/批量态。
                if (busyCmd == "auth.fetch")
                {
                    AuthVm()?.OnAuthRejected(msg);
                }

                // 被拒的若是设置页命令族 (重操作在跑), 同样要解锁。
                if (IsSettingsCmd(busyCmd))
                {
                    SettingsVm()?.OnBackendError(busyCmd, msg);
                }

                // 被拒的若是 downloads.*, 下载页解锁。
                if (busyCmd.StartsWith("downloads.", StringComparison.Ordinal))
                {
                    DownloadsVm()?.OnBackendError(busyCmd, msg);
                }
            }));
            return;
        }

        if (env.Type == "ack")
        {
            PostOp(BuildAckOp(env));
            return;
        }

        var ev = BackendEventParser.ParseEventFromEnvelope(env, out var evError);
        if (evError is not null)
        {
            PostOp(new ActionOp(() =>
            {
                ParseError?.Invoke(evError);
                Vm()?.AppendLog("✘ " + evError);
            }));
            return;
        }

        if (ev is not null)
        {
            PostOp(new EventOp(ev));
        }
    }

    /// <summary>ack → 操作。携带数据的 ack 解析后投递对应 VM; 纯回执不再刷日志。</summary>
    private ThrottleOp BuildAckOp(BackendEnvelope env)
    {
        if (env.Cmd == "sources.list")
        {
            // 解析在后台线程做 (纯函数, JSON 读取不碰 UI)。
            var result = BackendEventParser.ParseSourcesListAck(env);
            var groups = env.Groups;
            return new ActionOp(() =>
            {
                var svm = SourcesVm();
                if (result is not null)
                {
                    svm?.ApplySourcesList(result);
                }
                else
                {
                    svm?.FinishListLoad();
                }

                if (groups is { Count: > 0 })
                {
                    Vm()?.ApplyGroups(groups);
                }
            });
        }

        if (env.Cmd == "auth.list")
        {
            var result = BackendEventParser.ParseAuthListAck(env);
            return new ActionOp(() =>
            {
                var avm = AuthVm();
                if (result is not null)
                {
                    avm?.ApplyAuthList(result.Entries);
                }
                else
                {
                    avm?.FinishListLoad();
                }
            });
        }

        if (env.Cmd == "auth.save")
        {
            return new ActionOp(() => AuthVm()?.OnAuthSavedAck(env.Count));
        }

        if (env.Cmd == "auth.fetch")
        {
            var info = BackendEventParser.ParseAuthFetchAck(env);
            return new ActionOp(() =>
            {
                if (info is not null)
                {
                    AuthVm()?.ApplyFetchAck(info);
                }
            });
        }

        if (env.Cmd is "data.getdir" or "config.get")
        {
            var paths = BackendEventParser.ParseDataGetDirAck(env);
            return new ActionOp(() =>
            {
                if (paths is not null)
                {
                    SettingsVm()?.ApplyPaths(paths);
                    Vm()?.ApplyPaths(paths);
                }
            });
        }

        if (env.Cmd == "data.setdir")
        {
            var result = BackendEventParser.ParseDataSetDirAck(env);
            return new ActionOp(() =>
            {
                if (result is not null)
                {
                    SettingsVm()?.OnSetDirAck(result);
                    Vm()?.ApplyPaths(result.Paths);
                }
            });
        }

        if (env.Cmd is "cache.clear" or "data.clear")
        {
            var clearCmd = env.Cmd;
            var result = BackendEventParser.ParseClearAck(env, clearCmd);
            return new ActionOp(() =>
            {
                if (result is not null)
                {
                    SettingsVm()?.OnClearAck(clearCmd, result);
                }
            });
        }

        if (env.Cmd == "downloads.list")
        {
            var result = BackendEventParser.ParseDownloadsListAck(env);
            return new ActionOp(() =>
            {
                var dvm = DownloadsVm();
                if (result is not null)
                {
                    dvm?.ApplyList(result);
                }
                else
                {
                    dvm?.FinishList();
                }
            });
        }

        if (env.Cmd == "downloads.delete")
        {
            var path = ExtraString(env, "path");
            return new ActionOp(() =>
            {
                if (path is not null)
                {
                    DownloadsVm()?.OnDeleteAck(path);
                }
            });
        }

        return NullOp;
    }

    private static readonly ThrottleOp NullOp = new ActionOp(() => { });

    private void OnStderrLine(string line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            return;
        }

        PostOp(new ActionOp(() => Vm()?.AppendLog("[stderr] " + line)));
    }

    private void OnDiagnostic(string message)
    {
        PostOp(new ActionOp(() =>
        {
            StatusChanged?.Invoke(message);
            Vm()?.AppendLog(message);
        }));
    }

    private void OnProcessExited(int code)
    {
        var msg = $"后端进程已退出 (退出码 {code})。";
        PostOp(new ActionOp(() =>
        {
            StatusChanged?.Invoke(msg);
            Vm()?.AppendLog(msg);
            // 进程没了, 搜索/校验态必须解锁, 否则按钮会永远停在「停止」。
            Vm()?.AbortSearch("后端进程已退出");
            SourcesVm()?.AbortVerify("后端进程已退出");
            // 登录头页: 抓取浏览器也没了, 复位抓取/批量态。
            AuthVm()?.AbortAll("后端进程已退出");
            // 下载页: 刷新/删除在途也会被掐, 解锁。
            DownloadsVm()?.AbortAll("后端进程已退出");
        }));
    }

    private void PostOp(ThrottleOp op)
    {
        lock (_sync)
        {
            if (_disposed)
            {
                return;
            }
        }

        _throttle.Post(op);
    }

    // ------------------------------------------------------- 批量应用 (UI 线程) --

    /// <summary>
    /// UiThrottle 的 sink: 在 UI 线程按原序应用一批操作。
    /// 连续 hit/log 合并; 批内 sprog/vprog 仅保留最后一条 (最新值覆盖)。
    /// </summary>
    public void ApplyOps(IReadOnlyList<ThrottleOp> ops)
    {
        if (_disposed)
        {
            return;
        }

        TraceLog.Write($"ApplyOps n={ops.Count}");

        var vm = Vm();
        var svm = SourcesVm();
        var avm = AuthVm();

        // 1) 旁路出口逐条按序回放 (不丢被合流的中间事件)。
        foreach (var op in ops)
        {
            if (op is EventOp e)
            {
                EventParsed?.Invoke(e.Event);
            }
        }

        // 2) 合流锚点: 批内最后一条 sprog / vprog 的下标 (更早的同 kind 跳过, 不逐条刷)。
        int lastSprog = -1;
        int lastVprog = -1;
        for (var i = 0; i < ops.Count; i++)
        {
            if (ops[i] is EventOp { Event: SearchProgressEvent })
            {
                lastSprog = i;
            }
            else if (ops[i] is EventOp { Event: VerifyProgressEvent })
            {
                lastVprog = i;
            }
        }

        // 3) 顺序应用, 连续同种合流。
        var idx = 0;
        while (idx < ops.Count)
        {
            switch (ops[idx])
            {
                case ActionOp action:
                {
                    action.Action();
                    idx++;
                    continue;
                }

                case EventOp { Event: HitEvent }:
                {
                    var items = new List<HitItem>();
                    var j = idx;
                    while (j < ops.Count && ops[j] is EventOp { Event: HitEvent h })
                    {
                        items.Add(HitItem.FromDto(h.Hit));
                        j++;
                    }

                    vm?.AddHitsRange(items);
                    idx = j;
                    continue;
                }

                case EventOp { Event: LogEvent }:
                {
                    var msgs = new List<string>();
                    var j = idx;
                    while (j < ops.Count && ops[j] is EventOp { Event: LogEvent l })
                    {
                        msgs.Add(l.Message);
                        j++;
                    }

                    vm?.AppendLogsRange(msgs);
                    svm?.AppendLogs(msgs);   // 校验日志镜像到书源页进度区
                    avm?.AppendLogs(msgs);   // 全局日志镜像到登录头页日志区
                    idx = j;
                    continue;
                }

                case EventOp { Event: SearchProgressEvent prog }:
                {
                    if (idx == lastSprog && vm is not null)
                    {
                        vm.SearchProgress = prog.Text;
                    }

                    idx++;
                    continue;
                }

                case EventOp op when op.Event is VerifyProgressEvent:
                {
                    if (idx == lastVprog && svm is not null)
                    {
                        svm.ApplyEvent(op.Event);
                    }

                    idx++;
                    continue;
                }

                case EventOp single:
                {
                    var kind = single.Event.Kind;
                    vm?.ApplyEvent(single.Event);   // v* 在 MainViewModel 里 default 忽略
                    if (kind.Length > 0 && kind[0] == 'v')
                    {
                        svm?.ApplyEvent(single.Event);   // vfile/vfile_done/vdeep/vdone
                    }

                    idx++;
                    continue;
                }
            }
        }
    }

    // ------------------------------------------------------------ VM 引用读取 --

    private MainViewModel? Vm()
    {
        lock (_sync)
        {
            return _vm;
        }
    }

    private SourcesViewModel? SourcesVm()
    {
        lock (_sync)
        {
            return _sourcesVm;
        }
    }

    private AuthViewModel? AuthVm()
    {
        lock (_sync)
        {
            return _authVm;
        }
    }

    private LogSettingsViewModel? SettingsVm()
    {
        lock (_sync)
        {
            return _settingsVm;
        }
    }

    private DownloadsViewModel? DownloadsVm()
    {
        lock (_sync)
        {
            return _downloadsVm;
        }
    }

    /// <summary>ack 顶层扩展字段 (JsonExtensionData) 里的字符串; 缺失/非字符串返回 null。</summary>
    private static string? ExtraString(BackendEnvelope env, string key)
    {
        if (env.Extra is not null
            && env.Extra.TryGetValue(key, out var el)
            && el.ValueKind == JsonValueKind.String)
        {
            return el.GetString();
        }

        return null;
    }

    /// <summary>设置页命令族: data.* / cache.clear / data.clear (错误与忙拒都要解锁)。</summary>
    private static bool IsSettingsCmd(string cmd)
        => cmd.StartsWith("data.", StringComparison.Ordinal)
           || cmd is "cache.clear" or "data.clear";

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        try
        {
            if (_timer is not null)
            {
                _timer.Stop();
                _timer.Tick -= OnTimerTick;
            }
        }
        catch
        {
        }

        _throttle.ClearPending();
        _client.LineReceived -= OnLineReceived;
        _client.StderrLineReceived -= OnStderrLine;
        _client.DiagnosticMessage -= OnDiagnostic;
        _client.ProcessExited -= OnProcessExited;
    }
}
