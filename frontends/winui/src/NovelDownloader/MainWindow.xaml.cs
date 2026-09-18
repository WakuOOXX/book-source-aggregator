using System;
using System.Collections.Specialized;
using System.Reflection;
using Microsoft.UI;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using NovelDownloader.ViewModels;
using NovelDownloader.Views;

namespace NovelDownloader;

/// <summary>
/// 主窗口壳: 自定义 TitleBar (内容延伸, 左上汉堡) + Frame + 全局日志 + 右侧滑入导航面板。
/// Mica 背景在 Win11 生效, Win10 1809+ 自动退化为主题纯色 (#f3f6fa / #202020)。
/// 导航入口全部收进滑入面板 (搜索/书源/下载/登录头/日志与设置)。
/// 全局日志面板固定在 Frame 下方, 任何页面切换时可见。
/// 首启引导 (SettingsStore.SeenOnboarding 标记) 用 ContentDialog 展示三连提示。
/// </summary>
public sealed partial class MainWindow : Window
{
    private readonly ShellViewModel _shell = new();
    private bool _paneOpen;
    private bool _suppressNavEvent;

    public MainWindow()
    {
        InitializeComponent();

        TrySetMicaBackdrop();
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleDrag);

        PaneVersionText.Text = "书源聚合下载器 " + AppVersionText();

        // 默认落在搜索页。
        SelectNavItem("search");
        NavigateTo("search");

        // 全局日志面板: 绑定到 MainVM.LogBuffer, 自动滚到底。
        if (App.MainVM is { } mvm)
        {
            GlobalLogList.ItemsSource = mvm.LogBuffer;
            mvm.LogBuffer.CollectionChanged += OnGlobalLogChanged;
        }

        // 内容加载完成后再弹首启引导 (此时 XamlRoot 已可用)。
        ContentFrame.Loaded += OnContentFrameLoaded;
    }

    private static string AppVersionText()
    {
        var v = Assembly.GetExecutingAssembly().GetName().Version;
        return v is null ? "v?" : $"v{v.Major}.{v.Minor}";
    }

    private void TrySetMicaBackdrop()
    {
        try
        {
            if (MicaController.IsSupported())
            {
                SystemBackdrop = new MicaBackdrop { Kind = MicaKind.BaseAlt };
                // Mica 下让根 Grid 透明, 露出材质
                RootGrid.Background = new SolidColorBrush(Colors.Transparent);
            }
        }
        catch
        {
            // 不支持 Mica 时保持主题纯色底, 不致命。
        }
    }

    private void OnContentFrameLoaded(object sender, RoutedEventArgs e)
    {
        ContentFrame.Loaded -= OnContentFrameLoaded;
        MaybeShowOnboarding();
    }

    /// <summary>首次启动 (无「已见引导」标记) 用 ContentDialog 展示三连提示; 可手动关, 关后落标记。</summary>
    private void MaybeShowOnboarding()
    {
        // TeachingTip 将在后续提交中实现; 当前先用 ContentDialog 保持兼容。
        var settings = App.Settings;
        if (settings is null || settings.SeenOnboarding)
        {
            return;
        }

        try
        {
            var tips = new StackPanel { Spacing = 10 };
            tips.Children.Add(TipRow("1", "先准备书源文件", "到「书源」页点「+ 添加书源文件」, 选择自备的 Legado 格式书源 JSON, 加入后即可参与搜索。"));
            tips.Children.Add(TipRow("2", "点「校验书源」或「深度校验」", "校验后有效源会标记为绿色状态点, 失效源标红。"));
            tips.Children.Add(TipRow("3", "回「搜索」页输入书名开始下载", "搜索 → 勾选结果 → 点「下载选中」, 支持 TXT / EPUB。"));
            tips.Children.Add(new TextBlock
            {
                Text = "提示仅此一次, 之后可在「日志与设置」页随时查看关于信息。",
                FontSize = 12,
                Opacity = 0.7,
                TextWrapping = TextWrapping.Wrap,
            });

            var dialog = new ContentDialog
            {
                Title = "欢迎使用书源聚合下载器",
                Content = tips,
                PrimaryButtonText = "开始使用",
                DefaultButton = ContentDialogButton.Primary,
                XamlRoot = ContentFrame.XamlRoot,
            };
            _ = dialog.ShowAsync();
            settings.MarkOnboardingSeen();
        }
        catch
        {
            // 弹窗异常不致命: 下次启动仍会尝试引导。
        }
    }

    private static StackPanel TipRow(string num, string title, string desc)
        => new()
        {
            Spacing = 2,
            Children =
            {
                new TextBlock
                {
                    Text = $"{num}. {title}",
                    FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                    TextWrapping = TextWrapping.Wrap,
                },
                new TextBlock
                {
                    Text = desc,
                    Opacity = 0.8,
                    TextWrapping = TextWrapping.Wrap,
                },
            },
        };

    private void OnGlobalLogChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action == NotifyCollectionChangedAction.Add && GlobalLogList is { Items.Count: > 0 })
        {
            GlobalLogList.ScrollIntoView(GlobalLogList.Items[^1]);
        }
    }

    // ------------------------------------------------------------ 导航面板 --

    private void Hamburger_Click(object sender, RoutedEventArgs e)
    {
        if (_paneOpen)
        {
            ClosePane();
        }
        else
        {
            OpenPane();
        }
    }

    private void PaneMask_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e) => ClosePane();

    private void OpenPane()
    {
        if (_paneOpen)
        {
            return;
        }

        _paneOpen = true;
        PaneMask.Visibility = Visibility.Visible;
        NavPane.Visibility = Visibility.Visible;
        HamburgerButton.Visibility = Visibility.Collapsed;   // 面板顶栏 ☰ 原位接管
        AnimatePane(-300, 0, 0, 1, null);
    }

    private void ClosePane()
    {
        if (!_paneOpen)
        {
            return;
        }

        _paneOpen = false;
        PaneMask.Visibility = Visibility.Collapsed;
        HamburgerButton.Visibility = Visibility.Visible;
        AnimatePane(0, -300, 1, 0, () => NavPane.Visibility = Visibility.Collapsed);
    }

    /// <summary>面板滑入/滑出 + 淡入/淡出: TranslateTransform.X 与 Opacity 双关键帧 (200ms)。</summary>
    private void AnimatePane(double xFrom, double xTo, double oFrom, double oTo, Action? onCompleted)
    {
        var slide = new DoubleAnimationUsingKeyFrames();
        slide.KeyFrames.Add(new EasingDoubleKeyFrame { Value = xFrom, KeyTime = KeyTime.FromTimeSpan(TimeSpan.Zero) });
        slide.KeyFrames.Add(new EasingDoubleKeyFrame { Value = xTo, KeyTime = KeyTime.FromTimeSpan(TimeSpan.FromMilliseconds(200)) });
        Storyboard.SetTarget(slide, NavPaneShift);
        Storyboard.SetTargetProperty(slide, "X");

        var fade = new DoubleAnimationUsingKeyFrames();
        fade.KeyFrames.Add(new EasingDoubleKeyFrame { Value = oFrom, KeyTime = KeyTime.FromTimeSpan(TimeSpan.Zero) });
        fade.KeyFrames.Add(new EasingDoubleKeyFrame { Value = oTo, KeyTime = KeyTime.FromTimeSpan(TimeSpan.FromMilliseconds(200)) });
        Storyboard.SetTarget(fade, NavPane);
        Storyboard.SetTargetProperty(fade, "Opacity");

        var sb = new Storyboard();
        sb.Children.Add(slide);
        sb.Children.Add(fade);
        if (onCompleted is not null)
        {
            sb.Completed += (_, _) => onCompleted();
        }

        sb.Begin();
    }

    private void NavList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_suppressNavEvent)
        {
            return;
        }

        if (NavList.SelectedItem is ListBoxItem item && item.Tag is string tag)
        {
            NavigateTo(tag);
            ClosePane();
        }
    }

    /// <summary>把 NavList 选中项同步到指定 Tag (不触发导航回调)。</summary>
    private void SelectNavItem(string tag)
    {
        _suppressNavEvent = true;
        foreach (object? obj in NavList.Items)
        {
            if (obj is ListBoxItem item && item.Tag as string == tag)
            {
                NavList.SelectedItem = item;
                break;
            }
        }

        _suppressNavEvent = false;
    }

    private void NavigateTo(string tag)
    {
        Type pageType = tag switch
        {
            "search" => typeof(MainPage),
            "sources" => typeof(SourcesPage),
            "downloads" => typeof(DownloadsPage),
            "auth" => typeof(AuthPage),
            "settings" => typeof(LogSettingsPage),
            _ => typeof(MainPage),
        };

        _shell.CurrentTag = tag;
        SelectNavItem(tag);

        if (ContentFrame.CurrentSourcePageType != pageType)
        {
            ContentFrame.Navigate(pageType);
        }
    }
}
