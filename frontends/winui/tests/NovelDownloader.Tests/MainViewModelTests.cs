using NovelDownloader.Models;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// 搜索链路状态机单测: 用与 server.py 逐字一致的 JSONL 行喂 VM (或经 EventRouter),
/// 断言 hit 累加 / sres 结算 / sprog 更新 / 搜索态切换 / 同书分组注记。
/// 不涉及 UI 线程 (dispatcher 传 null 时 EventRouter 同步执行)。
/// </summary>
public class MainViewModelTests
{
    // ----------------------------------------------------------- 工具方法 --

    private static string HelloLine(int sources = 1292) =>
        $$$"""{"type":"hello","cmd":"init","version":"1.9.1","js":true,"sources":{{{sources}}},"files":7,"checked":7,"auth":835,"source_dir":"E:\\book\\bookdl\\shuyuan"}""";

    private static string HitLine(string name, string author = "作者", string kind = "都市",
                                  string last = "第1204章 尾声", string src = "起点") =>
        $$$"""{"type":"event","kind":"hit","payload":{"source":{"bookSourceName":"{{{src}}}"},"name":"{{{name}}}","author":"{{{author}}}","kind":"{{{kind}}}","book_url":"https://a/{{{name}}}","last_chapter":"{{{last}}}","intro":"简介"}}""";

    private static string SprogLine(string text) =>
        $$$"""{"type":"event","kind":"sprog","payload":"{{{text}}}"}""";

    private static string SresLine(int n, bool fuzzy = true) =>
        $$$"""{"type":"event","kind":"sres","payload":[{{{n}}},{{{(fuzzy ? "true" : "false")}}}]}""";

    // ---------------------------------------------------------------- 用例 --

    [Fact]
    public void Hello_UpdatesBackendStatusAndSourceCount()
    {
        var vm = new MainViewModel();

        vm.ApplyLine(HelloLine(1292));

        Assert.Equal(1292, vm.SourceCount);
        Assert.Contains("1292", vm.BackendStatus);
        Assert.Contains("v1.9.1", vm.BackendStatus);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("1292"));
    }

    [Fact]
    public void Hello_WithGroups_PopulatesSourceGroups_AndStaleSelectionResets()
    {
        var vm = new MainViewModel();
        vm.ApplyLine("""{"type":"hello","cmd":"init","version":"1.31","js":true,"sources":10,"files":3,"checked":3,"auth":0,"source_dir":"D:\\s","groups":["玄幻","都市"]}""");

        Assert.Equal(new[] { "全部", "玄幻", "都市" }, vm.SourceGroups);
        Assert.Equal("全部", vm.SourceGroup);

        vm.SourceGroup = "玄幻";
        vm.ApplyLine(HelloLine());

        Assert.Equal(new[] { "全部" }, vm.SourceGroups);
        Assert.Equal("全部", vm.SourceGroup);
    }

    [Fact]
    public void Hit_AccumulatesInObservableCollectionAndCount()
    {
        var vm = new MainViewModel();
        var raised = 0;
        vm.Hits.CollectionChanged += (_, _) => raised++;

        vm.ApplyLine(HitLine("我真没想重生啊"));
        vm.ApplyLine(HitLine("诡秘之主"));
        vm.ApplyLine(HitLine("宿命之环"));

        Assert.Equal(3, vm.Hits.Count);
        Assert.Equal(3, vm.HitCount);
        Assert.Equal(3, raised);
        Assert.Equal("我真没想重生啊", vm.Hits[0].Name);
        Assert.Equal("起点", vm.Hits[0].SourceName);
        Assert.Equal("作者 · 最新:第1204章 尾声", vm.Hits[0].SubtitleLine);
        Assert.Equal("都市 · 起点", vm.Hits[0].MetaLine);
    }

    [Fact]
    public void Sprog_UpdatesSearchProgressText()
    {
        var vm = new MainViewModel();

        vm.ApplyLine(SprogLine("37/384 源 · 抓取中"));

        Assert.Equal("37/384 源 · 抓取中", vm.SearchProgress);
    }

    [Fact]
    public void SearchFlow_BeginThenHitsThenSres_TransitionsStateAndSettlesCount()
    {
        var vm = new MainViewModel();

        vm.BeginSearch("我的");
        Assert.True(vm.IsSearching);
        Assert.False(vm.IsIdle);
        Assert.Equal("停止", vm.SearchButtonText);

        vm.ApplyLine(SprogLine("10/384 源 · 抓取中"));
        vm.ApplyLine(HitLine("我的1979"));
        vm.ApplyLine(HitLine("我的老板是妖皇"));
        Assert.True(vm.IsSearching);

        vm.ApplyLine(SresLine(2));

        Assert.False(vm.IsSearching);
        Assert.True(vm.IsIdle);
        Assert.Equal("搜索", vm.SearchButtonText);
        Assert.Equal(2, vm.HitCount);
        Assert.Equal(2, vm.EngineHitCount);
        Assert.Contains("命中 2 本", vm.SearchProgress);
        Assert.Contains("命中 2 本", vm.HitBadgeText);
    }

    [Fact]
    public void Sres_EngineCountGreaterThanUiCount_NotesFullCountInProgress()
    {
        // 引擎全量 12 本, 但「只看相关」只让 2 条到达 UI (hit 未发的其余被过滤)。
        var vm = new MainViewModel();
        vm.BeginSearch("我的");
        vm.ApplyLine(HitLine("我的1979"));
        vm.ApplyLine(HitLine("我的老板是妖皇"));

        vm.ApplyLine(SresLine(12));

        Assert.Equal(2, vm.HitCount);
        Assert.Equal(12, vm.EngineHitCount);
        Assert.Contains("引擎全量 12", vm.SearchProgress);
    }

    [Fact]
    public void BeginSearch_ClearsPreviousResults()
    {
        var vm = new MainViewModel();
        vm.ApplyLine(HitLine("上一轮"));
        Assert.Equal(1, vm.Hits.Count);

        vm.BeginSearch("新一轮");

        Assert.Empty(vm.Hits);
        Assert.Equal(0, vm.HitCount);
        Assert.True(vm.IsSearching);
    }

    [Fact]
    public void AddHit_SameNameAdjacent_MarksGroupContinuationWithAlternatingStripe()
    {
        // 同书相邻分组: 侧条颜色按**组**交替 (组内所有项同色), 只有非首项才有侧条。
        var vm = new MainViewModel();

        vm.AddHit(NewHit("诡秘之主", "起点"));     // 组 0 首项
        vm.AddHit(NewHit("诡秘之主", "笔趣阁"));   // 组 0 续项
        vm.AddHit(NewHit("诡秘之主", "飞卢"));     // 组 0 续项
        vm.AddHit(NewHit("宿命之环", "起点"));     // 组 1 首项 (交替色)
        vm.AddHit(NewHit("宿命之环", "笔趣阁"));   // 组 1 续项
        vm.AddHit(NewHit("长夜余火", "起点"));     // 组 2 首项 (回到非交替)

        Assert.Equal(6, vm.Hits.Count);

        // 组 0: 非交替, 续项显示"中性"侧条。
        Assert.False(vm.Hits[0].IsGroupContinuation);
        Assert.False(vm.Hits[0].IsAltGroup);
        Assert.True(vm.Hits[1].IsGroupContinuation);
        Assert.False(vm.Hits[1].IsAltGroup);
        Assert.True(vm.Hits[1].IsStripePlain);
        Assert.True(vm.Hits[2].IsGroupContinuation);
        Assert.True(vm.Hits[2].IsStripePlain);

        // 组 1: 交替 (Accent 侧条)。
        Assert.False(vm.Hits[3].IsGroupContinuation);
        Assert.True(vm.Hits[3].IsAltGroup);
        Assert.True(vm.Hits[4].IsGroupContinuation);
        Assert.True(vm.Hits[4].IsStripeAlt);

        // 组 2: 首项回到非交替。
        Assert.False(vm.Hits[5].IsGroupContinuation);
        Assert.False(vm.Hits[5].IsAltGroup);
    }

    [Fact]
    public void ClearHits_ResetsGroupAnchoring()
    {
        var vm = new MainViewModel();
        vm.AddHit(NewHit("A", "s1"));
        vm.AddHit(NewHit("A", "s2"));

        vm.ClearHits();
        vm.AddHit(NewHit("A", "s3"));

        Assert.Single(vm.Hits);
        // 清空后同名重启一组 → 首项 (无侧条)。
        Assert.False(vm.Hits[0].IsGroupContinuation);
    }

    [Fact]
    public void AppendLog_RespectsCapacity()
    {
        var vm = new MainViewModel();

        for (var i = 0; i < MainViewModel.LogCapacity + 50; i++)
        {
            vm.AppendLog($"line {i}");
        }

        Assert.Equal(MainViewModel.LogCapacity, vm.LogBuffer.Count);
        // 丢最旧, 保最新。
        Assert.Equal($"line {MainViewModel.LogCapacity + 49}", vm.LogBuffer[^1]);
        Assert.DoesNotContain("line 0", vm.LogBuffer);
    }

    [Fact]
    public void AbortSearch_UnlocksSearchingState()
    {
        var vm = new MainViewModel();
        vm.BeginSearch("我的");
        Assert.True(vm.IsSearching);

        vm.AbortSearch("后端忙");

        Assert.False(vm.IsSearching);
        Assert.Equal("后端忙", vm.SearchProgress);
    }

    [Fact]
    public void Dlrerr_Event_IsLogged()
    {
        var vm = new MainViewModel();

        vm.ApplyLine("""{"type":"event","kind":"dlerr","payload":"导出目录创建失败"}""");

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("导出目录创建失败"));
    }

    [Fact]
    public void BrokenJsonLine_IsLoggedNotThrown()
    {
        var vm = new MainViewModel();

        vm.ApplyLine("""{"type":"event","kind":"log",""");

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("JSON 解析失败"));
    }

    // -------------------------------------------------- EventRouter 集成 --

    [Fact]
    public void EventRouter_WithoutDispatcher_RoutesLinesIntoSameVmStateMachine()
    {
        var client = new BackendClient();          // 不启动进程
        using var router = new EventRouter(client, dispatcher: null);
        var vm = new MainViewModel();
        router.Attach(vm);

        var passed = new List<BackendEvent>();
        router.EventParsed += passed.Add;

        router.RouteRawLine(HelloLine(1292));
        router.RouteRawLine(SprogLine("5/384 源 · 抓取中"));
        Assert.Equal("5/384 源 · 抓取中", vm.SearchProgress);   // sprog 落定后, sres 前

        router.RouteRawLine(HitLine("诡秘之主"));
        router.RouteRawLine(HitLine("宿命之环"));
        router.RouteRawLine(SresLine(2));

        Assert.Equal(1292, vm.SourceCount);
        Assert.Equal(2, vm.Hits.Count);
        Assert.False(vm.IsSearching);
        Assert.Contains("命中 2 本", vm.SearchProgress);          // sres 结算覆盖进度文字

        // 旁路出口也收到全部 core 事件 (校验/下载事件不静默丢弃)。
        Assert.Equal(4, passed.Count);   // sprog + hit + hit + sres
        Assert.Contains(passed, e => e.Kind == "sres");
        Assert.Contains(passed, e => e.Kind == "sprog");
        Assert.Equal(2, passed.Count(e => e.Kind == "hit"));
    }

    [Fact]
    public void EventRouter_BusyMessage_UnlocksSearchState()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = new MainViewModel();
        router.Attach(vm);
        vm.BeginSearch("我的");

        router.RouteRawLine("""{"type":"busy","cmd":"search","running":"verify"}""");

        Assert.False(vm.IsSearching);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("verify"));
    }

    [Fact]
    public void EventRouter_PlainAck_IsSilent_NoProtocolJargonInLog()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = new MainViewModel();
        router.Attach(vm);

        router.RouteRawLine("""{"type":"ack","cmd":"stop","stopped":true,"busy":false,"running":""}""");

        // 纯回执不再刷日志, 命令名等协议术语不得泄漏到运行日志。
        Assert.DoesNotContain(vm.LogSnapshot(), l => l.Contains("ack") || l.Contains("stop"));
    }

    // ------------------------------------------------------------- 下载测试 --

    [Fact]
    public void Defaults_AutoFormatPickOneMode_NoOutDirUntilHello()
    {
        var vm = new MainViewModel();

        Assert.Equal("万里挑一", vm.DownloadMode);
        Assert.Equal("自动", vm.ExportFormat);
        Assert.Equal("", vm.OutDir);

        vm.ApplyHello(new BackendHello("1.31", Js: true, Sources: 1, Files: 1, Checked: 1,
            Auth: 0, SourceDir: "shuyuan", DataDir: @"D:\bookdata", DefaultOut: @"D:\bookdata\downloads"));

        Assert.Equal(@"D:\bookdata\downloads", vm.OutDir);
    }

    [Theory]
    [InlineData("万里挑一", "single")]
    [InlineData("全部下载", "merge")]
    [InlineData("合并", "merge")]
    [InlineData("单一", "single")]
    public void MapModeUiTextToWireProtocol(string ui, string wire)
        => Assert.Equal(wire, BackendClient.MapMode(ui));

    [Theory]
    [InlineData(null, "auto")]
    [InlineData("自动", "auto")]
    [InlineData("EPUB", "epub")]
    [InlineData("TXT", "txt")]
    public void MapFormatUiTextToWireProtocol(string? ui, string wire)
        => Assert.Equal(wire, BackendClient.MapFormat(ui));

    [Fact]
    public void BeginDownload_TransitionsToDownloadingState()
    {
        var vm = new MainViewModel();

        vm.BeginDownload(3, "万里挑一", "自动");

        Assert.True(vm.IsDownloading);
        Assert.Equal("万里挑一", vm.DownloadMode);
        Assert.Equal("自动", vm.ExportFormat);
        Assert.Contains("停止下载", vm.DownloadButtonText);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("开始下载"));
    }

    [Fact]
    public void DownloadBookEvent_UpdatesProgressAndLogs()
    {
        var vm = new MainViewModel();
        vm.BeginDownload(3, "万里挑一", "自动");

        vm.ApplyLine("""{"type":"event","kind":"dlbook","payload":[0,3,"诡秘之主","起点","探测目录…"]}""");

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("诡秘之主"));
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("起点"));
    }

    [Fact]
    public void DownloadProgressEvent_UpdatesChapterCount()
    {
        var vm = new MainViewModel();
        vm.BeginDownload(1, "万里挑一", "自动");

        vm.ApplyLine("""{"type":"event","kind":"dlprog","payload":[50,200,"第50章"]}""");

        Assert.Contains("50/200", vm.DownloadStatus);
    }

    [Fact]
    public void DownloadOneEvent_AddsToQueueAndLogs()
    {
        var vm = new MainViewModel();
        vm.BeginDownload(2, "万里挑一", "自动");

        vm.ApplyLine("""{"type":"event","kind":"dlone","payload":[{"title":"诡秘之主","author":"乌贼","kind":"玄幻","source":"起点","ok":1450,"total":1450},"D:/out/诡秘之主.txt",0,2]}""");

        Assert.Single(vm.DownloadQueue);
        Assert.Equal("诡秘之主", vm.DownloadQueue[0].Name);
        Assert.Equal("完成", vm.DownloadQueue[0].Status);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("诡秘之主") && l.Contains("D:/out"));
    }

    [Fact]
    public void DownloadDoneEvent_FinishesDownloadingState()
    {
        var vm = new MainViewModel();
        vm.BeginDownload(2, "batch", "epub");

        vm.ApplyLine("""{"type":"event","kind":"dldone","payload":["batch","epub",[[{"title":"A","ok":9,"total":10},"out/A.epub"]],[[1,"B","源X","403"]]]}""");

        Assert.False(vm.IsDownloading);
        Assert.Contains("成功 1 本", vm.DownloadStatus);
        Assert.Contains("失败 1 本", vm.DownloadStatus);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("下载结束"));
    }

    [Fact]
    public void DownloadCancelEvent_FinishesDownloadingState()
    {
        var vm = new MainViewModel();
        vm.BeginDownload(2, "万里挑一", "自动");

        vm.ApplyLine("""{"type":"event","kind":"dlcancel","payload":[[],[[0,"A","源X","取消"]]]}""");

        Assert.False(vm.IsDownloading);
        Assert.Contains("成功 0 本", vm.DownloadStatus);
    }

    [Fact]
    public void DownloadErrorEvent_IsLogged()
    {
        var vm = new MainViewModel();

        vm.ApplyLine("""{"type":"event","kind":"dlerr","payload":"导出目录创建失败"}""");

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("导出目录创建失败"));
    }

    // ------------------------------------------------------------- 助手 --

    private static HitItem NewHit(string name, string source) => new()
    {
        Name = name,
        Author = "作者",
        Kind = "都市",
        BookUrl = $"https://a/{source}/{name}",
        LastChapter = "第1章",
        SourceName = source,
    };
}
