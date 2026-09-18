using System;
using System.Collections.Generic;
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

        HookMarquee();
    }

    private BackendClient? Backend => App.Backend;

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
            deepOnly: ViewModel.DeepOnly,
            group: ViewModel.SourceGroup);

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

    // ------------------------------------------------- 资源管理器式框选 (B3) --
    //
    // ListView(Extended) 原生已覆盖: 单击 / Ctrl 加选 / Shift 连选 / 在行上拖动连续选。
    // 缺的只是「空白区按下拖动 → 画绿框 → 框内行入选」。实现要点:
    //   1) ListView 类处理会把 PointerPressed 标成 handled, 普通 += 收不到,
    //      必须 AddHandler(..., handledEventsToo: true) 挂在 ResultsView 上。
    //   2) 按下点若落在任一可见行容器上 → 完全不干预, 交给原生选择。
    //   3) 空白按下不立即捕获, 移动超过 4px 才激活 (CapturePointer) 并画框,
    //      保证普通空白点击不受影响。
    //   4) 拖动中按 Ctrl = 与按下前的既有选择快照取并集; 否则绿框即全部选择。

    private Windows.Foundation.Point? _marqueeStart;  // 按下点 (MarqueeCanvas 坐标系, 与列表重合); null=未在按下态
    private bool _marqueeActive;
    private uint _marqueePointerId;
    private HashSet<object> _marqueeBase = new();     // 按下瞬间的选择快照 (Ctrl 并集基)

    private void HookMarquee()
    {
        ResultsView.AddHandler(UIElement.PointerPressedEvent,
            new PointerEventHandler(Marquee_OnPressed), true);
        ResultsView.AddHandler(UIElement.PointerMovedEvent,
            new PointerEventHandler(Marquee_OnMoved), true);
        ResultsView.AddHandler(UIElement.PointerReleasedEvent,
            new PointerEventHandler(Marquee_OnEnded), true);
        ResultsView.AddHandler(UIElement.PointerCanceledEvent,
            new PointerEventHandler(Marquee_OnEnded), true);
    }

    private void Marquee_OnPressed(object sender, PointerRoutedEventArgs e)
    {
        _marqueeStart = null;
        _marqueeActive = false;
        if (e.Pointer.PointerDeviceType != Microsoft.UI.Input.PointerDeviceType.Mouse) return;

        var rel = e.GetCurrentPoint(ResultsView);
        if (!rel.Properties.IsLeftButtonPressed) return;
        if (PressHitsVisibleItem(rel.Position)) return;   // 行上 → 交给原生

        _marqueePointerId = rel.PointerId;
        _marqueeStart = e.GetCurrentPoint(MarqueeCanvas).Position;
        _marqueeBase = ResultsView.SelectedItems.Cast<object>().ToHashSet();
    }

    private bool PressHitsVisibleItem(Windows.Foundation.Point p)
    {
        foreach (var b in VisibleItemBounds(ResultsView))
        {
            if (b.Contains(p)) return true;
        }
        return false;
    }

    /// <summary>
    /// 已实现 (realized) 行容器在指定祖先坐标系中的矩形。WinUI 3 的 ListView 没有
    /// FirstVisibleIndex/LastVisibleIndex, 只能全量遍历 —— 未实现行 ContainerFromIndex
    /// 返回 null, 直接跳过。
    /// </summary>
    private IEnumerable<Windows.Foundation.Rect> VisibleItemBounds(UIElement reference)
    {
        for (int i = 0; i < ResultsView.Items.Count; i++)
        {
            if (ResultsView.ContainerFromIndex(i) is not ListViewItem c) continue;
            yield return c.TransformToVisual(reference)
                .TransformBounds(new Windows.Foundation.Rect(0, 0, c.ActualWidth, c.ActualHeight));
        }
    }

    private static bool RectsIntersect(Windows.Foundation.Rect a, Windows.Foundation.Rect b) =>
        a.Left < b.Right && b.Left < a.Right && a.Top < b.Bottom && b.Top < a.Bottom;

    private static bool CtrlDown() =>
        Microsoft.UI.Input.InputKeyboardSource
            .GetKeyStateForCurrentThread(Windows.System.VirtualKey.Control)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);

    private void Marquee_OnMoved(object sender, PointerRoutedEventArgs e)
    {
        if (_marqueeStart is null || e.Pointer.PointerId != _marqueePointerId) return;
        var start = _marqueeStart.Value;
        var cur = e.GetCurrentPoint(MarqueeCanvas).Position;

        if (!_marqueeActive)
        {
            if (Math.Abs(cur.X - start.X) < 4 && Math.Abs(cur.Y - start.Y) < 4) return;
            _marqueeActive = true;
            ResultsView.CapturePointer(e.Pointer);
            MarqueeRect.Visibility = Visibility.Visible;
        }

        var rect = new Windows.Foundation.Rect(
            Math.Min(cur.X, start.X), Math.Min(cur.Y, start.Y),
            Math.Abs(cur.X - start.X), Math.Abs(cur.Y - start.Y));
        Canvas.SetLeft(MarqueeRect, rect.X);
        Canvas.SetTop(MarqueeRect, rect.Y);
        MarqueeRect.Width = rect.Width;
        MarqueeRect.Height = rect.Height;

        var picked = ItemsInRect(rect);
        var target = CtrlDown() ? new HashSet<object>(_marqueeBase) : new HashSet<object>();
        target.UnionWith(picked);
        if (!target.SetEquals(AppliedSelection()))
        {
            ApplySelection(target);
        }
    }

    private HashSet<object> ItemsInRect(Windows.Foundation.Rect rect)
    {
        var picked = new HashSet<object>();
        for (int i = 0; i < ResultsView.Items.Count; i++)
        {
            if (ResultsView.ContainerFromIndex(i) is not ListViewItem c || c.Content is null) continue;
            var b = c.TransformToVisual(MarqueeCanvas)
                     .TransformBounds(new Windows.Foundation.Rect(0, 0, c.ActualWidth, c.ActualHeight));
            if (RectsIntersect(rect, b)) picked.Add(c.Content);
        }
        return picked;
    }

    private HashSet<object> AppliedSelection() =>
        ResultsView.SelectedItems.Cast<object>().ToHashSet();

    private void ApplySelection(HashSet<object> target)
    {
        // 按 Items 顺序重建, 保持选中列表稳定。
        ResultsView.SelectedItems.Clear();
        foreach (var item in ResultsView.Items)
        {
            if (item is not null && target.Contains(item))
            {
                ResultsView.SelectedItems.Add(item);
            }
        }
    }

    private void Marquee_OnEnded(object sender, PointerRoutedEventArgs e)
    {
        if (e.Pointer.PointerId != _marqueePointerId) return;
        if (_marqueeActive)
        {
            ResultsView.ReleasePointerCapture(e.Pointer);
            _marqueeActive = false;
        }
        MarqueeRect.Visibility = Visibility.Collapsed;
        _marqueeStart = default;
        _marqueeBase = new HashSet<object>();
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

        var mode = ViewModel.DownloadMode ?? "万里挑一";
        var fmt = ViewModel.ExportFormat ?? "自动";
        // 输出目录以后端 hello 的 default_out 为准; 未握手时回退本地 downloads 目录。
        var outDir = string.IsNullOrEmpty(ViewModel.OutDir)
            ? System.IO.Path.Combine(AppContext.BaseDirectory, "downloads")
            : ViewModel.OutDir;

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
