using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Views;

/// <summary>
/// 下载页: 展示下载目录里的 TXT/EPUB (downloads.list ack), 支持刷新 / 打开目录 /
/// 双击打开文件 / 删除选中 (downloads.delete, 确认框)。数据写入全部经 EventRouter
/// 回调 DownloadsViewModel, 这里只发命令。
/// </summary>
public sealed partial class DownloadsPage : Page
{
    public DownloadsViewModel ViewModel { get; }

    public DownloadsPage()
    {
        ViewModel = App.DownloadsVM ?? new DownloadsViewModel();
        DataContext = ViewModel;
        InitializeComponent();
    }

    private BackendClient? Backend => App.Backend;

    protected override void OnNavigatedTo(Microsoft.UI.Xaml.Navigation.NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        _ = RefreshAsync("进入下载页");
    }

    private async Task RefreshAsync(string why)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.OnBackendError("downloads.list", $"后端未运行, 无法刷新 ({why})");
            return;
        }

        ViewModel.BeginList();
        await backend.DownloadsListAsync();
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e) => await RefreshAsync("点刷新");

    private void OpenDir_Click(object sender, RoutedEventArgs e)
    {
        var dir = ViewModel.Dir;
        if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir))
        {
            ViewModel.OnBackendError("downloads", "下载目录还不存在, 先下载一本书或到设置页检查数据目录。");
            return;
        }

        try
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{dir}\"") { UseShellExecute = true });
        }
        catch
        {
            ViewModel.OnBackendError("downloads", "打开下载目录失败。");
        }
    }

    private async void FilesView_DoubleTapped(object sender, DoubleTappedRoutedEventArgs e)
    {
        var backend = Backend;
        if (FilesView.SelectedItem is not DownloadBookRow row || backend is null || !backend.IsRunning)
        {
            return;
        }

        await backend.DownloadsOpenAsync(row.Path);
    }

    private async void Delete_Click(object sender, RoutedEventArgs e)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.OnBackendError("downloads.delete", "后端未运行, 无法删除。");
            return;
        }

        if (FilesView.SelectedItem is not DownloadBookRow row)
        {
            ViewModel.OnBackendError("downloads.delete", "先在列表里选中要删除的书。");
            return;
        }

        var confirm = new ContentDialog
        {
            Title = "删除这本书?",
            Content = $"「{row.File}」将从下载目录永久删除, 无法恢复。",
            PrimaryButtonText = "删除",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = XamlRoot,
        };
        if (await confirm.ShowAsync() != ContentDialogResult.Primary)
        {
            return;
        }

        await backend.DownloadsDeleteAsync(row.Path);
    }
}
