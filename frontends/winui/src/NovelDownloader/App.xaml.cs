using System;
using System.Threading.Tasks;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader;

/// <summary>
/// 应用入口。M1: 负责后端进程 (BackendClient) 与事件路由 (EventRouter) 的生命周期,
/// 并给搜索页注入共享的 MainViewModel。
///
/// 启动顺序 (关键 —— 先建 VM/路由再建窗口, 页面才能拿到已接线的 VM):
///   1. 建 BackendClient + EventRouter (编组到当前 UI 线程的 DispatcherQueue);
///   2. 建 MainViewModel, Router.Attach(vm);
///   3. 建 MainWindow 并 Activate;
///   4. 异步 StartAsync + InitAsync —— 后端就绪后回 hello, 走 log 进日志区。
/// 退出: 窗口关闭 → 释放 Router/BackendClient (杀 Python 进程树)。
/// </summary>
public partial class App : Application
{
    private Window? _window;

    /// <summary>共享搜索页 VM (MainPage 直接取用)。</summary>
    public static MainViewModel? MainVM { get; private set; }

    /// <summary>共享书源页 VM (SourcesPage 直接取用; 校验进行中切页不中断)。</summary>
    public static SourcesViewModel? SourcesVM { get; private set; }

    /// <summary>共享登录头页 VM (AuthPage 直接取用; 批量抓取进行中切页不中断)。</summary>
    public static AuthViewModel? AuthVM { get; private set; }

    /// <summary>日志与设置页 VM (LogSettingsPage 直接取用)。</summary>
    public static LogSettingsViewModel? LogSettingsVM { get; private set; }

    /// <summary>本地设置存储 (主题 / 首启引导, %LOCALAPPDATA%\NovelDownloader\settings.json)。</summary>
    public static Services.SettingsStore? Settings { get; private set; }

    /// <summary>主窗口 (文件选择器等需要窗口句柄的场景用)。</summary>
    public static Window? Window { get; private set; }

    /// <summary>后端进程客户端。</summary>
    public static BackendClient? Backend { get; private set; }

    /// <summary>事件路由 (后台行 → UI 线程 → VM)。</summary>
    public static EventRouter? Router { get; private set; }

    public App()
    {
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        var dispatcher = DispatcherQueue.GetForCurrentThread();

        // 设置存储先建 (MainWindow 首启引导 / LogSettingsPage 主题初值都要读)。
        Settings = new Services.SettingsStore();

        Backend = new BackendClient();
        Router = new EventRouter(Backend, dispatcher);

        MainVM = new MainViewModel();
        Router.Attach(MainVM);

        SourcesVM = new SourcesViewModel();
        Router.Attach(SourcesVM);

        AuthVM = new AuthViewModel();
        Router.Attach(AuthVM);

        // 设置页 VM 独立接线: hello 到达 → 关于区刷新后端版本 / JS 引擎状态。
        LogSettingsVM = new LogSettingsViewModel(Settings);
        Router.HelloReceived += OnHelloReceived;

        _window = new MainWindow();
        Window = _window;
        _window.Closed += OnWindowClosed;
        _window.Activate();

        // 记忆主题即时生效 (设置页三选, 无需重启)。在 Activate 前设 RequestedTheme 同样有效。
        ApplySavedTheme();

        // 启动后端并握手。fire-and-forget; 失败/异常都经 EventRouter 落到日志区。
        _ = StartBackendAsync();
    }

    private static void OnHelloReceived(Services.Backend.BackendHello hello)
        => LogSettingsVM?.ApplyHello(hello);

    /// <summary>把本地记忆的主题应用到窗口根 (ElementTheme.Default = 跟随系统)。</summary>
    private static void ApplySavedTheme()
    {
        var mode = Settings?.Theme ?? Services.ThemeMode.System;
        ApplyTheme(mode switch
        {
            Services.ThemeMode.Light => ElementTheme.Light,
            Services.ThemeMode.Dark => ElementTheme.Dark,
            _ => ElementTheme.Default,
        });
    }

    private static async Task StartBackendAsync()
    {
        var backend = Backend;
        var vm = MainVM;
        if (backend is null)
        {
            return;
        }

        vm?.AppendLog("正在启动 Python 后端 (server.py)…");
        try
        {
            var ok = await backend.StartAsync();
            if (!ok)
            {
                vm?.AppendLog("✘ 后端启动失败, 后续操作不可用。");
                return;
            }

            await backend.InitAsync();
        }
        catch (Exception ex)
        {
            vm?.AppendLog($"✘ 后端启动异常: {ex.Message}");
        }
    }

    private void OnWindowClosed(object sender, WindowEventArgs args)
    {
        try
        {
            Router?.Dispose();
        }
        catch
        {
        }

        try
        {
            Backend?.Dispose();
        }
        catch
        {
        }

        Router = null;
        Backend = null;
    }

    /// <summary>M5: 供设置页切换主题时调用, 即时生效不重启。</summary>
    public static void ApplyTheme(ElementTheme theme)
    {
        if ((Application.Current as App)?._window?.Content is FrameworkElement root)
        {
            root.RequestedTheme = theme;
        }
    }
}
