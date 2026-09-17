using System;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 日志与设置页 VM (M5 落地)。
///
/// 纯逻辑, 不引用任何 WinUI 类型:
///   - 主题三选 (ThemeMode) 由设置页 RadioButtons → <see cref="SetTheme"/> 驱动,
///     页面同步调 App.ApplyTheme 即时生效; VM 负责记忆 (经 SettingsStore 持久化)。
///   - 关于区: 应用版本 (程序集版本号, 默认 1.0.0.0 时回退 v1.9.x 占位) +
///     后端版本 / JS 引擎状态 (来自 hello 的 <see cref="BackendHello"/>,
///     App.OnLaunched 订阅 EventRouter.HelloReceived 后灌入本 VM)。
///   - 数据区按钮为 M5 占位 (页面 AppendLog 提示未接真实操作)。
/// </summary>
public sealed class LogSettingsViewModel : ObservableObject
{
    private readonly SettingsStore? _store;

    private ThemeMode _theme;
    private string _appVersion;
    private string _backendVersion = "未连接";
    private string _jsEngineStatus = "未连接";
    private string _backendSummary = "";

    public LogSettingsViewModel(SettingsStore? store = null)
    {
        _store = store;
        _theme = store?.Theme ?? ThemeMode.System;
        _appVersion = ResolveAppVersion();
    }

    // ------------------------------------------------------------- 可绑定态 --

    /// <summary>当前主题 (RadioButtons 初值 + 记忆源)。</summary>
    public ThemeMode Theme
    {
        get => _theme;
        private set => SetProperty(ref _theme, value);
    }

    /// <summary>应用版本 (程序集; 默认版本回退 v1.9.x 占位)。</summary>
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

    /// <summary>数据目录 (M5 占位展示; 与下载逻辑的 outDir 同源)。</summary>
    public string DataDir
        => System.IO.Path.Combine(AppContext.BaseDirectory, "downloads");

    /// <summary>设置文件路径 (诊断/关于展示)。</summary>
    public string SettingsFilePath => _store?.SettingsFilePath ?? "";

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

    /// <summary>hello 到达: 关于区刷新后端版本 / JS 引擎状态。</summary>
    public void ApplyHello(BackendHello hello)
    {
        BackendVersion = string.IsNullOrEmpty(hello.Version) ? "未知" : $"v{hello.Version}";
        JsEngineStatus = hello.Js ? "可用 ✓" : "不可用 ✗";
        BackendSummary = hello.Summary;
    }

    // ------------------------------------------------------------- 助手 --

    /// <summary>
    /// 应用版本: 读程序集版本号; 未显式声明 (默认 1.0.0.0, csproj 未设 Version) 时
    /// 回退 v1.9.x 占位 —— 与后端 server.py 的 v1.9 大版本口径保持一致。
    /// </summary>
    private static string ResolveAppVersion()
    {
        var v = typeof(LogSettingsViewModel).Assembly.GetName().Version;
        if (v is null || (v.Major == 1 && v.Minor == 0 && v.Build == 0))
        {
            return "v1.9.0 (M5 占位)";
        }

        return $"v{v.Major}.{v.Minor}.{v.Build}";
    }
}
