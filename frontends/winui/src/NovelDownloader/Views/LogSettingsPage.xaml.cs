using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using NovelDownloader.Services;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Views;

/// <summary>
/// 设置页: 外观区主题三选经 App.ApplyTheme 即时生效并持久化; 关于区读后端 hello;
/// 数据区按钮为占位 (仅 AppendLog, 不接破坏性逻辑)。
/// 日志区已移至 MainWindow 全局面板, 任何页面可见。
/// </summary>
public sealed partial class LogSettingsPage : Page
{
    public LogSettingsViewModel ViewModel { get; }

    public LogSettingsPage()
    {
        ViewModel = App.LogSettingsVM ?? new LogSettingsViewModel(App.Settings);
        DataContext = ViewModel;
        InitializeComponent();
    }

    protected override void OnNavigatedTo(Microsoft.UI.Xaml.Navigation.NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);

        // 主题 RadioButton 初值 = 当前记忆主题 (Checked 处理器在初值设置时同步执行, 无害)。
        switch (ViewModel.Theme)
        {
            case ThemeMode.Light:
                ThemeLightRadio.IsChecked = true;
                break;
            case ThemeMode.Dark:
                ThemeDarkRadio.IsChecked = true;
                break;
            default:
                ThemeSystemRadio.IsChecked = true;
                break;
        }
    }

    // ------------------------------------------------------------- 主题 --

    /// <summary>任一主题 RadioButton 选中时触发 (真实点击与程序化 IsChecked=true 都会走这里)。</summary>
    private void ThemeRadio_Checked(object sender, RoutedEventArgs e)
    {
        if (sender is not RadioButton { IsChecked: true } rb || rb.Tag is not string tag)
        {
            return;
        }

        var mode = tag switch
        {
            "Light" => ThemeMode.Light,
            "Dark" => ThemeMode.Dark,
            _ => ThemeMode.System,
        };

        ViewModel.SetTheme(mode);
        App.ApplyTheme(mode switch
        {
            ThemeMode.Light => ElementTheme.Light,
            ThemeMode.Dark => ElementTheme.Dark,
            _ => ElementTheme.Default,
        });
    }

    // ------------------------------------------------------------- 数据区 (M5 占位) --

    private void OpenDataDirButton_Click(object sender, RoutedEventArgs e)
        => App.MainVM?.AppendLog("ℹ 「打开数据目录」在 M5 为占位, 未接真实目录跳转。");

    private void ClearCacheButton_Click(object sender, RoutedEventArgs e)
        => App.MainVM?.AppendLog("ℹ 「清除缓存」在 M5 为占位, 未接真实清理逻辑 (避免误删数据)。");
}
