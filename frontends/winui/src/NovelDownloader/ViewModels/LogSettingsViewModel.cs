using System;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 日志与设置页 VM。
///
/// 纯逻辑, 不引用任何 WinUI 类型:
///   - 主题三选 (ThemeMode) 由设置页 RadioButtons → <see cref="SetTheme"/> 驱动,
///     页面同步调 App.ApplyTheme 即时生效; VM 负责记忆 (经 SettingsStore 持久化)。
///   - 关于区: 应用版本 (程序集版本号) + 后端版本 / JS 引擎状态 (来自 hello)。
///   - 数据区: 数据存储目录经 hello / data.getdir / data.setdir ack 灌入;
///     迁移与清理是"发命令 → 等 ack"的异步往返, VM 只维护忙态并把结果经事件
///     抛给页面 (页面负责写 settings.json 与提示文案), 单测可用 EventRouter
///     RouteRawLine 喂 ack 行断言状态机。
/// </summary>
public sealed class LogSettingsViewModel : ObservableObject
{
    private readonly SettingsStore? _store;

    private ThemeMode _theme;
    private string _appVersion;
    private string _backendVersion = "未连接";
    private string _jsEngineStatus = "未连接";
    private string _backendSummary = "";
    private string _dataDir = "";
    private bool _isBusy;
    private string _busyCmd = "";

    public LogSettingsViewModel(SettingsStore? store = null)
    {
        _store = store;
        _theme = store?.Theme ?? ThemeMode.System;
        _dataDir = store?.DataDir ?? "";
        _appVersion = ResolveAppVersion();
    }

    // ------------------------------------------------------------- 可绑定态 --

    /// <summary>当前主题 (RadioButtons 初值 + 记忆源)。</summary>
    public ThemeMode Theme
    {
        get => _theme;
        private set => SetProperty(ref _theme, value);
    }

    /// <summary>应用版本 (程序集版本号, 形如 v1.31)。</summary>
    public string AppVersion => _appVersion;

    /// <summary>后端版本 (hello.version, 前缀 v)。</summary>
    public string BackendVersion
    {
        get => _backendVersion;
        private set => SetProperty(ref _backendVersion, value);
    }

    /// <summary>JS 引擎状态 (hello.js)。</summary>
    public string JsEngineStatus
    {
        get => _jsEngineStatus;
        private set => SetProperty(ref _jsEngineStatus, value);
    }

    /// <summary>hello 摘要一行 (约等于主页面状态栏)。</summary>
    public string BackendSummary
    {
        get => _backendSummary;
        private set => SetProperty(ref _backendSummary, value);
    }

    /// <summary>数据存储目录 (后端 hello / data.* ack 灌入; 未连接时为本地记忆值)。</summary>
    public string DataDir
    {
        get => _dataDir;
        private set
        {
            if (SetProperty(ref _dataDir, value))
            {
                OnPropertyChanged(nameof(DataDirDisplay));
            }
        }
    }

    /// <summary>数据目录展示文本 (空 = 后端还没握手)。</summary>
    public string DataDirDisplay => string.IsNullOrEmpty(_dataDir) ? "后端未连接" : _dataDir;

    /// <summary>是否有设置页命令在途 (迁移/清理等 ack 期间禁用按钮)。</summary>
    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            if (SetProperty(ref _isBusy, value))
            {
                OnPropertyChanged(nameof(CanEdit));
                OnPropertyChanged(nameof(BusyHint));
            }
        }
    }

    /// <summary>按钮可用性 (与 IsBusy 互补)。</summary>
    public bool CanEdit => !_isBusy;

    /// <summary>在途命令提示文本 (空闲时为空串)。</summary>
    public string BusyHint => _busyCmd switch
    {
        "data.setdir" when _isBusy => "正在更改数据目录…",
        "cache.clear" when _isBusy => "正在清除缓存…",
        "data.clear" when _isBusy => "正在清除数据…",
        _ when _isBusy => "正在执行…",
        _ => "",
    };

    // ------------------------------------------------------------- 结果事件 --

    /// <summary>data.setdir ack 到达 (页面据此写 settings.json 并提示迁移摘要)。</summary>
    public event Action<DataSetDirResult>? DirMigrated;

    /// <summary>cache.clear / data.clear ack 到达 (cmd, 结果)。</summary>
    public event Action<string, ClearResult>? ClearFinished;

    // ------------------------------------------------------------- 行为 --

    /// <summary>设置主题: 更新 VM 态 + 持久化。页面另行调 App.ApplyTheme 做即时渲染。</summary>
    public void SetTheme(ThemeMode theme)
    {
        if (Theme == theme)
        {
            return;
        }

        Theme = theme;
        _store?.SetTheme(theme);
    }

    /// <summary>页面已选定新目录并发出 data.setdir 前调用: 记录在途命令、锁按钮。</summary>
    public void BeginCommand(string cmd)
    {
        _busyCmd = cmd;
        IsBusy = true;
    }

    /// <summary>把 data.setdir 成功结果持久化到本地 settings.json (页面 ack 回调里调)。</summary>
    public void SaveDataDir(string? dataDir) => _store?.SetDataDir(dataDir);

    /// <summary>hello 到达: 关于区刷新后端版本 / JS 引擎状态, 数据区刷新目录。</summary>
    public void ApplyHello(BackendHello hello)
    {
        BackendVersion = string.IsNullOrEmpty(hello.Version) ? "未知" : $"v{hello.Version}";
        JsEngineStatus = hello.Js ? "可用 ✓" : "不可用 ✗";
        BackendSummary = hello.Summary;
        if (!string.IsNullOrEmpty(hello.DataDir))
        {
            DataDir = hello.DataDir;
        }
    }

    /// <summary>data.getdir / config.get ack 到达: 刷新目录显示。</summary>
    public void ApplyPaths(DataDirPaths paths)
    {
        if (!string.IsNullOrEmpty(paths.DataDir))
        {
            DataDir = paths.DataDir;
        }
    }

    /// <summary>data.setdir ack: 解锁 + 目录落到新值 + 抛给页面做持久化/提示。</summary>
    public void OnSetDirAck(DataSetDirResult result)
    {
        IsBusy = false;
        ApplyPaths(result.Paths);
        DirMigrated?.Invoke(result);
    }

    /// <summary>cache.clear / data.clear ack: 解锁 + 抛结果给页面。</summary>
    public void OnClearAck(string cmd, ClearResult result)
    {
        IsBusy = false;
        ClearFinished?.Invoke(cmd, result);
    }

    /// <summary>在途命令被后端拒绝 (busy) 或报错: 解锁, 错误本身已由全局日志镜像。</summary>
    public void OnBackendError(string cmd, string message)
    {
        if (_isBusy && _busyCmd == cmd)
        {
            IsBusy = false;
        }
    }

    // ------------------------------------------------------------- 助手 --

    /// <summary>应用版本: 读程序集版本号 (csproj &lt;Version&gt;), 显示 v主.次。</summary>
    private static string ResolveAppVersion()
    {
        var v = typeof(LogSettingsViewModel).Assembly.GetName().Version;
        return v is null ? "未知" : $"v{v.Major}.{v.Minor}";
    }
}
