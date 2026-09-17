using System;
using System.Collections.Generic;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.Services;

/// <summary>
/// 节流批处理的单元操作。
///   EventOp  —— core 事件 (hit/sprog/log/v*/dl*), 参与合流 (连续 hit/log 合并、sprog/vprog 覆盖)。
///   ActionOp —— 其余需要按原序到达 UI 的动作 (hello / ack / busy / error / stderr / 进程退出)。
/// </summary>
public abstract record ThrottleOp;

/// <summary>一条 core 事件 (旁路出口按序逐条回放, VM 应用时才合流)。</summary>
public sealed record EventOp(BackendEvent Event) : ThrottleOp;

/// <summary>一个需要在回调线程 (UI) 上执行的动作。</summary>
public sealed record ActionOp(Action Action) : ThrottleOp;

/// <summary>
/// UI 响应性节流器 (并发洪流不卡 UI 的核心)。
///
/// 背景: 384 并发搜索时 hit/sprog 事件每秒数百条。若每条都独立
/// DispatcherQueue.TryEnqueue → ObservableCollection.Add, UI 线程被逐条
/// 布局/动画占满, 界面假死。这里把消息按 FIFO 缓冲, 由外部时钟
/// (EventRouter 里的 DispatcherQueueTimer, ~64ms) 成批排空到 sink ——
/// 每个 UI 回合只处理一批, 回合之间留出输入/渲染时间。
///
/// 保序: 所有消息 (含 hello/ack/busy) 都走同一个 pending 队列, 与事件之间
/// 的相对顺序不变 (sres 一定排在本轮 hit 之后)。
///
/// 测试模式: batching=false 时 Post 即同步排空 (单条批次), 与旧逐条行为完全
/// 一致 —— dispatcher 为 null 的 EventRouter 单测因此无需改动。
///
/// 线程纪律: Post 可在后台线程 (读行线程) 调用; sink 保证在回调线程执行。
/// </summary>
public sealed class UiThrottle
{
    /// <summary>积压硬顶: 超过立即冲刷 (UI 被卡死时兜底, 防内存无界)。</summary>
    public const int OverflowCount = 4000;

    private readonly object _sync = new();
    private readonly List<ThrottleOp> _pending = new();
    private readonly Func<bool> _onCallbackThread;
    private readonly Action<Action> _postToCallbackThread;
    private readonly Action<IReadOnlyList<ThrottleOp>> _sink;

    /// <param name="batching">false = 逐条同步执行 (无 DispatcherQueue 的测试/降级路径)。</param>
    /// <param name="onCallbackThread">当前线程是否就是回调 (UI) 线程。</param>
    /// <param name="postToCallbackThread">把动作编组到回调线程。</param>
    /// <param name="sink">在回调线程上执行一批操作。</param>
    public UiThrottle(
        bool batching,
        Func<bool> onCallbackThread,
        Action<Action> postToCallbackThread,
        Action<IReadOnlyList<ThrottleOp>> sink)
    {
        Batching = batching;
        _onCallbackThread = onCallbackThread;
        _postToCallbackThread = postToCallbackThread;
        _sink = sink;
    }

    /// <summary>是否处于批量模式。</summary>
    public bool Batching { get; }

    /// <summary>当前积压条数 (测试断言"未逐条同步上屏"用)。</summary>
    public int PendingCount
    {
        get
        {
            lock (_sync)
            {
                return _pending.Count;
            }
        }
    }

    /// <summary>投喂一条操作。批量模式下仅入队, 等下一次定时排空。</summary>
    public void Post(ThrottleOp op)
    {
        int count;
        lock (_sync)
        {
            _pending.Add(op);
            count = _pending.Count;
        }

        if (!Batching || count >= OverflowCount)
        {
            FlushNow();
        }
    }

    /// <summary>立即排空 (DispatcherQueueTimer tick / 溢出兜底 / 测试手动触发)。空队无操作。</summary>
    public void FlushNow()
    {
        ThrottleOp[] ops;
        lock (_sync)
        {
            if (_pending.Count == 0)
            {
                return;
            }

            ops = _pending.ToArray();
            _pending.Clear();
        }

        TraceLog.Write($"FlushNow n={ops.Length}");

        if (_onCallbackThread())
        {
            _sink(ops);
        }
        else
        {
            _postToCallbackThread(() => _sink(ops));
        }
    }

    /// <summary>丢弃未排空的操作 (进程退出/释放时防陈旧上屏)。</summary>
    public void ClearPending()
    {
        lock (_sync)
        {
            _pending.Clear();
        }
    }
}
