using System;
using System.Collections.Specialized;
using System.Linq;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using NovelDownloader.Models;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Views;

/// <summary>
/// 页面一: 搜索 (主页面)。
/// M1: 绑定 BackendClient (经 App.Backend) 与共享 MainViewModel (经 App.MainVM)。
/// 事件 → UI 由 Services/EventRouter 在 UI 线程上驱动 VM, 本页只做交互入口与选择同步。
/// M2: 下载选中按钮 → send download 命令。
/// </summary>
public sealed partial class MainPage : Page
{
    public MainViewModel ViewModel { get; }

    public MainPage()
    {
        ViewModel = App.MainVM ?? new MainViewModel();
        DataContext = ViewModel;
        InitializeComponent();

        // Esc = 取消框选/清空选择 (沿用旧习惯)。
        KeyDown += OnPageKeyDown;

        // 日志自动滚到底 (新行才有意义)。
        ViewModel.LogBuffer.CollectionChanged += OnLogChanged;
    }

    private BackendClient? Backend => App.Backend;

    private void OnLogChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action == NotifyCollectionChangedAction.Add && ViewModel.LogBuffer.Count > 0)
        {
            LogList.ScrollIntoView(ViewModel.LogBuffer[^1]);
        }
    }

    // ----------------------------------------------------------- 搜索入口 --

    private void SearchBox_QuerySubmitted(AutoSuggestBox sender,
                                          AutoSuggestBoxQuerySubmittedEventArgs args)
    {
        _ = StartSearchAsync();
    }

    private void SearchButton_Click(object sender, RoutedEventArgs e)
    {
        // 搜索中 → 「停止」; 空闲 → 开始搜索。
        if (ViewModel.IsSearching)
        {
            _ = StopSearchAsync();
        }
        else
        {
            _ = StartSearchAsync();
        }
    }

    private async System.Threading.Tasks.Task StartSearchAsync()
    {
        var keyword = (SearchBox.Text ?? string.Empty).Trim();
        if (keyword.Length == 0)
        {
            ViewModel.AppendLog("请输入搜索关键词。");
            return;
        }

        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法搜索。");
            return;
        }

        ViewModel.BeginSearch(keyword);
        SearchBox.Text = keyword;

        await backend.SearchAsync(
            keyword,
            fuzzy: ViewModel.Fuzzy,
            rel: ViewModel.RelatedOnly,
            domain: ViewModel.Domain,
            deepOnly: ViewModel.DeepOnly);

        // 后续进度/命中/结算全部由 EventRouter 的 hit / sprog / sres 事件驱动。
    }

    private async System.Threading.Tasks.Task StopSearchAsync()
    {
        var backend = Backend;
        if (backend is null)
        {
            return;
        }

        ViewModel.AppendLog("⌨ 已请求停止搜索…");
        await backend.StopAsync();
    }

    // ----------------------------------------------------------- 选择操作 --

    private void SelectAll_Click(object sender, RoutedEventArgs e)
    {
        ResultsView.SelectedItems.Clear();
        foreach (var item in ResultsView.Items)
        {
            ResultsView.SelectedItems.Add(item);
        }
    }

    private void InvertSelection_Click(object sender, RoutedEventArgs e)
    {
        var current = ResultsView.SelectedItems.Cast<object>().ToHashSet();
        ResultsView.SelectedItems.Clear();
        foreach (var item in ResultsView.Items)
        {
            if (!current.Contains(item))
            {
                ResultsView.SelectedItems.Add(item);
            }
        }
    }

    private void ClearSelection_Click(object sender, RoutedEventArgs e) => ResultsView.SelectedItems.Clear();

    private void ResultsView_SelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        ViewModel.SelectedCount = ResultsView.SelectedItems.Count;
    }

    private void OnPageKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == Windows.System.VirtualKey.Escape)
        {
            ResultsView.SelectedItems.Clear();
            e.Handled = true;
        }
    }

    // ------------------------------------------------------------- 下载 (M2) --

    private void Download_Click(object sender, RoutedEventArgs e)
    {
        if (ViewModel.IsDownloading)
        {
            // 停止下载
            _ = StopDownloadAsync();
            return;
        }

        var selectedHits = ResultsView.SelectedItems
            .Cast<HitItem>()
            .ToList();

        if (selectedHits.Count == 0)
        {
            ViewModel.AppendLog("请先选中要下载的书目。");
            return;
        }

        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法下载。");
            return;
        }

        var mode = ViewModel.DownloadMode ?? "单一";
        var fmt = ViewModel.ExportFormat ?? "TXT";
        var outDir = System.IO.Path.Combine(AppContext.BaseDirectory, "downloads");

        ViewModel.BeginDownload(selectedHits.Count, mode, fmt);
        _ = backend.DownloadAsync(selectedHits, mode, fmt, outDir);
    }

    private async System.Threading.Tasks.Task StopDownloadAsync()
    {
        var backend = Backend;
        if (backend is null)
        {
            return;
        }

        ViewModel.AppendLog("⌨ 已请求停止下载…");
        await backend.StopAsync();
    }
}
