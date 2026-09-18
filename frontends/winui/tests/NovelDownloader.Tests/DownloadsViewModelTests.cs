using System;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// 下载页 VM 单测: downloads.list ack 上屏 / 删除 ack 就地移除 / 错误与忙拒解锁。
/// 纯逻辑 VM, 不依赖 UI 线程 (dispatcher 为 null 时 EventRouter 同步执行)。
/// </summary>
public class DownloadsViewModelTests
{
    private static DownloadsListResult ListResult() => new()
    {
        Dir = "D:/out",
        Items = new[]
        {
            new DownloadItemDto("诡秘之主", "诡秘之主.epub", "D:/out/诡秘之主.epub", "epub", 3_500_000, 1_750_000_000),
            new DownloadItemDto("大王饶命", "大王饶命.txt", "D:/out/大王饶命.txt", "txt", 900_000, 1_749_000_000),
        },
    };

    [Fact]
    public void ApplyList_PopulatesRowsDirAndStatus()
    {
        var vm = new DownloadsViewModel();

        vm.ApplyList(ListResult());

        Assert.Equal(2, vm.Files.Count);
        Assert.Equal("D:/out", vm.Dir);
        Assert.Equal("诡秘之主", vm.Files[0].Name);
        Assert.Equal("epub", vm.Files[0].Ext);
        Assert.Equal("3.3 MB", vm.Files[0].SizeText);
        Assert.Equal("879 KB", vm.Files[1].SizeText);
        Assert.Equal("共 2 本", vm.Status);
        Assert.False(vm.IsBusy);
        Assert.False(vm.IsEmpty);
    }

    [Fact]
    public void ApplyList_Empty_FallsToEmptyStatusAndHint()
    {
        var vm = new DownloadsViewModel();

        vm.ApplyList(new DownloadsListResult { Dir = "D:/out" });

        Assert.Empty(vm.Files);
        Assert.True(vm.IsEmpty);
        Assert.Equal("下载目录里还没有书", vm.Status);
    }

    [Fact]
    public void BeginList_LocksAndFinishList_Fallbacks()
    {
        var vm = new DownloadsViewModel();

        vm.BeginList();
        Assert.True(vm.IsBusy);
        Assert.False(vm.CanEdit);

        vm.FinishList();
        Assert.False(vm.IsBusy);
        Assert.Equal("下载目录读取失败", vm.Status);
    }

    [Fact]
    public void OnDeleteAck_RemovesMatchingRow()
    {
        var vm = new DownloadsViewModel();
        vm.ApplyList(ListResult());

        vm.OnDeleteAck("D:/out/诡秘之主.epub");

        Assert.Single(vm.Files);
        Assert.Equal("大王饶命", vm.Files[0].Name);
        Assert.Contains("已删除", vm.Status);
    }

    [Fact]
    public void OnBackendError_UnlocksAndShowsReason()
    {
        var vm = new DownloadsViewModel();
        vm.BeginList();

        vm.OnBackendError("downloads.delete", "路径不在下载目录内");

        Assert.False(vm.IsBusy);
        Assert.Contains("路径不在下载目录内", vm.Status);
    }

    // ------------------------------------------------------- EventRouter 接线 --

    private static (EventRouter router, DownloadsViewModel vm) Routed()
    {
        var client = new BackendClient();
        var router = new EventRouter(client, dispatcher: null);
        var vm = new DownloadsViewModel();
        router.Attach(vm);
        return (router, vm);
    }

    [Fact]
    public void EventRouter_DownloadsListAck_FillsVm()
    {
        var (router, vm) = Routed();
        vm.BeginList();

        router.RouteRawLine("""{"type":"ack","cmd":"downloads.list","dir":"D:/out","items":[{"name":"A","file":"A.txt","path":"D:/out/A.txt","ext":"txt","size":2048,"mtime":1750000000}]}""");

        Assert.Single(vm.Files);
        Assert.Equal("A", vm.Files[0].Name);
        Assert.False(vm.IsBusy);
    }

    [Fact]
    public void EventRouter_DownloadsDeleteAck_RemovesRow()
    {
        var (router, vm) = Routed();
        vm.ApplyList(ListResult());

        router.RouteRawLine("""{"type":"ack","cmd":"downloads.delete","path":"D:/out/大王饶命.txt","deleted":true}""");

        Assert.Single(vm.Files);
        Assert.Equal("诡秘之主", vm.Files[0].Name);
    }

    [Fact]
    public void EventRouter_DownloadsError_UnlocksVm()
    {
        var (router, vm) = Routed();
        vm.BeginList();

        router.RouteRawLine("""{"type":"error","cmd":"downloads.delete","message":"路径无效"}""");

        Assert.False(vm.IsBusy);
        Assert.Contains("路径无效", vm.Status);
    }

    [Fact]
    public void EventRouter_BusyRejectionOfDownloads_UnlocksVm()
    {
        var (router, vm) = Routed();
        vm.BeginList();

        router.RouteRawLine("""{"type":"busy","cmd":"downloads.delete","running":"download"}""");

        Assert.False(vm.IsBusy);
    }
}
