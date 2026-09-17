using System;
using System.Collections.Specialized;
using Microsoft.UI;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using NovelDownloader.Views;

namespace NovelDownloader;

/// <summary>
/// 主窗口壳: 自定义 TitleBar (内容延伸到标题栏) + NavigationView(LeftCompact) + 右侧 Frame。
/// Mica 背景在 Win11 生效, Win10 1809+ 自动退化为主题纯色 (#f3f6fa / #202020)。
/// 全局日志面板固定在 Frame 下方, 任何页面切换时可见。
/// 首启引导 (SettingsStore.SeenOnboarding 标记) 用 TeachingTip 展示三连提示。
/// </summary>
public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        TrySetMicaBackdrop();
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);

        // 默认落在搜索页
        NavView.SelectedItem = NavView.MenuItems[0];
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
            tips.Children.Add(TipRow("1", "把书源文件放进书源目录 (shuyuan/)", "前往「书源」页可查看 / 添加书源文件并移入清单。"));
            tips.Children.Add(TipRow("2", "点「校验书源」或「深度校验」", "校验后有效源会标记为绿色状态点, 失效源标红。"));
            tips.Children.Add(TipRow("3", "回「搜索」页输入书名开始下载", "搜索 → 勾选结果 → 点「下载选中」, 支持 TXT / EPUB。"));
            tips.Children.Add(new TextBlock
            {
                Text = "提示仅此一次, 之后可在「设置」页随时查看关于信息。",
                FontSize = 12,
                Opacity = 0.7,
                TextWrapping = TextWrapping.Wrap,
            });

            var dialog = new ContentDialog
            {
                Title = "欢迎使用小说下载器",
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

    private void NavView_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is NavigationViewItem item && item.Tag is string tag)
        {
            NavigateTo(tag);
        }
    }

    private void NavigateTo(string tag)
    {
        Type pageType = tag switch
        {
            "search" => typeof(MainPage),
            "sources" => typeof(SourcesPage),
            "auth" => typeof(AuthPage),
            "log" => typeof(LogSettingsPage),
            "settings" => typeof(LogSettingsPage),
            _ => typeof(MainPage),
        };

        if (ContentFrame.CurrentSourcePageType != pageType)
        {
            ContentFrame.Navigate(pageType);
        }
    }
}
