using System;
using System.Collections.Specialized;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Views;

/// <summary>
/// 页面二: 书源管理 (M3)。数据全部绑定 App.SourcesVM (共享实例 —— 切页不中断校验)。
/// sources.list 上屏文件清单; 校验/深度校验按钮发 verify 命令, 进度经
/// EventRouter → SourcesViewModel.ApplyEvent 实时回流 (vfile/vprog/vdeep/vdone)。
/// </summary>
public sealed partial class SourcesPage : Page
{
    public SourcesViewModel ViewModel { get; }

    public SourcesPage()
    {
        ViewModel = App.SourcesVM ?? new SourcesViewModel();
        DataContext = ViewModel;
        InitializeComponent();
    }

    private BackendClient? Backend => App.Backend;

    protected override void OnNavigatedTo(Microsoft.UI.Xaml.Navigation.NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        _ = RefreshSourcesListAsync("进入书源页");
    }

    // ------------------------------------------------------- 校验入口 --

    private async void VerifyButton_Click(object sender, RoutedEventArgs e)
    {
        if (ViewModel.IsVerifying)
        {
            await StopVerifyAsync();
        }
        else
        {
            await StartVerifyAsync(deep: false);
        }
    }

    private async void DeepVerifyButton_Click(object sender, RoutedEventArgs e)
    {
        if (ViewModel.IsVerifying)
        {
            await StopVerifyAsync();
        }
        else
        {
            await StartVerifyAsync(deep: true);
        }
    }

    private async System.Threading.Tasks.Task StartVerifyAsync(bool deep)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法校验。");
            return;
        }

        if (ViewModel.SourceFiles.Count == 0)
        {
            ViewModel.AppendLog("✘ 书源清单还没加载好, 无法校验。");
            return;
        }

        ViewModel.BeginVerify(deep);
        await backend.VerifyAsync(deep: deep);

        // 后续 vfile/vprog/vfile_done/vdeep/vdone 由 EventRouter 驱动 VM 上屏。
    }

    private async System.Threading.Tasks.Task StopVerifyAsync()
    {
        var backend = Backend;
        if (backend is null)
        {
            return;
        }

        ViewModel.AppendLog("⌨ 已请求停止校验…");
        await backend.StopAsync();
    }

    // ------------------------------------------------------- 清单操作 --

    private async void RemoveFile_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: SourceFileItem item })
        {
            var backend = Backend;
            if (backend is null || !backend.IsRunning)
            {
                ViewModel.AppendLog("✘ 后端未运行, 无法移出清单。");
                return;
            }

            ViewModel.AppendLog($"⌨ 移出清单: {item.Name}");
            await backend.SourcesRemoveAsync(item.Name);
            await RefreshSourcesListAsync("移出清单后");
        }
    }

    private async void AddSources_Click(object sender, RoutedEventArgs e)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法添加书源文件。");
            return;
        }

        var paths = await PickSourceFilesAsync();
        if (paths.Count == 0)
        {
            return;
        }

        ViewModel.AppendLog($"⌨ 添加书源文件: {string.Join(" · ", paths)}");
        await backend.SourcesAddAsync(paths);
        await RefreshSourcesListAsync("添加后");
    }

    /// <summary>文件选择器 (多选 .json)。非打包应用需先 InitializeWithWindow 绑定窗口句柄。</summary>
    private async System.Threading.Tasks.Task<IReadOnlyList<string>> PickSourceFilesAsync()
    {
        var result = new System.Collections.Generic.List<string>();
        try
        {
            var picker = new Windows.Storage.Pickers.FileOpenPicker();
            picker.FileTypeFilter.Add(".json");
            if (App.Window is not null)
            {
                var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(App.Window);
                WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            }

            var files = await picker.PickMultipleFilesAsync();
            if (files is not null)
            {
                foreach (var f in files)
                {
                    result.Add(f.Path);
                }
            }
        }
        catch (Exception ex)
        {
            ViewModel.AppendLog($"✘ 文件选择器不可用: {ex.Message}");
        }

        return result;
    }

    /// <summary>重新拉取 sources.list (进入页面 / 移出 / 添加后)。</summary>
    private async System.Threading.Tasks.Task RefreshSourcesListAsync(string why)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog($"✘ 后端未运行, 无法刷新书源清单 ({why})。");
            return;
        }

        await backend.SourcesListAsync();
    }
}
