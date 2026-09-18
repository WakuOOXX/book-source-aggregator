using System.Collections.Generic;
using System.Linq;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// 登录头页 VM (M4) 状态机单测: auth.list 上屏、过滤、选中→保存参数、
/// 单站两段式抓取、批量目标收集与阶段推进、暂停/跳过语义、
/// auth 命令族 ack 经 EventRouter 路由。不涉及 UI 线程。
/// </summary>
public class AuthViewModelTests
{
    // ------------------------------------------------------------ 工具方法 --

    private static AuthViewModel NewVmWithSources()
    {
        var vm = new AuthViewModel();
        vm.ApplyAuthList(new[]
        {
            new AuthEntryDto("http://a.com", "x=1", default),
            new AuthEntryDto("http://b.com/", "", default),
            new AuthEntryDto("https://c.com#别名", "y=2", default),
        });
        return vm;
    }

    private static AuthViewModel NewBatchVm(string auto, int nHosts)
    {
        var vm = new AuthViewModel();
        var urls = Enumerable.Range(0, nHosts)
            .Select(i => $"http://h{i}.com")
            .ToList();
        var result = vm.BuildBatchTargets(urls, skipConfigured: false);
        vm.BeginBatch(auto == "auto", result.Hosts, result.Targets, k: 10);
        return vm;
    }

    private static string AuthListAckLine(int count, params (string Url, string Cookie, string HeaderJson)[] rows)
    {
        var entries = string.Join(",", rows.Select(r =>
            $$"""{"url":"{{r.Url}}","cookie":"{{r.Cookie}}","header":{{(r.HeaderJson.Length > 0 ? r.HeaderJson : "{}")}}}"""));
        return $$"""{"type":"ack","cmd":"auth.list","count":{{count}},"entries":[{{entries}}]}""";
    }

    private static string AuthFetchLaunchedLine(string url, string grabHost) =>
        $$"""{"type":"ack","cmd":"auth.fetch","phase":"launched","url":"{{url}}","grab_host":"{{grabHost}}"}""";

    private static string AuthFetchCapturedLine(string host, string cookie) =>
        $$"""{"type":"ack","cmd":"auth.fetch","phase":"captured","host":"{{host}}","cookie":"{{cookie}}"}""";

    // ------------------------------------------------------------ 列表/过滤 --

    [Fact]
    public void ApplyAuthList_PopulatesSourcesAndConfigDots()
    {
        var vm = NewVmWithSources();

        Assert.True(vm.ListLoaded);
        Assert.Equal(3, vm.Sources.Count);
        Assert.Equal(3, vm.FilteredSources.Count);
        Assert.True(vm.Sources[0].HasCookie);
        Assert.False(vm.Sources[1].HasCookie);
        Assert.Equal("●", vm.Sources[0].Dot);
        Assert.Equal("○", vm.Sources[1].Dot);
        Assert.Equal("a.com", vm.Sources[0].Name);
        Assert.Equal("c.com", vm.Sources[2].Name);
    }

    [Fact]
    public void ApplyAuthList_PreservesStillExistingSelection()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com", "http://gone.com" });

        vm.ApplyAuthList(new[]
        {
            new AuthEntryDto("http://a.com", "x=2", default),
            new AuthEntryDto("http://b.com/", "", default),
        });

        Assert.Equal(2, vm.Sources.Count);
        Assert.Equal(new[] { "http://a.com" }, vm.SelectedUrls);
        Assert.Equal(1, vm.SelectedUrls.Count);
    }

    [Fact]
    public void FilterText_FiltersByNameAndUrl_CaseInsensitive()
    {
        var vm = NewVmWithSources();

        vm.FilterText = "B.COM";

        Assert.Single(vm.FilteredSources);   // 只命中 http://b.com/
        Assert.Equal("http://b.com/", vm.FilteredSources[0].Url);

        vm.FilterText = "不存在";

        Assert.Empty(vm.FilteredSources);

        vm.FilterText = "";

        Assert.Equal(3, vm.FilteredSources.Count);
    }

    [Fact]
    public void FilterText_EmptyShowsAllSources()
    {
        var vm = NewVmWithSources();
        Assert.Equal(3, vm.FilteredSources.Count);
    }

    // ------------------------------------------------------------ 选择/保存 --

    [Fact]
    public void SetSelectedUrls_SingleSelectionLoadsDetailEditor()
    {
        var vm = NewVmWithSources();

        vm.SetSelectedUrls(new[] { "http://a.com" });

        Assert.Equal(1, vm.SelectedUrls.Count);
        Assert.StartsWith("当前源", vm.DetailTitle);
        Assert.Contains("●", vm.DetailTitle);
        Assert.Equal("x=1", vm.EditCookie);
    }

    [Fact]
    public void SetSelectedUrls_MultiClearsEditorForBatchEntry()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com" });
        Assert.Equal("x=1", vm.EditCookie);

        vm.SetSelectedUrls(new[] { "http://a.com", "http://b.com/" });

        Assert.StartsWith("已选 2", vm.DetailTitle);
        Assert.Equal("", vm.EditCookie);
        Assert.True(vm.CanSave);
    }

    [Fact]
    public void PrepareSave_ReturnsSelectedUrlsCookieAndHeader()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.EditCookie = "test=1";
        vm.EditHeaderJson = """{"Referer": "https://a.com/"}""";

        var payload = vm.PrepareSave();

        Assert.NotNull(payload);
        Assert.Equal(new[] { "http://a.com" }, payload!.Urls);
        Assert.Equal("test=1", payload.Cookie);
        Assert.Equal("""{"Referer": "https://a.com/"}""", payload.HeaderJson);
    }

    [Fact]
    public void PrepareSave_NoSelectionReturnsNullAndLogs()
    {
        var vm = NewVmWithSources();

        var payload = vm.PrepareSave();

        Assert.Null(payload);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("未选中"));
    }

    [Fact]
    public void PrepareRemove_ReturnsEmptyCookieAndHeader()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com" });

        var payload = vm.PrepareRemove();

        Assert.NotNull(payload);
        Assert.Equal("", payload!.Cookie);
        Assert.Equal("", payload.HeaderJson);
    }

    [Fact]
    public void ApplySavedPayload_UpdatesEntryCookieAndDot()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://b.com/" });

        vm.ApplySavedPayload(new AuthSavePayload(new[] { "http://b.com/" }, "new=1", ""));

        Assert.True(vm.Sources[1].HasCookie);
        Assert.Equal("new=1", vm.Sources[1].Cookie);
    }

    [Fact]
    public void OnAuthSavedAck_LogsAck()
    {
        var vm = NewVmWithSources();

        vm.OnAuthSavedAck(5);

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("已保存"));
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("5 条"));
    }

    // ------------------------------------------------------------ 单站抓取 --

    [Fact]
    public void PrepareFetchLaunch_RequiresSingleSelection()
    {
        var vm = NewVmWithSources();
        Assert.Null(vm.PrepareFetchLaunch());

        vm.SetSelectedUrls(new[] { "http://a.com" });
        Assert.Equal("http://a.com", vm.PrepareFetchLaunch());
        Assert.Equal("启动中…", vm.FetchButtonText);
        Assert.True(vm.IsFetchBusy);
        Assert.False(vm.CanFetch);
    }

    [Fact]
    public void ApplyFetchAck_LaunchedThenCapturedFillsEditor()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.PrepareFetchLaunch();

        vm.ApplyFetchAck(new AuthFetchAckInfo("launched", "http://a.com", "", "a.com", ""));

        Assert.True(vm.IsFetchLaunched);
        Assert.Equal("我登录好了 → 抓取", vm.FetchButtonText);

        vm.BeginFetchGrab();
        vm.ApplyFetchAck(new AuthFetchAckInfo("captured", "", "a.com", "", "k=v; m=n"));

        Assert.False(vm.IsFetchBusy);
        Assert.Equal("浏览器登录抓取", vm.FetchButtonText);
        Assert.Equal("k=v; m=n", vm.EditCookie);
        Assert.Contains("2 条 Cookie", vm.FetchStatus);
    }

    [Fact]
    public void ApplyFetchAck_CapturedEmptyCookie_ShowsRetryHint()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.PrepareFetchLaunch();
        vm.ApplyFetchAck(new AuthFetchAckInfo("launched", "http://a.com", "", "a.com", ""));

        vm.ApplyFetchAck(new AuthFetchAckInfo("captured", "", "a.com", "", ""));

        Assert.Contains("没抓到", vm.FetchStatus);
        Assert.Equal("浏览器登录抓取", vm.FetchButtonText);
    }

    [Fact]
    public void OnAuthError_FetchResetsPhaseToIdle()
    {
        var vm = NewVmWithSources();
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.PrepareFetchLaunch();

        vm.OnAuthError("auth.fetch", "抓取失败: 连接被拒绝");

        Assert.False(vm.IsFetchBusy);
        Assert.Equal("浏览器登录抓取", vm.FetchButtonText);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("抓取失败"));
    }

    // ------------------------------------------------------------ 批量收集 --

    [Fact]
    public void BuildBatchTargets_DedupesUrlsByHost()
    {
        var vm = NewVmWithSources();
        var result = vm.BuildBatchTargets(
            new[] { "http://a.com", "http://a.com/", "http://b.com/x", "c.com" },
            skipConfigured: false);

        Assert.Equal(3, result.TotalHosts);   // a.com (两 URL 合并) + b.com + c.com
        Assert.Equal(3, result.Hosts.Count);
        Assert.Equal(2, result.Targets["a.com"].Count);
        Assert.Equal(0, result.Skipped);
    }

    [Fact]
    public void BuildBatchTargets_SkipsConfiguredHosts()
    {
        var vm = NewVmWithSources();   // a.com 与 https://c.com#别名 已配 cookie

        var result = vm.BuildBatchTargets(
            new[] { "http://a.com", "http://b.com/", "https://c.com#别名" },
            skipConfigured: true);

        Assert.Equal(1, result.Hosts.Count);
        Assert.Equal("b.com", result.Hosts[0]);
        Assert.Equal(2, result.Skipped);
    }

    [Fact]
    public void BuildBatchTargets_NoValidUrls()
    {
        var vm = new AuthViewModel();
        var result = vm.BuildBatchTargets(new[] { "", "   " }, skipConfigured: false);

        Assert.Equal(0, result.TotalHosts);
        Assert.Empty(result.Hosts);
    }

    // ------------------------------------------------------------ 批量阶段推进 --

    [Fact]
    public void BeginBatch_ManualEntersSerialStage()
    {
        var vm = NewBatchVm("manual", 2);

        Assert.True(vm.IsBatchRunning);
        Assert.Equal("手动逐站", vm.BatchStageText);
        Assert.True(vm.CanSkipSite);
        Assert.True(vm.CanGrabCurrent);
        Assert.Equal("暂停", vm.PauseButtonText);
    }

    [Fact]
    public void BeginBatch_AutoEntersParaStage()
    {
        var vm = NewBatchVm("auto", 3);

        Assert.True(vm.IsBatchRunning);
        Assert.Equal("并行扫", vm.BatchStageText);
        Assert.False(vm.CanSkipSite);   // 并行阶段无单站跳过语义
        Assert.False(vm.CanGrabCurrent);
    }

    [Fact]
    public void NextSite_AdvancesAndFinishesAtEnd()
    {
        var vm = NewBatchVm("manual", 2);

        var h1 = vm.NextSite();
        var h2 = vm.NextSite();
        var h3 = vm.NextSite();   // 越界 → 收尾

        Assert.Equal("h0.com", h1);
        Assert.Equal("h1.com", h2);
        Assert.Null(h3);
        Assert.False(vm.IsBatchRunning);
        Assert.Equal("待命", vm.BatchStageText);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("批量抓取结束"));
    }

    [Fact]
    public void NextSite_SetsCurrentHostAndTargetUrls()
    {
        var vm = NewBatchVm("manual", 2);

        vm.NextSite();

        Assert.Equal("h0.com", vm.CurrentBatchHost);
        Assert.Equal(new[] { "http://h0.com" }, vm.TargetUrlsFor("h0.com"));
        Assert.Contains("1/2", vm.BatchProgress);
    }

    [Fact]
    public void SkipSite_SerialAdvances_ParaRejects()
    {
        var manual = NewBatchVm("manual", 3);
        manual.NextSite();
        var next = manual.SkipSite();

        Assert.Equal("h1.com", next);
        Assert.False(manual.BatchProgress.Contains("不支持单站跳过"));

        var auto = NewBatchVm("auto", 3);
        auto.NextSite();
        var rejected = auto.SkipSite();

        Assert.Null(rejected);
        Assert.Contains("不支持单站跳过", auto.BatchProgress);
    }

    [Fact]
    public void TogglePause_FlipsStateAndButtonText()
    {
        var vm = NewBatchVm("manual", 2);

        var paused = vm.TogglePause();

        Assert.True(paused);
        Assert.Equal("继续", vm.PauseButtonText);
        Assert.Contains("已暂停", vm.BatchProgress);

        var resumed = vm.TogglePause();

        Assert.False(resumed);
        Assert.Equal("暂停", vm.PauseButtonText);
    }

    [Fact]
    public void StopBatch_FinishesAndKeepsPartialState()
    {
        var vm = NewBatchVm("manual", 3);
        vm.NextSite();

        vm.StopBatch();

        Assert.False(vm.IsBatchRunning);
        Assert.Equal("待命", vm.BatchStageText);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("已结束"));
    }

    [Fact]
    public void ApplyFetchAck_CapturedInBatch_IncrementsParaProgress()
    {
        var vm = NewBatchVm("auto", 3);
        vm.NextSite();

        vm.ApplyFetchAck(new AuthFetchAckInfo("captured", "", "h0.com", "", "ck=1"));

        Assert.True(vm.IsBatchRunning);
        Assert.True(vm.BatchPercent > 0);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("已保存 h0.com 的 Cookie"));
    }

    [Fact]
    public void ApplyFetchAck_CapturedInBatch_EmptyCookieLogsSkip()
    {
        var vm = NewBatchVm("manual", 2);
        vm.NextSite();

        vm.ApplyFetchAck(new AuthFetchAckInfo("captured", "", "h0.com", "", ""));

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("未抓到 Cookie"));
    }

    [Fact]
    public void EnterLoginStage_TransitionsToLoginStage()
    {
        var vm = NewBatchVm("auto", 3);

        vm.EnterLoginStage(1, 1, 1, 10);

        Assert.Equal("并行登录扫", vm.BatchStageText);
        Assert.Contains("并行登录扫", vm.BatchProgress);
        Assert.False(vm.CanSkipSite);
    }

    [Fact]
    public void ApplyLoginProgress_UpdatesBatchProgressText()
    {
        var vm = NewBatchVm("auto", 3);
        vm.EnterLoginStage(0, 1, 5, 10);

        vm.ApplyLoginProgress(2, 5, 10, 1, 1, "h1.com", 5);

        Assert.Contains("并行登录 2/5 站", vm.BatchProgress);
        Assert.Contains("已抓 1 · 已跳 1", vm.BatchProgress);
        Assert.Contains("无操作会自动抓取", vm.BatchProgress);
        Assert.True(vm.BatchPercent > 0);
    }

    [Fact]
    public void AbortAll_ResetsBatchAndFetch()
    {
        var vm = NewBatchVm("manual", 2);
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.PrepareFetchLaunch();

        vm.AbortAll("后端进程已退出");

        Assert.False(vm.IsBatchRunning);
        Assert.False(vm.IsFetchBusy);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("后端进程已退出"));
    }

    // ------------------------------------------------------------ HostOf --

    [Fact]
    public void HostOf_HandlesSchemePortPathFragment()
    {
        Assert.Equal("a.com", AuthViewModel.HostOf("http://a.com"));
        Assert.Equal("a.com", AuthViewModel.HostOf("http://a.com/x/y"));
        Assert.Equal("127.0.0.1", AuthViewModel.HostOf("http://127.0.0.1:11225/"));
        Assert.Equal("c.com", AuthViewModel.HostOf("https://c.com#别名"));
        Assert.Equal("a.com", AuthViewModel.HostOf("a.com"));
        Assert.Equal("", AuthViewModel.HostOf(""));
    }

    // ------------------------------------------------------- EventRouter 集成 --

    [Fact]
    public void EventRouter_AuthListAck_RoutesToAuthViewModel()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = new AuthViewModel();
        router.Attach(vm);

        router.RouteRawLine(AuthListAckLine(2,
            ("http://a.com", "x=1", "{}"),
            ("http://b.com/", "", """{"Referer":"http://b.com/"}""")));

        Assert.True(vm.ListLoaded);
        Assert.Equal(2, vm.Sources.Count);
        Assert.True(vm.Sources[0].HasCookie);
        Assert.False(vm.Sources[1].HasCookie);
        Assert.Contains("Referer", vm.Sources[1].HeaderJson);
    }

    [Fact]
    public void EventRouter_AuthFetchAck_RoutesSingleFetchPhase()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = NewVmWithSources();
        router.Attach(vm);
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.PrepareFetchLaunch();

        router.RouteRawLine(AuthFetchLaunchedLine("http://a.com", "a.com"));

        Assert.True(vm.IsFetchLaunched);
        Assert.Equal("我登录好了 → 抓取", vm.FetchButtonText);

        vm.BeginFetchGrab();
        router.RouteRawLine(AuthFetchCapturedLine("a.com", "k=1"));

        Assert.Equal("k=1", vm.EditCookie);
        Assert.False(vm.IsFetchBusy);
    }

    [Fact]
    public void EventRouter_AuthError_RoutesToAuthViewModel()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = NewVmWithSources();
        router.Attach(vm);
        vm.SetSelectedUrls(new[] { "http://a.com" });
        vm.PrepareFetchLaunch();

        router.RouteRawLine("""{"type":"error","message":"cdp_cookie 不可用,无法抓取","cmd":"auth.fetch"}""");

        Assert.False(vm.IsFetchBusy);
        Assert.Contains(vm.LogSnapshot(), l => l.Contains("cdp_cookie"));
    }

    [Fact]
    public void EventRouter_AuthSaveAck_RoutesToAuthViewModel()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = NewVmWithSources();
        router.Attach(vm);

        router.RouteRawLine("""{"type":"ack","cmd":"auth.save","urls":["http://a.com"],"count":4}""");

        Assert.Contains(vm.LogSnapshot(), l => l.Contains("已保存"));
    }

    // -------------------------------------------------- 线协议解析 (BackendEventParser) --

    [Fact]
    public void ParseAuthListAck_ReadsCountAndEntries()
    {
        var env = BackendEventParser.ParseEnvelope(AuthListAckLine(2,
            ("http://a.com", "x=1", "{}"),
            ("http://b.com/", "", """{"Referer":"http://b.com/"}""")), out _);

        var result = BackendEventParser.ParseAuthListAck(env!);

        Assert.NotNull(result);
        Assert.Equal(2, result!.Count);
        Assert.Equal(2, result.Entries.Count);
        Assert.Equal("http://a.com", result.Entries[0].Url);
        Assert.Equal("x=1", result.Entries[0].Cookie);
        Assert.Contains("Referer", result.Entries[1].HeaderJson);
    }

    [Fact]
    public void ParseAuthFetchAck_ReadsPhaseAndFields()
    {
        var env = BackendEventParser.ParseEnvelope(
            AuthFetchCapturedLine("a.com", "k=1"), out _);

        var info = BackendEventParser.ParseAuthFetchAck(env!);

        Assert.NotNull(info);
        Assert.Equal("captured", info!.Phase);
        Assert.Equal("a.com", info.Host);
        Assert.Equal("k=1", info.Cookie);
    }
}
