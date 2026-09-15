using System;
using Microsoft.UI.Dispatching;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Services;

/// <summary>
/// 事件路由: BackendClient 的后台线程行 → BackendEventParser → DispatcherQueue → VM。
///
/// 线程纪律 (§4.2): 解析可在后台线程做, 但所有 VM/UI 写操作一律经
/// <see cref="DispatcherQueue.TryEnqueue"/> 编组到 UI 线程后再执行 —— WinUI 3 是单线程
/// 单元, 在后台线程直接改 ObservableCollection 会崩。
///
/// 除搜索链路 (log/sprog/hit/sres) 外, 校验 (v*) 与下载 (dl*) 事件不会静默丢弃:
/// 它们在没有订阅者时原样从 <see cref="EventParsed"/> 出口旁路, 供 M2/M3 接入。
/// </summary>
public sealed class EventRouter : IDisposable
{
    private readonly BackendClient _client;
    private readonly DispatcherQueue? _dispatcher;
    private readonly object _sync = new();

    private MainViewModel? _vm;
    private bool _disposed;

    public EventRouter(BackendClient client, DispatcherQueue? dispatcher)
    {
        _client = client;
        _dispatcher = dispatcher;

        _client.LineReceived += OnLineReceived;
        _client.StderrLineReceived += OnStderrLine;
        _client.DiagnosticMessage += OnDiagnostic;
        _client.ProcessExited += OnProcessExited;
    }

    /// <summary>核心事件出口 (任意 kind), 已在 UI 线程上触发。M1 只有搜索链路消费。</summary>
    public event Action<BackendEvent>? EventParsed;

    /// <summary>hello 到达 (已在 UI 线程上触发)。</summary>
    public event Action<BackendHello>? HelloReceived;

    /// <summary>状态/诊断文本变化 (已在 UI 线程上触发)。</summary>
    public event Action<string>? StatusChanged;

    /// <summary>协议级错误 (JSON 破损 / 未知 kind), 已在 UI 线程上触发。</summary>
    public event Action<string>? ParseError;

    /// <summary>把指定 VM 接上搜索链路 (log/sprog/hit/sres + hello)。</summary>
    public void Attach(MainViewModel vm)
    {
        lock (_sync)
        {
            _vm = vm;
        }
    }

    /// <summary>
    /// 测试/复用入口: 直接投喂一行后端输出 (无需真实进程)。
    /// dispatcher 为 null 时同步执行, 便于脱离 UI 线程做状态机单测。
    /// </summary>
    public void RouteRawLine(string line) => OnLineReceived(line);

    // ------------------------------------------------------------ 后台线程 --

    private void OnLineReceived(string line)
    {
        var env = BackendEventParser.ParseEnvelope(line, out var error);
        if (error is not null)
        {
            PostLine(() => ParseError?.Invoke(error), () => _vm?.AppendLog("✘ " + error));
            return;
        }

        if (env is null)
        {
            return;
        }

        if (env.Type == "hello")
        {
            var hello = env.ToHello();
            PostLine(
                () => HelloReceived?.Invoke(hello),
                () => _vm?.ApplyHello(hello));
            return;
        }

        if (env.Type == "error")
        {
            var msg = env.Message ?? "未知协议错误";
            PostLine(
                () => StatusChanged?.Invoke("后端错误: " + msg),
                () => _vm?.AppendLog("✘ [后端] " + msg));
            return;
        }

        if (env.Type == "busy")
        {
            var running = env.Running ?? "";
            var msg = $"后端忙 (正在跑 {running}), 已拒绝本条命令。";
            PostLine(
                () => StatusChanged?.Invoke(msg),
                () => _vm?.AbortSearch(msg));
            return;
        }

        if (env.Type == "ack")
        {
            // stop / sources.* / auth.* / config.get 的受理确认。M1 只需在日志可见。
            PostLine(null, () => _vm?.AppendLog($"· ack {env.Cmd}"));
            return;
        }

        var ev = BackendEventParser.ParseEventFromEnvelope(env, out var evError);
        if (evError is not null)
        {
            PostLine(() => ParseError?.Invoke(evError), () => _vm?.AppendLog("✘ " + evError));
            return;
        }

        if (ev is not null)
        {
            // 先喂 VM 状态机, 再放事件出口 (旁路订阅方)。
            PostLine(
                () => EventParsed?.Invoke(ev),
                () => _vm?.ApplyEvent(ev));
        }
    }

    private void OnStderrLine(string line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            return;
        }

        PostLine(null, () => _vm?.AppendLog("[stderr] " + line));
    }

    private void OnDiagnostic(string message)
        => PostLine(() => StatusChanged?.Invoke(message), () => _vm?.AppendLog(message));

    private void OnProcessExited(int code)
    {
        var msg = $"后端进程已退出 (退出码 {code})。";
        PostLine(
            () => StatusChanged?.Invoke(msg),
            () =>
            {
                _vm?.AppendLog(msg);
                // 进程没了, 搜索态必须解锁, 否则按钮会永远停在「停止」。
                _vm?.AbortSearch("后端进程已退出");
            });
    }

    // ------------------------------------------------------------ 编组助手 --

    /// <summary>把两段回调编组到 UI 线程; dispatcher 缺失 (单测) 时直接内联执行。</summary>
    private void PostLine(Action? uiAction, Action? vmAction)
    {
        void Run()
        {
            if (_disposed)
            {
                return;
            }

            MainViewModel? vm;
            lock (_sync)
            {
                vm = _vm;
            }

            uiAction?.Invoke();
            if (vm is not null && vmAction is not null)
            {
                vmAction();
            }
        }

        var dq = _dispatcher;
        if (dq is null || dq.HasThreadAccess)
        {
            Run();
        }
        else
        {
            dq.TryEnqueue(Run);
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _client.LineReceived -= OnLineReceived;
        _client.StderrLineReceived -= OnStderrLine;
        _client.DiagnosticMessage -= OnDiagnostic;
        _client.ProcessExited -= OnProcessExited;
    }
}
