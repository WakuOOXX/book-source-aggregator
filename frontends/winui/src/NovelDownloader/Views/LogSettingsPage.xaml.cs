using System;
using System.Diagnostics;
using System.IO;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Views;

/// <summary>
/// 日志与设置页: 外观区主题三选经 App.ApplyTheme 即时生效并持久化;
/// 关于区读后端 hello; 数据区接后端 data.setdir / cache.clear / data.clear
/// (破坏性操作先弹确认框列清删什么留什么)。
/// 日志区已移至 MainWindow 全局面板, 任何页面可见。
/// </summary>
public sealed partial class LogSettingsPage : Page
{
    public LogSettingsViewModel ViewModel { get; }

    public LogSettingsPage()
    {
        ViewModel = App.LogSettingsVM ?? new LogSettingsViewModel(App.Settings);
        DataContext = ViewModel;
        ViewModel.DirMigrated += OnDirMigrated;
        ViewModel.ClearFinished += OnClearFinished;
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

    // ------------------------------------------------------------- 数据区 --

    private void OpenDataDirButton_Click(object sender, RoutedEventArgs e)
    {
        var dir = ViewModel.DataDir;
        if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir))
        {
            Log("ℹ 数据目录还不存在, 先搜索或下载一次书就会建出来。");
            return;
        }

        try
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{dir}\"") { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            Log($"✘ 打开数据目录失败: {ex.Message}");
        }
    }

    private async void ChangeDataDirButton_Click(object sender, RoutedEventArgs e)
    {
        var backend = App.Backend;
        if (backend is null || !backend.IsRunning)
        {
            Log("✘ 后端未运行, 无法更改数据目录。");
            return;
        }

        if (ViewModel.IsBusy)
        {
            return;
        }

        var target = await PickFolderAsync();
        if (string.IsNullOrEmpty(target))
        {
            return;
        }

        var ok = await ConfirmAsync(
            "更改数据存储目录",
            $"新目录: {target}\n\n" +
            "书源文件、下载的书、登录头和运行记录会一起搬过去; " +
            "新目录里已有同名内容的会留在原处不覆盖。\n\n" +
            "搬家期间请等待完成提示, 不要关闭应用。",
            "开始搬家");
        if (!ok)
        {
            return;
        }

        Log($"▶ 数据目录搬家 → {target}");
        ViewModel.BeginCommand("data.setdir");
        await backend.DataSetDirAsync(target, migrate: true);
    }

    private async void ClearCacheButton_Click(object sender, RoutedEventArgs e)
        => await RunClearAsync("cache.clear",
            "清除缓存",
            "将删除: 校验产生的中间文件、校验记录、上次的选择和选项。\n" +
            "保留: 书源文件、书源勾选清单、登录头、已下载的书。\n" +
            "清除后需要重新校验书源。",
            "清除缓存");

    private async void ClearDataButton_Click(object sender, RoutedEventArgs e)
        => await RunClearAsync("data.clear",
            "清除数据",
            "将删除: 登录头、Cookie、抓取浏览器配置、全部书源文件 (内置和你导入的)、运行记录。\n" +
            "删除后书源恢复成出厂自带的那一份。\n" +
            "保留: 已下载的书。",
            "清除数据");

    /// <summary>确认 → 发清理命令的公共流程 (VM 忙态锁按钮, ack 回来解锁并汇报)。</summary>
    private async System.Threading.Tasks.Task RunClearAsync(
        string cmd, string title, string detail, string primaryText)
    {
        var backend = App.Backend;
        if (backend is null || !backend.IsRunning)
        {
            Log("✘ 后端未运行, 无法执行清理。");
            return;
        }

        if (ViewModel.IsBusy)
        {
            return;
        }

        if (!await ConfirmAsync(title, detail, primaryText))
        {
            return;
        }

        Log($"▶ {title}…");
        ViewModel.BeginCommand(cmd);
        if (cmd == "cache.clear")
        {
            await backend.CacheClearAsync();
        }
        else
        {
            await backend.DataClearAsync();
        }
    }

    // ------------------------------------------------------------- ack 回调 --

    private void OnDirMigrated(DataSetDirResult result)
    {
        if (result.Unchanged)
        {
            Log("ℹ 新目录和当前目录相同, 没有搬家。");
            return;
        }

        ViewModel.SaveDataDir(result.Paths.DataDir);
        var moved = result.Moved.Count > 0 ? string.Join("、", result.Moved) : "无";
        var kept = result.KeptOld.Count > 0
            ? $"; 新目录已有同名内容、保留在原处的: {string.Join("、", result.KeptOld)}"
            : "";
        Log($"✔ 数据目录已切换到 {result.Paths.DataDir} · 搬走: {moved}{kept}");
    }

    private void OnClearFinished(string cmd, ClearResult result)
    {
        var what = cmd == "cache.clear" ? "缓存" : "数据";
        var freed = cmd == "cache.clear" && result.FreedBytes > 0
            ? $" · 释放 {FormatSize(result.FreedBytes)}"
            : "";
        Log($"✔ 清除{what}完成: 删除 {result.Deleted.Count} 项{freed}。{result.Note}");
        foreach (var f in result.Failed)
        {
            Log($"  ✘ 未能删除: {f}");
        }
    }

    // ------------------------------------------------------------- 助手 --

    private static string FormatSize(long bytes) => bytes switch
    {
        >= 1024 * 1024 => $"{bytes / 1024.0 / 1024.0:0.#} MB",
        >= 1024 => $"{bytes / 1024.0:0.#} KB",
        _ => $"{bytes} B",
    };

    private void Log(string message) => App.MainVM?.AppendLog(message);

    /// <summary>文件夹选择器。非打包应用需先 InitializeWithWindow 绑定窗口句柄。</summary>
    private async System.Threading.Tasks.Task<string> PickFolderAsync()
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            picker.FileTypeFilter.Add("*");
            if (App.Window is not null)
            {
                var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(App.Window);
                WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            }

            var folder = await picker.PickSingleFolderAsync();
            return folder?.Path ?? "";
        }
        catch (Exception ex)
        {
            Log($"✘ 文件夹选择器不可用: {ex.Message}");
            return "";
        }
    }

    /// <summary>破坏性操作确认框: PrimaryButton=执行, 取消即放弃。</summary>
    private async System.Threading.Tasks.Task<bool> ConfirmAsync(
        string title, string detail, string primaryText)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = App.Window?.Content is FrameworkElement root ? root.XamlRoot : XamlRoot,
            Title = title,
            Content = new TextBlock { Text = detail, TextWrapping = TextWrapping.Wrap },
            PrimaryButtonText = primaryText,
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }
}
