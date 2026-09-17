using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// UiThrottle 节流器单测: 1000 条 hit 洪流下必须整批缓冲、单回合排空,
/// 证明「逐条上屏会卡 UI」的问题被合流/批量路径消除 (配合 EventRouter 的
/// AddHitsRange 整批计数)。
/// </summary>
public class UiThrottleTests
{
    private static string HitLine(string name, string src = "起点") =>
        $$$"""{"type":"event","kind":"hit","payload":{"source":{"bookSourceName":"{{{src}}}"},"name":"{{{name}}}","author":"作者","kind":"都市","book_url":"https://a/{{{name}}}","last_chapter":"第1章","intro":"简介"}}""";

    [Fact]
    public void UiThrottle_Batching_PostsThenFlushesWholeBatch()
    {
        var flushed = new List<IReadOnlyList<ThrottleOp>>();
        var throttle = new UiThrottle(
            batching: true,
            onCallbackThread: () => true,
            postToCallbackThread: _ => { },
            sink: flushed.Add);

        for (var i = 0; i < 1000; i++)
        {
            throttle.Post(new ActionOp(() => { }));
        }

        // 全部积压, 未逐条同步执行。
        Assert.Equal(1000, throttle.PendingCount);

        throttle.FlushNow();

        Assert.Equal(0, throttle.PendingCount);
        Assert.Single(flushed);                 // 整批一次排空
        Assert.Equal(1000, flushed[0].Count);   // 不丢任何一条
    }

    [Fact]
    public void EventRouter_Throttled_1000Hits_ApplyInSingleFlush_CountCorrect()
    {
        var client = new BackendClient();
        var vm = new MainViewModel();
        EventRouter router = null!;
        var throttle = new UiThrottle(
            batching: true,
            onCallbackThread: () => true,
            postToCallbackThread: _ => { },
            sink: ops => router.ApplyOps(ops));

        router = new EventRouter(client, throttle);
        router.Attach(vm);

        for (var i = 0; i < 1000; i++)
        {
            router.RouteRawLine(HitLine($"书{i}"));
        }

        // 洪流尚未排空: 全部积压在节流器里, VM 集合仍是空的 —— 逐条上屏的
        // 布局/动画风暴被缓冲掉, UI 线程留出输入/渲染时间。
        Assert.Equal(1000, throttle.PendingCount);
        Assert.Empty(vm.Hits);

        throttle.FlushNow();

        Assert.Equal(0, throttle.PendingCount);
        Assert.Equal(1000, vm.Hits.Count);
        Assert.Equal(1000, vm.HitCount);
    }

    [Fact]
    public void EventRouter_NoDispatcher_BehavesSynchronouslyLikeOldPath()
    {
        var client = new BackendClient();
        using var router = new EventRouter(client, dispatcher: null);
        var vm = new MainViewModel();
        router.Attach(vm);

        router.RouteRawLine(HitLine("诡秘之主"));
        router.RouteRawLine(HitLine("宿命之环"));

        // 无 DispatcherQueue 时 batching 关闭, 逐条同步执行 (测试/降级路径)。
        Assert.Equal(2, vm.Hits.Count);
        Assert.Equal(2, vm.HitCount);
    }
}
