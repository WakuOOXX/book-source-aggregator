using System;
using System.Collections.Generic;
using System.Collections.Specialized;
using System.Linq;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Views;

/// <summary>
/// 页面三: 登录头管理 (M4, 旧 Toplevel 的完整重生)。数据全部绑定 App.AuthVM
/// (共享实例 —— 批量抓取进行中切页不中断)。
///
/// 命令流: auth.list 上屏源列表; 选中后详情编辑 →「保存到所选」auth.save /
/// 「删除所选」auth.remove(=auth.save 空载荷);「浏览器登录抓取」两段式
/// auth.fetch (url → grab) 单站抓取。批量控制台以两段式 API 串行编排:
///   自动 = 每站 launch→(launched ack)→立即 grab→(captured ack)→保存→下一站;
///   手动 = 每站 launch→用户点「抓取本站」或「跳过该站」。
/// 批量推进由 VM.FetchAck 事件驱动 (router → VM.ApplyFetchAck → 本页 OnFetchAck)。
/// </summary>
public sealed partial class AuthPage : Page
{
    private readonly List<string> _pendingSelect = new();

    public AuthViewModel ViewModel { get; }

    public AuthPage()
    {
        ViewModel = App.AuthVM ?? new AuthViewModel();
        DataContext = ViewModel;
        InitializeComponent();

        ViewModel.LogBuffer.CollectionChanged += OnLogChanged;
        ViewModel.FilterChanging += OnFilterChanging;
        ViewModel.FilterChanged += OnFilterChanged;
        ViewModel.ListRefreshed += OnListRefreshed;
        ViewModel.FetchAck += OnFetchAck;
    }

    private BackendClient? Backend => App.Backend;

    protected override void OnNavigatedTo(Microsoft.UI.Xaml.Navigation.NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        _ = RefreshAuthListAsync("进入登录头页");
    }

    // ------------------------------------------------------------ 选择/过滤 --

    private void AuthSourceList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        ViewModel.SetSelection(AuthSourceList.SelectedItems.Cast<AuthEntry>());
    }

    private void SelectAll_Click(object sender, RoutedEventArgs e)
    {
        AuthSourceList.SelectedItems.Clear();
        foreach (var item in AuthSourceList.Items)
        {
            AuthSourceList.SelectedItems.Add(item);
        }
    }

    private void ClearSelection_Click(object sender, RoutedEventArgs e)
    {
        AuthSourceList.SelectedItems.Clear();
    }

    /// <summary>过滤重建前: 记录当前选中, 重建后按 URL 重选 (过滤不丢选择)。</summary>
    private void OnFilterChanging()
    {
        TraceLog.Write("OnFilterChanging");
        _pendingSelect.Clear();
        _pendingSelect.AddRange(AuthSourceList.SelectedItems.Cast<AuthEntry>().Select(x => x.Url));
    }

    private void OnFilterChanged()
    {
        TraceLog.Write("OnFilterChanged");
        ReapplySelection(_pendingSelect);
    }

    /// <summary>auth.list 上屏完成: 重选 (保存/删除/进入页面后刷新列表)。</summary>
    private void OnListRefreshed()
    {
        TraceLog.Write("OnListRefreshed");
        ReapplySelection(_pendingSelect);
        _pendingSelect.Clear();
    }

    private void ReapplySelection(IReadOnlyList<string> urls)
    {
        TraceLog.Write("ReapplySelection start n=" + urls.Count);
        var set = new HashSet<string>(urls);
        AuthSourceList.SelectedItems.Clear();
        foreach (var item in AuthSourceList.Items)
        {
            if (item is AuthEntry e && set.Contains(e.Url))
            {
                AuthSourceList.SelectedItems.Add(item);
            }
        }

        TraceLog.Write("ReapplySelection done");
    }

    // ------------------------------------------------------------ 详情操作 --

    private async void SaveButton_Click(object sender, RoutedEventArgs e)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法保存。");
            return;
        }

        var payload = ViewModel.PrepareSave();
        if (payload is null)
        {
            return;
        }

        ViewModel.AppendLog($"⌨ 保存到所选: {payload.Urls.Count} 个源"
                            + (string.IsNullOrWhiteSpace(payload.Cookie) && string.IsNullOrWhiteSpace(payload.HeaderJson)
                                ? " (cookie/header 都空 = 移除所选配置)"
                                : ""));
        ViewModel.ApplySavedPayload(payload);
        await backend.AuthSaveAsync(payload.Urls, payload.Cookie, payload.HeaderJson);
        await RefreshAuthListAsync("保存后");
    }

    private async void DeleteButton_Click(object sender, RoutedEventArgs e)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法删除。");
            return;
        }

        var payload = ViewModel.PrepareRemove();
        if (payload is null)
        {
            return;
        }

        ViewModel.AppendLog($"⌨ 删除所选: {payload.Urls.Count} 个源 (仅移除登录头配置, 不删书源)");
        ViewModel.ApplySavedPayload(payload);
        await backend.AuthRemoveAsync(payload.Urls);
        await RefreshAuthListAsync("删除后");
    }

    /// <summary>「浏览器登录抓取」两段式: idle→launch(打开浏览器) / launched→grab(抓取)。</summary>
    private async void FetchButton_Click(object sender, RoutedEventArgs e)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法抓取。");
            return;
        }

        if (ViewModel.IsFetchLaunched)
        {
            ViewModel.BeginFetchGrab();
            await backend.AuthFetchGrabAsync(ViewModel.CurrentGrabHost);
        }
        else
        {
            var url = ViewModel.PrepareFetchLaunch();
            if (url is null)
            {
                return;
            }

            ViewModel.AppendLog($"⌨ 浏览器登录抓取: {url}");
            await backend.AuthFetchStartAsync(url);
        }
    }

    // ------------------------------------------------------------ 批量抓取 --

    private async void AutoGrab_Click(object sender, RoutedEventArgs e)
        => await StartBatchAsync(auto: true);

    private async void ManualGrab_Click(object sender, RoutedEventArgs e)
        => await StartBatchAsync(auto: false);

    private async System.Threading.Tasks.Task StartBatchAsync(bool auto)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog("✘ 后端未运行, 无法批量抓取。");
            return;
        }

        if (ViewModel.IsBatchRunning)
        {
            ViewModel.AppendLog("✘ 批量已在运行中, 先点「结束批量」。");
            return;
        }

        var urls = ViewModel.SelectedUrls.ToList();
        if (urls.Count == 0)
        {
            urls = ViewModel.FilteredSources.Select(s => s.Url).ToList();
            if (urls.Count == 0)
            {
                ViewModel.AppendLog("✘ 源列表为空, 无法批量抓取。");
                return;
            }

            ViewModel.AppendLog($"批量: 未选中任何源, 按全部 {urls.Count} 个源按站点去重抓取。");
        }

        var result = ViewModel.BuildBatchTargets(urls, ViewModel.SkipConfigured);
        if (result.TotalHosts == 0)
        {
            ViewModel.AppendLog("✘ 选中的源没有有效网址。");
            return;
        }

        if (result.Hosts.Count == 0)
        {
            ViewModel.AppendLog("✘ 选中的站点全都配过 Cookie(勾选了「跳过已配站」), 可取消勾选后重试。");
            return;
        }

        if (result.Skipped > 0)
        {
            ViewModel.AppendLog($"批量: 已跳过 {result.Skipped}/{result.TotalHosts} 个配过 Cookie 的站点。");
        }

        ViewModel.BeginBatch(auto, result.Hosts, result.Targets, ViewModel.ParaTabs);
        await LaunchHostAsync(ViewModel.NextSite());
    }

    /// <summary>打开下一站的抓取浏览器 (auth.fetch 两段式第一步)。</summary>
    private async System.Threading.Tasks.Task LaunchHostAsync(string? host)
    {
        if (string.IsNullOrEmpty(host))
        {
            return;
        }

        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            return;
        }

        var url = ViewModel.TargetUrlsFor(host).FirstOrDefault();
        if (string.IsNullOrEmpty(url))
        {
            ViewModel.SkipSite();   // 没有可打开的 URL, 直接推进到下一站
            return;
        }

        await backend.AuthFetchStartAsync(url);
    }

    private async void GrabCurrent_Click(object sender, RoutedEventArgs e)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            return;
        }

        var host = ViewModel.CurrentBatchHost;
        if (string.IsNullOrEmpty(host))
        {
            return;
        }

        ViewModel.AppendLog($"批量: 正在抓取 {host} 的 Cookie…");
        await backend.AuthFetchGrabAsync(host);
    }

    private async void SkipSite_Click(object sender, RoutedEventArgs e)
    {
        var next = ViewModel.SkipSite();
        if (next is not null)
        {
            await LaunchHostAsync(next);
        }
    }

    private void PauseButton_Click(object sender, RoutedEventArgs e)
        => ViewModel.TogglePause();

    private void StopBatch_Click(object sender, RoutedEventArgs e)
        => ViewModel.StopBatch();

    /// <summary>
    /// VM.FetchAck → 批量推进驱动: captured 保存该站全部源并推下一站;
    /// 自动模式 launched 即抓 (后端无三段式, 以两段式近似)。单站抓取仅记日志。
    /// </summary>
    private async void OnFetchAck(AuthFetchAckInfo info)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            return;
        }

        if (ViewModel.IsBatchRunning)
        {
            if (info.Phase == "captured")
            {
                if (info.Cookie.Length > 0)
                {
                    var urls = ViewModel.TargetUrlsFor(info.Host).ToArray();
                    if (urls.Length > 0)
                    {
                        await backend.AuthSaveAsync(urls, info.Cookie);
                    }
                }

                await LaunchHostAsync(ViewModel.NextSite());
            }
            else if (info.Phase == "launched" && ViewModel.IsBatchAuto)
            {
                await backend.AuthFetchGrabAsync(ViewModel.CurrentBatchHost);
            }

            return;
        }

        if (info.Phase == "captured" && info.Cookie.Length > 0)
        {
            ViewModel.AppendLog($"⌨ 已抓取 {info.Host} 的 Cookie → 点「保存到所选」写入。");
        }
    }

    // ------------------------------------------------------------ 日志 --

    private void OnLogChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action == NotifyCollectionChangedAction.Add && ViewModel.LogBuffer.Count > 0)
        {
            AuthLogList.ScrollIntoView(ViewModel.LogBuffer[^1]);
        }
    }

    /// <summary>重新拉取 auth.list (进入页面 / 保存 / 删除后)。</summary>
    private async System.Threading.Tasks.Task RefreshAuthListAsync(string why)
    {
        var backend = Backend;
        if (backend is null || !backend.IsRunning)
        {
            ViewModel.AppendLog($"✘ 后端未运行, 无法刷新源列表 ({why})。");
            return;
        }

        // 刷新前记下当前选中 (列表重建后按 URL 重选, 不丢选择)。
        OnFilterChanging();
        await backend.AuthListAsync();
    }
}
