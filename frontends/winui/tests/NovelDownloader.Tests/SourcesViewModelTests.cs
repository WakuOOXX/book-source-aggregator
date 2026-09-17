using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// 书源页 VM (M3) 状态机单测: sources.list 上屏、vfile→vprog→vdone 推进、
/// busy/进程退出时 AbortVerify 回空闲、校验事件经 EventRouter 路由。
/// 不涉及 UI 线程 (dispatcher 为 null 时 EventRouter 同步执行)。
/// </summary>
public class SourcesViewModelTests
{
    // ------------------------------------------------------------ 工具方法 --

    private static string SourcesListAckLine() =>
        """{"type":"ack","cmd":"sources.list","files":[{"name":"a.json","checked":true,"exists":true},{"name":"b.json","checked":false,"exists":false}],"checked":["a.json"],"dir":"E:\\book\\bookdl\\shuyuan"}""";

    private static string VFileLine(int idx, int count, string name) =>
        $$$"""{"type":"event","kind":"vfile","payload":[{{{idx}}},{{{count}}},"{{{name}}}"]}""";

    private static string VProgLine(int idx, int count, string name, int done, int total, int ok, int bad) =>
        $$$"""{"type":"event","kind":"vprog","payload":[{{{idx}}},{{{count}}},"{{{name}}}",{{{done}}},{{{total}}},{{{ok}}},{{{bad}}}]}""";

    private static string VFileDoneLine(string name, int ok, int bad, double elapsed) =>
        $$$"""{"type":"event","kind":"vfile_done","payload":["{{{name}}}","shuyuan",{{{ok}}},{{{bad}}},{{{elapsed}}}]}""";

    private static string VDoneLine(int files, int ok, int bad, double elapsed, bool aborted) =>
        $$$"""{"type":"event","kind":"vdone","payload":[{{{files}}},{{{ok}}},{{{bad}}},{{{elapsed}}},{{{(aborted ? "true" : "false")}}}]}""";

    // ------------------------------------------------------------ sources.list --

    [Fact]
    public void ApplySourcesList_PopulatesFileListWithCheckedAndExists()
    {
        var vm = new SourcesViewModel();

        vm.ApplySourcesList(new SourcesListResult
        {
            Files = new[]
            {
                new SourceFileDto("a.json", true, true),
                new SourceFileDto("b.json", false, false),
            },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });

        Assert.True(vm.IsListLoaded);
        Assert.Equal(2, vm.SourceFiles.Count);
        Assert.Equal("a.json", vm.SourceFiles[0].Name);
        Assert.True(vm.SourceFiles[0].IsChecked);
        Assert.Equal("清单内", vm.SourceFiles[0].CheckedText);
        Assert.Equal("文件缺失", vm.SourceFiles[1].StatusText);
        Assert.Equal(2, vm.SourceFiles.Count);
    }

    [Fact]
    public void FinishListLoad_UnlocksListState()
    {
        var vm = new SourcesViewModel();
        Assert.False(vm.IsListLoaded);

        vm.FinishListLoad();

        Assert.True(vm.IsListLoaded);
    }

    // ------------------------------------------------------------ 校验状态机 --

    [Fact]
    public void BeginVerify_EntersVerifyingStateAndResetsProgress()
    {
        var vm = new SourcesViewModel();
        vm.ApplySourcesList(new SourcesListResult
        {
            Files = new[] { new SourceFileDto("a.json", true, true) },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });
        vm.SourceFiles[0].GoodCount = 99;

        vm.BeginVerify(deep: false);

        Assert.True(vm.IsVerifying);
        Assert.Equal("停止", vm.VerifyButtonText);
        Assert.Equal(0, vm.SourceFiles[0].GoodCount);
        Assert.Equal(VerifyState.Verifying, vm.SourceFiles[0].State);
    }

    [Fact]
    public void VFileThenVProgThenVDone_DrivesProgressAndSettlesState()
    {
        var vm = new SourcesViewModel();
        vm.ApplySourcesList(new SourcesListResult
        {
            Files = new[] { new SourceFileDto("a.json", true, true) },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });
        vm.BeginVerify(deep: false);

        vm.ApplyEvent(new VerifyFileEvent(1, 1, "a.json"));
        Assert.True(vm.IsVerifying);
        Assert.Contains("文件 1/1", vm.VerifyStatus);

        vm.ApplyEvent(new VerifyProgressEvent(1, 1, "a.json", 10, 25, 8, 2));
        Assert.Contains("探测 10/25", vm.VerifyStatus);
        Assert.Contains("有效 8", vm.VerifyStatus);
        Assert.Contains("失效 2", vm.VerifyStatus);
        Assert.Equal(25, vm.SourceFiles[0].SourceCount);
        Assert.Equal(VerifyState.Verifying, vm.SourceFiles[0].State);

        vm.ApplyEvent(new VerifyFileDoneEvent("a.json", "shuyuan", 8, 2, 3.5));
        Assert.Equal(VerifyState.Done, vm.SourceFiles[0].State);
        Assert.Equal(8, vm.SourceFiles[0].GoodCount);
        Assert.Equal(2, vm.SourceFiles[0].BadCount);
        Assert.Single(vm.VerifyFiles);
        Assert.Equal("完成", vm.VerifyFiles[0].Status);

        vm.ApplyEvent(new VerifyDoneEvent(1, 8, 2, 3.5, false));
        Assert.False(vm.IsVerifying);
        Assert.Contains("校验结束", vm.VerifyStatus);
        Assert.Contains("有效 8", vm.VerifyStatus);
    }

    [Fact]
    public void VDeep_PhaseShowsInStatus()
    {
        var vm = new SourcesViewModel();
        vm.ApplySourcesList(new SourcesListResult
        {
            Files = new[] { new SourceFileDto("a.json", true, true) },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });
        vm.BeginVerify(deep: true);

        vm.ApplyEvent(new VerifyDeepEvent(1, 2, "a.json", "试搜", 3, 8));

        Assert.True(vm.IsVerifying);
        Assert.Contains("深度校验", vm.VerifyStatus);
        Assert.Contains("试搜", vm.VerifyStatus);
        Assert.Contains("3/8", vm.VerifyStatus);
    }

    [Fact]
    public void AbortVerify_UnlocksVerifyingState()
    {
        var vm = new SourcesViewModel();
        vm.ApplySourcesList(new SourcesListResult
        {
            Files = new[] { new SourceFileDto("a.json", true, true) },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });
        vm.BeginVerify(deep: false);

        vm.AbortVerify("后端忙 (正在跑 search)");

        Assert.False(vm.IsVerifying);
        Assert.Equal("校验书源", vm.VerifyButtonText);
        Assert.Equal("后端忙 (正在跑 search)", vm.VerifyStatus);
        Assert.Equal(VerifyState.Aborted, vm.SourceFiles[0].State);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("后端忙"));
    }

    [Fact]
    public void VDoneAborted_ShowsStoppedSummary()
    {
        var vm = new SourcesViewModel();
        vm.ApplySourcesList(new SourcesListResult
        {
            Files = new[] { new SourceFileDto("a.json", true, true) },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });
        vm.BeginVerify(deep: false);

        vm.ApplyEvent(new VerifyDoneEvent(1, 4, 1, 2.0, true));

        Assert.False(vm.IsVerifying);
        Assert.Contains("校验已停止", vm.VerifyStatus);
        Assert.Equal(VerifyState.Aborted, vm.SourceFiles[0].State);
    }

    // ------------------------------------------------------- EventRouter 集成 --

    [Fact]
    public void EventRouter_SourcesListAck_RoutesToSourcesViewModel()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var svm = new SourcesViewModel();
        router.Attach(svm);

        router.RouteRawLine(SourcesListAckLine());

        Assert.True(svm.IsListLoaded);
        Assert.Equal(2, svm.SourceFiles.Count);
        Assert.Equal("a.json", svm.SourceFiles[0].Name);
    }

    [Fact]
    public void EventRouter_VerifyEventStream_DrivesSourcesViewModelState()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var svm = new SourcesViewModel();
        router.Attach(svm);

        router.RouteRawLine(SourcesListAckLine());
        svm.BeginVerify(deep: false);

        router.RouteRawLine(VFileLine(1, 2, "a.json"));
        router.RouteRawLine(VProgLine(1, 2, "a.json", 10, 25, 8, 2));
        router.RouteRawLine(VFileDoneLine("a.json", 8, 2, 3.5));
        router.RouteRawLine(VDoneLine(2, 10, 5, 12.0, false));

        Assert.False(svm.IsVerifying);
        Assert.Contains("校验结束", svm.VerifyStatus);
        Assert.Single(svm.VerifyFiles);
        Assert.Equal("完成", svm.VerifyFiles[0].Status);

        var a = svm.SourceFiles.Single(x => x.Name == "a.json");
        Assert.Equal(VerifyState.Done, a.State);
        Assert.Equal(8, a.GoodCount);
        Assert.Equal(2, a.BadCount);
    }

    [Fact]
    public void EventRouter_BusyVerify_UnlocksSourcesViewModel()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var svm = new SourcesViewModel();
        router.Attach(svm);
        svm.ApplySourcesList(new SourcesListResult
        {
            Files = new[] { new SourceFileDto("a.json", true, true) },
            Checked = new[] { "a.json" },
            Dir = "E:\\book\\bookdl\\shuyuan",
        });
        svm.BeginVerify(deep: false);

        router.RouteRawLine("""{"type":"busy","cmd":"verify","running":"search"}""");

        Assert.False(svm.IsVerifying);
        Assert.Contains(svm.LogSnapshot(), l => l.Contains("后端忙"));
    }

    [Fact]
    public void AppendLogs_BatchMirrorsToSourcesLogBuffer()
    {
        var vm = new SourcesViewModel();

        vm.AppendLogs(new[] { "行1", "", "行3" });

        Assert.Equal(2, vm.LogBuffer.Count);
        Assert.Equal("行1", vm.LogBuffer[0]);
        Assert.Equal("行3", vm.LogBuffer[^1]);
    }

    [Fact]
    public void AppendLog_RespectsCapacity()
    {
        var vm = new SourcesViewModel();
        for (var i = 0; i < SourcesViewModel.LogCapacity + 30; i++)
        {
            vm.AppendLog($"line {i}");
        }

        Assert.Equal(SourcesViewModel.LogCapacity, vm.LogBuffer.Count);
        Assert.Equal($"line {SourcesViewModel.LogCapacity + 29}", vm.LogBuffer[^1]);
    }
}
