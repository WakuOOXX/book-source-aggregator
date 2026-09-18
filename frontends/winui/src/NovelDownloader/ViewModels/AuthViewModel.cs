using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 登录头管理页 VM (M4): auth.list 源列表 + 过滤/多选 + 详情编辑
/// (Cookie / 自定义 header JSON) + 单站两段式抓取 + 批量抓取控制台状态机。
///
/// 与 SourcesViewModel 同规约: 纯逻辑, 不引用任何 WinUI 类型 —— 所有写入由
/// EventRouter 在 UI 线程上调用 (ApplyAuthList / ApplyFetchAck / OnAuthSavedAck 等),
/// 命令发出由页面 code-behind 调 BackendClient (payload 由本 VM 的 Prepare* 系列
/// 方法构造, 便于脱离 UI 线程单测断言参数)。
///
/// 路由 (EventRouter):
///   auth.list ack        → ApplyAuthList
///   auth.save ack        → OnAuthSavedAck
///   auth.fetch ack       → ApplyFetchAck (phase=launched/captured, 单站与批量共用)
///   error / busy(带 auth) → OnAuthError / OnAuthRejected
///   log                   → AppendLogs (批量镜像)
///   auth.remove 无独立命令: 删除 = auth.save 空 cookie/header (server.py 语义)
///
/// 后端能力边界 (M4): server.py 的 auth.fetch 只实现「单站两段式」(launch→grab),
/// 批量三段式 (core.auth_manager.AuthManager) 未接进 server —— 前端批量控制台以
/// 两段式 API 串行编排近似: 手动逐站 = 每站 launch→用户抓取→下一站; 自动 =
/// 每站 launch→自动抓取→下一站 (无 Phase0 秒过 / 并行 K 标签 / 并行登录扫)。
/// 状态机属性 (stage/pprog/lprog/login_stage) 齐全, 供后端补齐后直接挂载。
/// </summary>
public sealed class AuthViewModel : ObservableObject
{
    /// <summary>本页日志上限 (与搜索/书源页一致)。</summary>
    public const int LogCapacity = 500;

    /// <summary>并行标签数范围与默认值 (与 core.config.AUTO_PARA_TABS=10 一致)。</summary>
    public const int ParaTabsMin = 2;
    public const int ParaTabsMax = 20;
    public const int ParaTabsDefault = 10;

    /// <summary>登录扫无操作自动抓取的秒数 (core.config.AUTO_IDLE_SECS=5)。</summary>
    public const int AutoIdleSecs = 5;

    private readonly List<string> _log = new();
    private readonly List<string> _selectedUrls = new();
    private readonly List<string> _hosts = new();
    private readonly Dictionary<string, List<string>> _targets = new();

    // ---- 列表/过滤 ----
    private string _filterText = "";
    private bool _listLoaded;

    // ---- 详情编辑 / 单站抓取 ----
    private string _detailTitle = "未选中 · 在左侧列表点选站点（可多选）";
    private string _editCookie = "";
    private string _editHeaderJson = "";
    private string _fetchStatus = "就绪: 选中单个站点后可「浏览器登录抓取」";
    private string _fetchButtonText = "浏览器登录抓取";
    private string _fetchPhase = "idle";      // idle / launching / launched / capturing
    private string _grabHost = "";

    // ---- 批量 ----
    private bool _batchActive;
    private bool _batchAuto;
    private bool _batchPaused;
    private string _batchStage = "idle";      // idle / serial / para / login
    private string _batchProgress = "批量抓取待命: 选中源后点「自动抓取」或「手动逐站」";
    private double _batchPercent;
    private int _paraTabs = ParaTabsDefault;
    private bool _skipConfigured;
    private int _batchIndex = -1;
    private int _paraDone;
    private int _paraTotal;
    private int _loginDone;
    private int _loginTotal;
    private int _loginGot;
    private int _loginSkip;
    private string _curHost = "";
    private int _curIdle = AutoIdleSecs;

    /// <summary>源列表 (auth.list 全量; ● 配置状态点就地更新)。</summary>
    public ObservableCollection<AuthEntry> Sources { get; } = new();

    /// <summary>过滤后的显示列表 (ListView 数据源)。</summary>
    public ObservableCollection<AuthEntry> FilteredSources { get; } = new();

    /// <summary>本页运行日志。</summary>
    public ObservableCollection<string> LogBuffer { get; } = new();

    /// <summary>auth.fetch ack 到达 (单站/批量共用; 页面借此驱动下一步命令)。UI 线程触发。</summary>
    public event Action<AuthFetchAckInfo>? FetchAck;

    /// <summary>auth.list 上屏完成 (页面借此重选选中项)。UI 线程触发。</summary>
    public event Action? ListRefreshed;

    /// <summary>过滤重建前后 (页面借此保住选中不丢)。UI 线程触发。</summary>
    public event Action? FilterChanging;
    public event Action? FilterChanged;

    // ------------------------------------------------------------- 可绑定态 --

    /// <summary>过滤关键字 (名称/URL 子串, 不区分大小写)。</summary>
    public string FilterText
    {
        get => _filterText;
        set
        {
            if (SetProperty(ref _filterText, value))
            {
                RebuildFiltered();
            }
        }
    }

    /// <summary>auth.list 是否已加载完成 (驱动「加载中…」占位)。</summary>
    public bool ListLoaded
    {
        get => _listLoaded;
        private set => SetProperty(ref _listLoaded, value);
    }

    /// <summary>列表标题计数。</summary>
    public string SourceCountText => $"已配置站点 ({Sources.Count})";

    /// <summary>过滤计数文字。</summary>
    public string FilteredCountText => $"已配 {Sources.Count} · 显示 {FilteredSources.Count}";

    /// <summary>已选计数文字。</summary>
    public string SelectedCountText => $"已选 {_selectedUrls.Count}";

    /// <summary>详情标题 (当前源 / 多选提示 / 空选复位)。</summary>
    public string DetailTitle
    {
        get => _detailTitle;
        private set => SetProperty(ref _detailTitle, value);
    }

    /// <summary>Cookie 编辑框 (选中单个源时回填; 多选清空待填)。</summary>
    public string EditCookie
    {
        get => _editCookie;
        set => SetProperty(ref _editCookie, value);
    }

    /// <summary>自定义 header JSON 编辑框 (可空)。</summary>
    public string EditHeaderJson
    {
        get => _editHeaderJson;
        set => SetProperty(ref _editHeaderJson, value);
    }

    /// <summary>单站抓取状态文字。</summary>
    public string FetchStatus
    {
        get => _fetchStatus;
        set => SetProperty(ref _fetchStatus, value);
    }

    /// <summary>「浏览器登录抓取」按钮文案 (两段式切换)。</summary>
    public string FetchButtonText
    {
        get => _fetchButtonText;
        private set => SetProperty(ref _fetchButtonText, value);
    }

    /// <summary>抓取进行中 (launching/capturing → 按钮禁用)。</summary>
    public bool IsFetchBusy => _fetchPhase is "launching" or "capturing";

    /// <summary>浏览器已打开待抓取 (launched → 按钮可再点)。</summary>
    public bool IsFetchLaunched => _fetchPhase == "launched";

    /// <summary>「保存到所选」可用 (有选中)。</summary>
    public bool CanSave => _selectedUrls.Count > 0;

    /// <summary>「浏览器登录抓取」可用 (单选且未在抓取中)。</summary>
    public bool CanFetch => _selectedUrls.Count == 1 && !IsFetchBusy;

    // ------------------------------------------------------------- 批量状态 --

    /// <summary>批量是否在运行 (控制台按钮显隐)。</summary>
    public bool IsBatchRunning => _batchActive;

    /// <summary>批量模式: 自动 / 手动逐站。</summary>
    public bool IsBatchAuto => _batchAuto;

    /// <summary>阶段文字: 待命 / 手动逐站 / 并行扫 / 并行登录扫。</summary>
    public string BatchStageText => _batchStage switch
    {
        "serial" => "手动逐站",
        "para" => "并行扫",
        "login" => "并行登录扫",
        _ => "待命",
    };

    /// <summary>批量进度文字 (状态栏)。</summary>
    public string BatchProgress
    {
        get => _batchProgress;
        private set => SetProperty(ref _batchProgress, value);
    }

    /// <summary>批量进度百分比 (0~100)。</summary>
    public double BatchPercent
    {
        get => _batchPercent;
        private set => SetProperty(ref _batchPercent, value);
    }

    /// <summary>暂停/继续按钮文案。</summary>
    public string PauseButtonText => _batchActive ? (_batchPaused ? "继续" : "暂停") : "暂停";

    /// <summary>「跳过该站」仅串行阶段可用 (并行阶段无单站语义, 与 core.skip_site 一致)。</summary>
    public bool CanSkipSite => _batchActive && _batchStage == "serial";

    /// <summary>「抓取本站」仅手动逐站 (serial) 运行态可用 (自动模式打开即抓)。</summary>
    public bool CanGrabCurrent => _batchActive && _batchStage == "serial";

    /// <summary>并行标签数 (Slider 2~20, 默认 10)。</summary>
    public int ParaTabs
    {
        get => _paraTabs;
        set
        {
            var clamped = Math.Clamp(value, ParaTabsMin, ParaTabsMax);
            if (SetProperty(ref _paraTabs, clamped))
            {
                OnPropertyChanged(nameof(ParaTabsText));
                TraceLog.Write("ParaTabs set " + clamped);
            }
        }
    }

    public string ParaTabsText => _paraTabs.ToString();

    /// <summary>跳过已配 Cookie 的站点 (批量目标收集时生效)。</summary>
    public bool SkipConfigured
    {
        get => _skipConfigured;
        set => SetProperty(ref _skipConfigured, value);
    }

    // --------------------------------------------------------------- 列表 --

    /// <summary>auth.list ack 上屏: 重建源列表, 进入「已加载」态。</summary>
    public void ApplyAuthList(IReadOnlyList<AuthEntryDto>? entries)
    {
        var keep = new HashSet<string>(_selectedUrls);
        Sources.Clear();
        if (entries is not null)
        {
            foreach (var dto in entries)
            {
                Sources.Add(FromDto(dto));
            }
        }

        ListLoaded = true;
        OnPropertyChanged(nameof(SourceCountText));
        RebuildFiltered();
        TraceLog.Write($"auth.list: {Sources.Count} entries");

        // 保住刷新前仍存在的选中 (保存/删除后重拉列表不丢选择语义)。
        _selectedUrls.Clear();
        _selectedUrls.AddRange(keep.Where(u => Sources.Any(s => s.Url == u)));
        OnSelectionCountChanged();
        AppendLog($"· 登录头清单: {Sources.Count} 个已配置站点");
        ListRefreshed?.Invoke();
    }

    /// <summary>auth.list 应答无数据 (解析失败/形状不符): 保持旧列表, 仅解锁加载态。</summary>
    public void FinishListLoad()
    {
        ListLoaded = true;
        AppendLog("· 登录头清单读取失败, 保持现有列表。");
    }

    /// <summary>DTO → 行模型 (header dict 序列化为可编辑 JSON 文本)。</summary>
    private static AuthEntry FromDto(AuthEntryDto dto)
    {
        var cookie = dto.Cookie ?? "";
        return new AuthEntry
        {
            Url = dto.Url ?? "",
            Name = HostOf(dto.Url),
            Cookie = cookie,
            HeaderJson = dto.HeaderJson,
            HasCookie = !string.IsNullOrWhiteSpace(cookie),
        };
    }

    private void RebuildFiltered()
    {
        FilterChanging?.Invoke();
        FilteredSources.Clear();
        var kw = (_filterText ?? "").Trim();
        foreach (var s in Sources)
        {
            if (kw.Length == 0
                || s.Name.Contains(kw, StringComparison.OrdinalIgnoreCase)
                || s.Url.Contains(kw, StringComparison.OrdinalIgnoreCase))
            {
                FilteredSources.Add(s);
            }
        }

        OnPropertyChanged(nameof(FilteredCountText));
        FilterChanged?.Invoke();
        TraceLog.Write($"RebuildFiltered n={FilteredSources.Count}");
    }

    // ------------------------------------------------------------- 选择 --

    /// <summary>ListView 选中变化 → VM (items 为选中行模型; 空 = 清空)。</summary>
    public void SetSelection(IEnumerable<AuthEntry>? items)
    {
        _selectedUrls.Clear();
        if (items is not null)
        {
            foreach (var e in items)
            {
                if (!string.IsNullOrEmpty(e.Url))
                {
                    _selectedUrls.Add(e.Url);
                }
            }
        }

        OnSelectionChanged();
    }

    /// <summary>直接按 URL 设置选中 (页面重选 / 单测)。</summary>
    public void SetSelectedUrls(IEnumerable<string>? urls)
    {
        _selectedUrls.Clear();
        if (urls is not null)
        {
            _selectedUrls.AddRange(urls.Where(u => !string.IsNullOrEmpty(u)));
        }

        OnSelectionChanged();
    }

    /// <summary>当前选中 URL (页面发命令用)。</summary>
    public IReadOnlyList<string> SelectedUrls => _selectedUrls.ToArray();

    private void OnSelectionChanged()
    {
        OnSelectionCountChanged();
        if (_selectedUrls.Count == 1)
        {
            var e = Sources.FirstOrDefault(s => s.Url == _selectedUrls[0]);
            if (e is not null)
            {
                DetailTitle = $"当前源: {e.Name}{(e.HasCookie ? "  ● 已配置" : "  ○ 未配置")}";
                EditCookie = e.Cookie;
                EditHeaderJson = e.HeaderJson;
            }
        }
        else if (_selectedUrls.Count > 1)
        {
            DetailTitle = $"已选 {_selectedUrls.Count} 个源 · 输入将整项覆盖保存到所选全部源";
            EditCookie = "";
            EditHeaderJson = "";
        }
        else
        {
            DetailTitle = "未选中 · 在左侧列表点选站点（可多选）";
            EditCookie = "";
            EditHeaderJson = "";
        }

        TraceLog.Write($"OnSelectionChanged n={_selectedUrls.Count}");
    }

    private void OnSelectionCountChanged()
    {
        OnPropertyChanged(nameof(SelectedCountText));
        OnPropertyChanged(nameof(CanSave));
        OnPropertyChanged(nameof(CanFetch));
        TraceLog.Write("SelCount " + _selectedUrls.Count);
    }

    // ------------------------------------------------------------- 保存/删除 --

    /// <summary>构造「保存到所选」载荷 (选中 URL + 编辑框 cookie/header JSON)。</summary>
    public AuthSavePayload? PrepareSave()
    {
        if (_selectedUrls.Count == 0)
        {
            AppendLog("✘ 未选中任何源, 无法保存。");
            return null;
        }

        return new AuthSavePayload(_selectedUrls.ToArray(), EditCookie ?? "", EditHeaderJson ?? "");
    }

    /// <summary>构造「删除所选」载荷 (选中 URL + 空 cookie/header = 移除配置)。</summary>
    public AuthSavePayload? PrepareRemove()
    {
        if (_selectedUrls.Count == 0)
        {
            AppendLog("✘ 未选中任何源, 无法删除。");
            return null;
        }

        return new AuthSavePayload(_selectedUrls.ToArray(), "", "");
    }

    /// <summary>auth.save ack 落账 (count 为后端现有配置总数)。</summary>
    public void OnAuthSavedAck(int count)
    {
        AppendLog($"✔ 已保存: 应用到所选 {_selectedUrls.Count} 个站点 · 现有配置共 {count} 条。");
    }

    /// <summary>保存/删除命令发出后就地更新列表行 (● 状态点与已存值), 不必等重拉。</summary>
    public void ApplySavedPayload(AuthSavePayload payload)
    {
        foreach (var u in payload.Urls)
        {
            var e = Sources.FirstOrDefault(s => s.Url == u);
            if (e is null)
            {
                continue;
            }

            var configured = !string.IsNullOrWhiteSpace(payload.Cookie)
                             || !string.IsNullOrWhiteSpace(payload.HeaderJson);
            e.Cookie = configured ? payload.Cookie : "";
            e.HeaderJson = configured ? payload.HeaderJson : "";
            e.HasCookie = configured;
        }
    }

    // ------------------------------------------------------------- 单站抓取 --

    /// <summary>「浏览器登录抓取」第一步: 返回要打开的 url (单选才允许)。</summary>
    public string? PrepareFetchLaunch()
    {
        if (_selectedUrls.Count != 1)
        {
            AppendLog($"✘ 浏览器登录抓取需选中单个站点（当前选中 {_selectedUrls.Count} 个）。");
            return null;
        }

        _grabHost = "";
        SetFetchPhase("launching");
        FetchStatus = "正在启动浏览器…";
        return _selectedUrls[0];
    }

    /// <summary>抓取进行中 (launched → 用户点「我登录好了 → 抓取」)。</summary>
    public void BeginFetchGrab()
    {
        if (_fetchPhase != "launched")
        {
            return;
        }

        SetFetchPhase("capturing");
        FetchStatus = "正在从浏览器抓取 Cookie…";
    }

    /// <summary>本次抓取的 host (grab_host 优先, 其次选中源 host)。</summary>
    public string CurrentGrabHost
    {
        get
        {
            if (!string.IsNullOrEmpty(_grabHost))
            {
                return _grabHost;
            }

            return HostOf(_selectedUrls.FirstOrDefault() ?? "");
        }
    }

    /// <summary>
    /// auth.fetch ack 入口 (单站与批量共用)。批量进行中按批量语义处理,
    /// 否则走单站两段式。处理完统一发 <see cref="FetchAck"/> 供页面驱动下一步。
    /// </summary>
    public void ApplyFetchAck(AuthFetchAckInfo info)
    {
        if (_batchActive)
        {
            if (info.Phase == "launched")
            {
                OnBatchLaunched(info);
            }
            else if (info.Phase == "captured")
            {
                OnBatchCaptured(info);
            }

            FetchAck?.Invoke(info);
            return;
        }

        if (info.Phase == "launched")
        {
            _grabHost = info.GrabHost.Length > 0
                ? info.GrabHost
                : HostOf(info.Url.Length > 0 ? info.Url : _selectedUrls.FirstOrDefault() ?? "");
            SetFetchPhase("launched");
            FetchStatus = "浏览器已启动 —— 在浏览器里登录该站即可(已登录可忽略), 完成后点「我登录好了 → 抓取」。";
        }
        else if (info.Phase == "captured")
        {
            SetFetchPhase("idle");
            if (info.Cookie.Length > 0)
            {
                if (_selectedUrls.Count == 1)
                {
                    EditCookie = info.Cookie;
                }

                FetchStatus = $"已抓取 {info.Cookie.Split(';', StringSplitOptions.RemoveEmptyEntries).Length} 条 Cookie 并填入 → 点「保存到所选」写入。";
            }
            else
            {
                FetchStatus = "没抓到该站点的 Cookie —— 浏览器打开的可能不是目标站点(网址无效会落到浏览器主页), 可点「重试抓取」或手动粘贴。";
            }
        }

        FetchAck?.Invoke(info);
    }

    private void SetFetchPhase(string phase)
    {
        _fetchPhase = phase;
        FetchButtonText = phase switch
        {
            "launching" => "启动中…",
            "launched" => "我登录好了 → 抓取",
            "capturing" => "抓取中…",
            _ => "浏览器登录抓取",
        };
        OnPropertyChanged(nameof(IsFetchBusy));
        OnPropertyChanged(nameof(IsFetchLaunched));
        OnPropertyChanged(nameof(CanFetch));
    }

    /// <summary>单站抓取失败 (error/busy 带 auth.fetch): 复位, 保留可重试。</summary>
    public void OnFetchError(string message)
    {
        if (_batchActive)
        {
            FinishBatch("批量抓取中止: " + message);
        }
        else
        {
            SetFetchPhase("idle");
            FetchStatus = message;
        }

        AppendLog("✘ " + message);
    }

    /// <summary>auth 命令被后端拒绝/报错 (cmd 区分语义)。</summary>
    public void OnAuthError(string cmd, string message)
    {
        if (cmd == "auth.fetch")
        {
            OnFetchError(message);
        }
        else
        {
            AppendLog("✘ [后端] " + message);
        }
    }

    /// <summary>busy 拒绝 (auth.fetch 被其它重操作占用)。</summary>
    public void OnAuthRejected(string message)
    {
        OnFetchError(message);
    }

    // ------------------------------------------------------------- 批量 --

    /// <summary>
    /// 批量目标收集 (镜像 core.auth_manager.collect_targets): URL 按站点主机去重
    /// (无协议补 http://), skipConfigured 时跳过任一 URL 已配 Cookie 的主机。
    /// </summary>
    public BatchStartResult BuildBatchTargets(IReadOnlyList<string>? urls, bool skipConfigured)
    {
        var all = new Dictionary<string, HashSet<string>>();
        foreach (var raw in urls ?? Array.Empty<string>())
        {
            var u = (raw ?? "").Trim();
            if (u.Length == 0)
            {
                continue;
            }

            if (!u.Contains("://", StringComparison.Ordinal))
            {
                u = "http://" + u;
            }

            var host = HostOf(u);
            if (host.Length == 0)
            {
                continue;
            }

            if (!all.TryGetValue(host, out var set))
            {
                all[host] = set = new HashSet<string>();
            }

            set.Add(u);
        }

        var targets = new Dictionary<string, IReadOnlyList<string>>();
        var skipped = 0;
        foreach (var (host, us) in all)
        {
            var list = us.ToList();
            if (skipConfigured && us.Any(IsConfigured))
            {
                skipped++;
                continue;
            }

            targets[host] = list;
        }

        return new BatchStartResult(targets.Keys.ToList(), targets, skipped, all.Count);
    }

    /// <summary>该 URL 是否已配 Cookie (列表 ● 状态)。</summary>
    public bool IsConfigured(string url)
        => Sources.Any(s => s.Url == url && s.HasCookie);

    /// <summary>
    /// 开始批量: 落位 hosts/targets/k 并进入阶段 (镜像 core.AuthManager.begin)。
    /// auto → stage=para (并行扫近似), 手动 → stage=serial。
    /// </summary>
    public void BeginBatch(
        bool auto, IReadOnlyList<string> hosts,
        IReadOnlyDictionary<string, IReadOnlyList<string>> targets, int k)
    {
        _hosts.Clear();
        _hosts.AddRange(hosts);
        _targets.Clear();
        foreach (var (h, us) in targets)
        {
            _targets[h] = us.ToList();
        }

        _batchIndex = -1;
        _batchActive = true;
        _batchAuto = auto;
        _batchPaused = false;
        ParaTabs = k;
        if (auto)
        {
            _batchStage = "para";
            _paraDone = 0;
            _paraTotal = _hosts.Count;
        }
        else
        {
            _batchStage = "serial";
        }

        OnBatchStateChanged();
        AppendLog($"▶ 批量抓取开始({(auto ? "自动" : "手动逐站")}): {_hosts.Count} 个站点 · 并行标签 {k}");
    }

    /// <summary>推进到下一站 (镜像 core.next_site); 返回下一站 host, 越界返回 null 并收尾。</summary>
    public string? NextSite()
    {
        if (!_batchActive)
        {
            return null;
        }

        _batchIndex++;
        if (_batchIndex >= _hosts.Count)
        {
            FinishBatch(_batchAuto
                ? $"全自动完成: 处理 {_paraTotal} 站 · 抓到并保存 {_paraDone} 站。"
                : $"批量抓取结束: 共处理 {_hosts.Count} 个站点, Cookie 已全部注入。");
            return null;
        }

        _curHost = _hosts[_batchIndex];
        if (_batchAuto)
        {
            BatchProgress = $"并行扫 {_paraDone + 1}/{_paraTotal} 站({_paraTabs} 标签): 正在打开 {_curHost} …";
        }
        else
        {
            BatchProgress = $"批量 {_batchIndex + 1}/{_hosts.Count}: {_curHost} —— 在浏览器里登录(已登录可忽略), 点「抓取本站」; 不用配 Cookie 点「跳过该站」。";
        }

        return _curHost;
    }

    /// <summary>当前站 host (页面发命令用)。</summary>
    public string CurrentBatchHost => _curHost;

    /// <summary>当前站的源 URL 列表 (抓到 Cookie 后按这些 URL 保存)。</summary>
    public IReadOnlyList<string> TargetUrlsFor(string host)
        => _targets.TryGetValue(host, out var us) ? (IReadOnlyList<string>)us : Array.Empty<string>();

    /// <summary>「跳过该站」: 串行阶段跳下一站; 并行阶段无单站语义 (与 core 一致)。</summary>
    public string? SkipSite()
    {
        if (!_batchActive)
        {
            return null;
        }

        if (_batchStage is "para" or "login")
        {
            BatchProgress = "并行阶段进行中, 不支持单站跳过; 超时的站会自动跳过, 也可点「结束批量」。";
            return null;
        }

        AppendLog($"批量: 跳过 {_curHost}");
        return NextSite();
    }

    /// <summary>暂停/继续 (冻结并行倒计时; 前端近似下主要是控制台状态切换)。</summary>
    public bool TogglePause()
    {
        if (!_batchActive)
        {
            return _batchPaused;
        }

        _batchPaused = !_batchPaused;
        OnPropertyChanged(nameof(PauseButtonText));
        BatchProgress = _batchPaused
            ? "已暂停: 全部标签倒计时已冻结(标签保持打开), 点「继续」恢复。"
            : "已继续。";
        return _batchPaused;
    }

    /// <summary>「结束批量」: 停止并收尾 (已抓到的 Cookie 保留)。</summary>
    public void StopBatch()
    {
        if (!_batchActive)
        {
            return;
        }

        var summary = _batchStage == "login"
            ? "已结束批量(并行登录扫收尾, 已抓到的 Cookie 保留)。"
            : "批量抓取已结束(已完成站点的 Cookie 保留)。";
        FinishBatch(summary);
    }

    /// <summary>批量收尾 (自然结束 / 中止 / 后端进程退出)。</summary>
    public void FinishBatch(string summary)
    {
        _batchActive = false;
        _batchStage = "idle";
        _batchPaused = false;
        BatchPercent = 0;
        BatchProgress = "批量抓取待命: 选中源后点「自动抓取」或「手动逐站」";
        OnBatchStateChanged();
        AppendLog("■ " + summary);
    }

    /// <summary>后端进程退出 / 其它全局中止: 批量与单站一起复位。</summary>
    public void AbortAll(string reason)
    {
        if (_batchActive)
        {
            FinishBatch("批量已中止: " + reason);
        }

        if (_fetchPhase != "idle")
        {
            SetFetchPhase("idle");
            FetchStatus = reason;
        }
    }

    private void OnBatchLaunched(AuthFetchAckInfo info)
    {
        if (!_batchActive)
        {
            return;
        }

        if (_batchAuto)
        {
            BatchProgress = $"并行扫 {_paraDone + 1}/{_paraTotal} 站({_paraTabs} 标签): {_curHost} 已打开, 正在抓取…";
        }
        else
        {
            var warn = info.Url.Length > 0 && info.GrabHost.Length > 0
                && !HostOf(info.Url).Equals(info.GrabHost, StringComparison.OrdinalIgnoreCase)
                ? $" ⚠ 打开页与目标站不符(页面在 {HostOf(info.Url)})"
                : "";
            BatchProgress = $"批量 {_batchIndex + 1}/{_hosts.Count}: {_curHost} —— 浏览器已打开{warn}。在浏览器里登录后点「抓取本站」, 或点「跳过该站」。";
        }
    }

    private void OnBatchCaptured(AuthFetchAckInfo info)
    {
        if (!_batchActive)
        {
            return;
        }

        if (_batchStage == "para")
        {
            _paraDone++;
            BatchPercent = _paraTotal > 0 ? (double)_paraDone / _paraTotal * 100 : 0;
        }

        if (info.Cookie.Length > 0)
        {
            AppendLog($"批量: 已保存 {info.Host} 的 Cookie({TargetUrlsFor(info.Host).Count} 个源)");
        }
        else
        {
            AppendLog($"批量: {info.Host} 未抓到 Cookie(可能打不开/不是目标站), 已跳过");
        }
    }

    /// <summary>pprog 事件 (后端补齐三段式后挂载; 当前批量以两段式近似, 不经此路径)。</summary>
    public void ApplyParaProgress(int done, int total, int k)
    {
        _paraDone = done;
        _paraTotal = total;
        ParaTabs = k;
        BatchPercent = total > 0 ? (double)done / total * 100 : 0;
        BatchProgress = $"并行扫 {done}/{total} 站({k} 标签, 完成后进入并行登录扫)…";
    }

    /// <summary>login_stage 事件入口 (进入并行登录扫)。</summary>
    public void EnterLoginStage(int nInstant, int nPara, int nLeft, int k)
    {
        _batchStage = "login";
        _loginDone = 0;
        _loginTotal = nLeft;
        OnBatchStateChanged();
        BatchProgress = $"并行扫完成: 秒过 {nInstant} 站 + 并行扫抓到 {nPara} 站; 剩余 {nLeft} 站进入并行登录扫({k} 标签)。";
    }

    /// <summary>lprog 事件 (并行登录扫进度)。</summary>
    public void ApplyLoginProgress(int done, int total, int k, int got, int skip, string curHost, int idle)
    {
        _loginDone = done;
        _loginTotal = total;
        _loginGot = got;
        _loginSkip = skip;
        _curHost = curHost;
        _curIdle = idle;
        ParaTabs = k;
        BatchPercent = total > 0 ? (double)done / total * 100 : 0;
        BatchProgress = $"并行登录 {done}/{total} 站（{k} 个标签）: 已抓 {got} · 已跳 {skip}｜{curHost} —— 在浏览器里登录该站即可，{idle}s 无操作会自动抓取并跳到下一站。";
    }

    private void OnBatchStateChanged()
    {
        OnPropertyChanged(nameof(IsBatchRunning));
        OnPropertyChanged(nameof(IsBatchAuto));
        OnPropertyChanged(nameof(BatchStageText));
        OnPropertyChanged(nameof(PauseButtonText));
        OnPropertyChanged(nameof(CanSkipSite));
        OnPropertyChanged(nameof(CanGrabCurrent));
    }

    // --------------------------------------------------------------- 日志 --

    /// <summary>追加一行日志 (超上限丢最旧)。</summary>
    public void AppendLog(string? message)
    {
        if (string.IsNullOrEmpty(message))
        {
            return;
        }

        _log.Add(message);
        LogBuffer.Add(message);
        TrimLogs();
    }

    /// <summary>批量日志 (UiThrottle 排空路径, 全局 log 镜像到本页)。</summary>
    public void AppendLogs(IReadOnlyList<string> messages)
    {
        var added = false;
        foreach (var m in messages)
        {
            if (string.IsNullOrEmpty(m))
            {
                continue;
            }

            _log.Add(m);
            LogBuffer.Add(m);
            added = true;
        }

        if (added)
        {
            TrimLogs();
        }
    }

    /// <summary>日志快照 (便于断言/导出)。</summary>
    public IReadOnlyList<string> LogSnapshot() => _log.ToArray();

    private void TrimLogs()
    {
        while (_log.Count > LogCapacity)
        {
            _log.RemoveAt(0);
            LogBuffer.RemoveAt(0);
        }
    }

    // --------------------------------------------------------------- 助手 --

    /// <summary>URL → 站点主机 (无协议补 http://; 截到 路径/端口/片段 前)。</summary>
    public static string HostOf(string? url)
    {
        var s = (url ?? "").Trim();
        var scheme = s.IndexOf("://", StringComparison.Ordinal);
        if (scheme >= 0)
        {
            s = s.Substring(scheme + 3);
        }

        var cut = s.IndexOfAny(new[] { '/', '?', '#', ':' });
        if (cut >= 0)
        {
            s = s.Substring(0, cut);
        }

        return s;
    }
}

/// <summary>登录头源列表行 (auth.list 条目; ● 配置状态点就地上屏)。</summary>
public sealed class AuthEntry : ObservableObject
{
    public string Url { get; init; } = "";

    /// <summary>显示名 (站点主机; server 无书源名, 以 host 代)。</summary>
    public string Name { get; init; } = "";

    private string _cookie = "";
    private string _headerJson = "";
    private bool _hasCookie;

    public string Cookie
    {
        get => _cookie;
        set => SetProperty(ref _cookie, value);
    }

    public string HeaderJson
    {
        get => _headerJson;
        set => SetProperty(ref _headerJson, value);
    }

    public bool HasCookie
    {
        get => _hasCookie;
        set
        {
            if (SetProperty(ref _hasCookie, value))
            {
                OnPropertyChanged(nameof(Dot));
                OnPropertyChanged(nameof(ConfigText));
            }
        }
    }

    /// <summary>配置状态点: ● 已配 / ○ 未配。</summary>
    public string Dot => HasCookie ? "●" : "○";

    public string ConfigText => HasCookie ? "已配置" : "未配置";
}

/// <summary>「保存到所选」/「删除所选」载荷 (页面据此发 auth.save)。</summary>
public sealed record AuthSavePayload(IReadOnlyList<string> Urls, string Cookie, string HeaderJson);

/// <summary>批量目标收集结果 (host 去重 + 跳过已配站)。</summary>
public sealed record BatchStartResult(
    IReadOnlyList<string> Hosts,
    IReadOnlyDictionary<string, IReadOnlyList<string>> Targets,
    int Skipped,
    int TotalHosts);
