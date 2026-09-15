using System;
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
