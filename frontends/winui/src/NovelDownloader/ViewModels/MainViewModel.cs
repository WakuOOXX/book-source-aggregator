using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using NovelDownloader.Models;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 搜索页 VM (§4.2 事件映射的落点)。
///
/// 纯逻辑, 不引用任何 WinUI 类型 —— 事件由 EventRouter 在 UI 线程上调用
/// <see cref="ApplyEvent"/> / <see cref="ApplyHello"/>, 因此这里可以脱离 UI 线程单测
/// (tests/NovelDownloader.Tests 用 EventRouter.RouteLine 喂 JSONL 行断言状态机)。
///
/// 映射:
///   log   → Logs (带上限)         sprog → SearchProgress
///   hit   → Hits (逐条 Add)       sres  → 结算 IsSearching=false + 计数徽标
/// </summary>
public sealed class MainViewModel : ObservableObject
{
    /// <summary>日志上限 (防长跑内存膨胀; 旧版 tk.Text 无上限是已知短板)。</summary>
    public const int LogCapacity = 500;

    private readonly List<string> _log = new();

    private string _keyword = string.Empty;
    private string _searchProgress = "就绪";
    private string _backendStatus = "后端未连接";
    private string _domain = "自动";
    private string _sourceGroup = "全部";
    private int _hitCount;
    private int _engineHitCount;
    private int _selectedCount;
    private int _sourceCount;
    private bool _isSearching;
    private bool _fuzzy = true;
    private bool _relatedOnly = true;
    private bool _deepOnly;

    // 下载状态 (M2)
    private bool _isDownloading;
    private string _downloadMode = "单一";
    private string _exportFormat = "TXT";
    private int _downloadBookCount;
    private int _downloadBookIndex;
    private int _downloadChapterDone;
    private int _downloadChapterTotal;
    private string _downloadStatus = "";

    // 同书分组 (增量流不含引擎的 _group_key, 这里按"相邻同名"自建分组)。
    private string? _lastHitName;
    private int _groupIndex = -1;
    private bool _currentGroupAlt;

    /// <summary>逐条飞入的结果卡片来源 (hit 事件 Add)。</summary>
    public ObservableCollection<HitItem> Hits { get; } = new();

    /// <summary>运行日志行 (log 事件 / 后端诊断)。</summary>
    public ObservableCollection<string> LogBuffer { get; } = new();

    /// <summary>下载队列: 下载中/完成/失败的书目列表 (M2)。</summary>
    public ObservableCollection<DownloadQueueItem> DownloadQueue { get; } = new();

    /// <summary>搜索域下拉数据源。</summary>
    public IReadOnlyList<string> Domains { get; } = new[] { "自动", "书名", "作者", "分类" };

    // ------------------------------------------------------------- 可绑定态 --

    public string Keyword
    {
        get => _keyword;
        set => SetProperty(ref _keyword, value);
    }

    /// <summary>搜索进度文字 (sprog)。</summary>
    public string SearchProgress
    {
        get => _searchProgress;
        set => SetProperty(ref _searchProgress, value);
    }

    /// <summary>后端连接摘要 (hello)。</summary>
    public string BackendStatus
    {
        get => _backendStatus;
        set => SetProperty(ref _backendStatus, value);
    }

    /// <summary>搜索域: 自动 / 书名 / 作者 / 分类。</summary>
    public string Domain
    {
        get => _domain;
        set => SetProperty(ref _domain, value);
    }

    /// <summary>书源分组 (M1 只有「全部」; M3 接后端 sources.list 填充)。</summary>
    public string SourceGroup
    {
        get => _sourceGroup;
        set => SetProperty(ref _sourceGroup, value);
    }

    /// <summary>界面上实际收到的命中卡片数 (与 sres 的引擎全量口径可能不同)。</summary>
    public int HitCount
    {
        get => _hitCount;
        private set
        {
            if (SetProperty(ref _hitCount, value))
            {
                OnPropertyChanged(nameof(HitBadgeText));
            }
        }
    }

    /// <summary>sres 报告的引擎全量命中数 (含被「只看相关」过滤掉的)。</summary>
    public int EngineHitCount
    {
        get => _engineHitCount;
        private set => SetProperty(ref _engineHitCount, value);
    }

    public int SelectedCount
    {
        get => _selectedCount;
        set
        {
            if (SetProperty(ref _selectedCount, value))
            {
                OnPropertyChanged(nameof(SelectedBadgeText));
                OnPropertyChanged(nameof(DownloadButtonText));
                OnPropertyChanged(nameof(CanDownload));
            }
        }
    }

    /// <summary>hello 里的工作源数。</summary>
    public int SourceCount
    {
        get => _sourceCount;
        private set => SetProperty(ref _sourceCount, value);
    }

    public bool IsSearching
    {
        get => _isSearching;
        private set
        {
            if (SetProperty(ref _isSearching, value))
            {
                OnPropertyChanged(nameof(SearchButtonText));
                OnPropertyChanged(nameof(IsIdle));
                OnPropertyChanged(nameof(ShowEmptyHint));
            }
        }
    }

    /// <summary>空闲态 (与 <see cref="IsSearching"/> 互补, 供 XAML 简化显隐)。</summary>
    public bool IsIdle => !_isSearching;

    /// <summary>是否显示"还没有结果"占位: 仅空闲且当前无命中时显示 (避免浮在结果之上)。</summary>
    public bool ShowEmptyHint => !_isSearching && Hits.Count == 0;

    /// <summary>模糊搜索 (默认开)。</summary>
    public bool Fuzzy
    {
        get => _fuzzy;
        set => SetProperty(ref _fuzzy, value);
    }

    /// <summary>只看相关结果 (默认开)。</summary>
    public bool RelatedOnly
    {
        get => _relatedOnly;
        set => SetProperty(ref _relatedOnly, value);
    }

    /// <summary>只搜试搜通过源 (默认关)。</summary>
    public bool DeepOnly
    {
        get => _deepOnly;
        set => SetProperty(ref _deepOnly, value);
    }

    // ------------------------------------------------------------- 下载属性 (M2) --

    /// <summary>是否正在下载中。</summary>
    public bool IsDownloading
    {
        get => _isDownloading;
        private set
        {
            if (SetProperty(ref _isDownloading, value))
            {
                OnPropertyChanged(nameof(DownloadButtonText));
                OnPropertyChanged(nameof(CanDownload));
            }
        }
    }

    /// <summary>下载按钮文案: 下载中变「停止下载」。</summary>
    public string DownloadButtonText
    {
        get
        {
            if (_isDownloading)
            {
                return $"停止下载 ({_downloadBookIndex + 1}/{_downloadBookCount})";
            }

            return $"下载选中({_selectedCount})";
        }
    }

    /// <summary>当前下载进度: 已完成章数/总章数。</summary>
    public string DownloadStatus
    {
        get => _downloadStatus;
        set => SetProperty(ref _downloadStatus, value);
    }

    /// <summary>导出格式: TXT 或 EPUB。</summary>
    public string ExportFormat
    {
        get => _exportFormat;
        set => SetProperty(ref _exportFormat, value);
    }

    /// <summary>下载模式: 单一 或 合并。</summary>
    public string DownloadMode
    {
        get => _downloadMode;
        set => SetProperty(ref _downloadMode, value);
    }

    // ------------------------------------------------------------- 计算属性 --

    /// <summary>搜索按钮文案: 搜索中变「停止」。</summary>
    public string SearchButtonText => _isSearching ? "停止" : "搜索";

    public string HitBadgeText => $"命中 {_hitCount} 本";

    public string SelectedBadgeText => $"已选 {_selectedCount} 本";

    public bool CanDownload => _selectedCount > 0 && !_isDownloading;

    // --------------------------------------------------------------- 事件入口 --

    /// <summary>分发一条 core 事件 (在 UI 线程调用)。M1 搜索 + M2 下载。</summary>
    public void ApplyEvent(BackendEvent ev)
    {
        switch (ev)
        {
            case LogEvent log:
                AppendLog(log.Message);
                break;

            case SearchProgressEvent prog:
                SearchProgress = prog.Text;
                break;

            case HitEvent hit:
                AddHit(HitItem.FromDto(hit.Hit));
                break;

            case SearchResultEvent sres:
                FinishSearch(sres);
                break;

            // ---- 下载事件 (M2) ----
            case DownloadBookEvent dlbook:
                _downloadBookIndex = dlbook.BookIndex;
                _downloadBookCount = dlbook.BookCount;
                DownloadStatus = $"{dlbook.Name} ({dlbook.Source}) {dlbook.Message}";
                OnPropertyChanged(nameof(DownloadButtonText));
                AppendLog($"▼ 下载 [{dlbook.BookIndex + 1}/{dlbook.BookCount}] {dlbook.Name} · {dlbook.Source}");
                break;

            case DownloadProgressEvent dlprog:
                _downloadChapterDone = dlprog.Done;
                _downloadChapterTotal = dlprog.Total;
                DownloadStatus = $"章节 {dlprog.Done}/{dlprog.Total} · {dlprog.Message}";
                break;

            case DownloadOneEvent dlone:
                DownloadQueue.Add(new DownloadQueueItem
                {
                    Name = dlone.Book.Title,
                    Author = dlone.Book.Author,
                    Source = dlone.Book.Source,
                    Path = dlone.Path,
                    Status = "完成",
                });
                AppendLog($"✔ [{dlone.BookIndex + 1}/{dlone.BookCount}] {dlone.Book.Title} → {dlone.Path}");
                break;

            case DownloadDoneEvent dldone:
                FinishDownload(dldone.Mode, dldone.Format, dldone.OkList, dldone.FailList);
                break;

            case DownloadCancelEvent dlcancel:
                FinishDownload("cancel", "", dlcancel.OkList, dlcancel.FailList);
                break;

            case DownloadErrorEvent err:
                AppendLog("✘ " + err.Message);
                break;

            // vfile/vprog/vfile_done/vdeep/vdone: M1 不接, 由 EventParsed 出口旁路。
            default:
                break;
        }
    }

    /// <summary>hello 到达: 记录后端版本 / 源统计。</summary>
    public void ApplyHello(BackendHello hello)
    {
        SourceCount = hello.Sources;
        BackendStatus = hello.Summary;
        AppendLog("· " + hello.Summary);
    }

    /// <summary>开始搜索: 清空上一轮结果, 进入搜索态。</summary>
    public void BeginSearch(string keyword)
    {
        Keyword = keyword;
        ClearHits();
        EngineHitCount = 0;
        SelectedCount = 0;
        SearchProgress = $"搜索中… ({_domain})";
        AppendLog($"▶ 搜索「{keyword}」· 域 {_domain} · 模糊 {(Fuzzy ? "开" : "关")}"
                  + $" · 只看相关 {(RelatedOnly ? "开" : "关")}"
                  + $" · 只搜通过源 {(DeepOnly ? "开" : "关")}");
        IsSearching = true;
    }

    /// <summary>sres 结算: 退出搜索态, 落定计数。</summary>
    public void FinishSearch(SearchResultEvent sres)
    {
        EngineHitCount = sres.HitCount;
        HitCount = Hits.Count;
        IsSearching = false;

        var suffix = sres.Fuzzy ? "（模糊）" : "（精确）";
        var extra = sres.HitCount != Hits.Count
            ? $" · 引擎全量 {sres.HitCount} 本"
            : "";
        SearchProgress = $"搜索结束：命中 {Hits.Count} 本{suffix}{extra}";
        AppendLog("■ " + SearchProgress);
    }

    /// <summary>搜索被后端拒绝 (busy) 或协议错误时回到空闲态。</summary>
    public void AbortSearch(string reason)
    {
        IsSearching = false;
        SearchProgress = reason;
        AppendLog("✘ " + reason);
    }

    /// <summary>清空结果 (保留日志)。</summary>
    public void ClearHits()
    {
        Hits.Clear();
        HitCount = 0;
        _lastHitName = null;
        _groupIndex = -1;
        _currentGroupAlt = false;
        OnPropertyChanged(nameof(ShowEmptyHint));
    }

    /// <summary>命中逐条上屏 (含同书相邻分组注记)。</summary>
    public void AddHit(HitItem item)
    {
        if (item.Name == _lastHitName && _groupIndex >= 0)
        {
            // 同一本书的下一个源: 属当前组, 非首项。
            item.IsGroupContinuation = true;
            item.IsAltGroup = _currentGroupAlt;
        }
        else
        {
            _groupIndex++;
            _lastHitName = item.Name;
            _currentGroupAlt = _groupIndex % 2 == 1;
            item.IsGroupContinuation = false;
            item.IsAltGroup = _currentGroupAlt;
        }

        Hits.Add(item);
        HitCount = Hits.Count;
        OnPropertyChanged(nameof(ShowEmptyHint));
    }

    /// <summary>追加一行日志 (超上限丢最旧)。</summary>
    public void AppendLog(string? message)
    {
        if (string.IsNullOrEmpty(message))
        {
            return;
        }

        _log.Add(message);
        LogBuffer.Add(message);
        while (_log.Count > LogCapacity)
        {
            _log.RemoveAt(0);
            LogBuffer.RemoveAt(0);
        }
    }

    /// <summary>VM 层纯函数: 解析一行 JSONL 并分发 (单测复用, 不依赖 UI)。</summary>
    public void ApplyLine(string jsonlLine)
    {
        var env = BackendEventParser.ParseEnvelope(jsonlLine, out var error);
        if (error is not null)
        {
            AppendLog("✘ " + error);
            return;
        }

        if (env is null)
        {
            return;
        }

        if (env.Type == "hello")
        {
            ApplyHello(env.ToHello());
            return;
        }

        if (!string.IsNullOrEmpty(env.Message) && env.Type == "error")
        {
            AppendLog("✘ [后端] " + env.Message);
            return;
        }

        var ev = BackendEventParser.ParseEventFromEnvelope(env, out var evError);
        if (evError is not null)
        {
            AppendLog("✘ " + evError);
            return;
        }

        if (ev is not null)
        {
            ApplyEvent(ev);
        }
    }

    /// <summary>日志快照 (便于断言/导出)。</summary>
    public IReadOnlyList<string> LogSnapshot() => _log.ToArray();

    // ------------------------------------------------------------- 下载方法 (M2) --

    /// <summary>开始下载: 进入下载态, 清空下载队列。</summary>
    public void BeginDownload(int bookCount, string mode, string fmt)
    {
        DownloadQueue.Clear();
        _downloadBookCount = bookCount;
        _downloadBookIndex = 0;
        _downloadChapterDone = 0;
        _downloadChapterTotal = 0;
        DownloadMode = mode;
        ExportFormat = fmt;
        DownloadStatus = $"准备下载 {bookCount} 本 ({mode}/{fmt})…";
        IsDownloading = true;
        AppendLog($"▶ 开始下载: {bookCount} 本 · 模式 {mode} · 格式 {fmt}");
    }

    /// <summary>下载完成: 退出下载态, 汇总结果。</summary>
    public void FinishDownload(string mode, string fmt,
        IReadOnlyList<DownloadOkItemDto> okList,
        IReadOnlyList<DownloadFailItemDto> failList)
    {
        IsDownloading = false;
        var okCount = okList.Count;
        var failCount = failList.Count;
        DownloadStatus = $"下载结束: 成功 {okCount} 本, 失败 {failCount} 本";
        AppendLog($"■ 下载结束: 成功 {okCount} 本, 失败 {failCount} 本");

        foreach (var f in failList)
        {
            AppendLog($"  ✘ [{f.Index}] {f.Name} ({f.Source}): {f.Error}");
        }
    }

    /// <summary>停止下载 (取消态)。</summary>
    public void AbortDownload(string reason)
    {
        IsDownloading = false;
        DownloadStatus = reason;
        AppendLog("✘ " + reason);
    }
}

/// <summary>下载队列中的单条记录 (M2)。</summary>
public sealed class DownloadQueueItem
{
    public string Name { get; init; } = "";
    public string Author { get; init; } = "";
    public string Source { get; init; } = "";
    public string Path { get; init; } = "";
    public string Status { get; init; } = ""; // "下载中" / "完成" / "失败"
}
