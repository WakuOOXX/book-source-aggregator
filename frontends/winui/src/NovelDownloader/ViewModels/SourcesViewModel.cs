using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 书源管理页 VM (M3): 书源文件清单 (sources.list ack) + 校验链路
/// (vfile / vprog / vfile_done / vdeep / vdone) + 校验日志镜像。
///
/// 与 MainViewModel 同规约: 纯逻辑, 不引用任何 WinUI 类型 —— 所有写入由
/// EventRouter 在 UI 线程上调 <see cref="ApplyEvent"/> / <see cref="ApplySourcesList"/> 完成,
/// 因此可以脱离 UI 线程单测 (dispatcher 为 null 时 EventRouter 同步执行)。
///
/// 路由 (EventRouter.ApplyOps):
///   sources.list ack → ApplySourcesList / FinishListLoad
///   log             → AppendLogs (批量镜像到书源页日志区)
///   vfile/vprog     → 当前文件 + 进度文字 + VerifyFiles 集合
///   vfile_done      → 单文件 ok/bad 落定 + 文件列表状态
///   vdeep           → 深度阶段文字
///   vdone           → 退出校验态 + 汇总
///   busy/进程退出   → AbortVerify (解锁按钮)
/// </summary>
public sealed class SourcesViewModel : ObservableObject
{
    /// <summary>书源页日志上限 (防长跑内存膨胀, 与搜索页一致)。</summary>
    public const int LogCapacity = 500;

    private readonly List<string> _log = new();

    private bool _isVerifying;
    private bool _isListLoaded;
    private string _verifyStatus = "就绪";
    private double _verifyPercent;
    private bool _deep;
    private int _totalOk;
    private int _totalBad;

    /// <summary>书源文件清单 (sources.list ack 上屏; 校验时状态点/计数就地更新)。</summary>
    public ObservableCollection<SourceFileItem> SourceFiles { get; } = new();

    /// <summary>本次校验逐文件进度 (每个文件一行: ok/bad + 状态)。</summary>
    public ObservableCollection<VerifyFileItem> VerifyFiles { get; } = new();

    /// <summary>书源页运行日志 (校验命令/事件/后端诊断镜像)。</summary>
    public ObservableCollection<string> LogBuffer { get; } = new();

    // ------------------------------------------------------------- 可绑定态 --

    /// <summary>是否正在校验 (按钮变「停止」)。</summary>
    public bool IsVerifying
    {
        get => _isVerifying;
        private set
        {
            if (SetProperty(ref _isVerifying, value))
            {
                OnPropertyChanged(nameof(IsIdle));
                OnPropertyChanged(nameof(VerifyButtonText));
                OnPropertyChanged(nameof(DeepVerifyButtonText));
                OnPropertyChanged(nameof(ShowVerifyingProgress));
            }
        }
    }

    /// <summary>空闲态 (与 IsVerifying 互补)。</summary>
    public bool IsIdle => !_isVerifying;

    /// <summary>校验/停止按钮文案 (校验中统一变「停止」)。</summary>
    public string VerifyButtonText => _isVerifying ? "停止" : "校验书源";

    /// <summary>深度校验/停止按钮文案。</summary>
    public string DeepVerifyButtonText => _isVerifying ? "停止" : "深度校验";

    /// <summary>是否显示进度区 (校验中或已有校验结果都显示)。</summary>
    public bool ShowVerifyingProgress => _isVerifying || VerifyFiles.Count > 0;

    /// <summary>sources.list 是否已加载完成 (驱动「加载中…」占位)。</summary>
    public bool IsListLoaded
    {
        get => _isListLoaded;
        private set => SetProperty(ref _isListLoaded, value);
    }

    /// <summary>文件列表标题计数。</summary>
    public string FileCountText => $"书源文件 ({SourceFiles.Count})";

    /// <summary>校验进度文字 (vfile「文件 i/n」/ vprog「探测 x/y · 有效 a · 失效 b」/ vdeep 阶段)。</summary>
    public string VerifyStatus
    {
        get => _verifyStatus;
        set
        {
            if (SetProperty(ref _verifyStatus, value))
            {
                TraceLog.Write("vstatus: " + value);
            }
        }
    }

    /// <summary>校验进度百分比 (vprog/vdeep 的 done/total; 空闲回 0)。</summary>
    public double VerifyPercent
    {
        get => _verifyPercent;
        private set => SetProperty(ref _verifyPercent, value);
    }

    // --------------------------------------------------------------- 文件清单 --

    /// <summary>sources.list ack 上屏: 重建文件列表, 进入「已加载」态。</summary>
    public void ApplySourcesList(SourcesListResult result)
    {
        SourceFiles.Clear();
        foreach (var dto in result.Files)
        {
            SourceFiles.Add(new SourceFileItem
            {
                Name = dto.Name,
                IsChecked = dto.Checked,
                Exists = dto.Exists,
                State = VerifyState.Idle,
            });
        }

        IsListLoaded = true;
        OnPropertyChanged(nameof(FileCountText));
        TraceLog.Write($"sources.list: {result.Files.Count} files, checked={result.Checked.Count}, dir={result.Dir}");
        AppendLog($"· 书源清单: {result.Files.Count} 个文件 · 勾选 {result.Checked.Count} · 目录 {result.Dir}");
    }

    /// <summary>sources.list ack 无数据 (解析失败/形状不符): 保持旧列表, 仅解锁加载态。</summary>
    public void FinishListLoad()
    {
        IsListLoaded = true;
        AppendLog("· 书源清单读取失败, 保持现有清单。");
    }

    // --------------------------------------------------------------- 校验入口 --

    /// <summary>开始校验 (code-behind 在发 verify 命令前调用): 进入校验态, 重置逐文件进度。</summary>
    public void BeginVerify(bool deep)
    {
        _deep = deep;
        _totalOk = 0;
        _totalBad = 0;
        VerifyFiles.Clear();
        VerifyPercent = 0;
        foreach (var f in SourceFiles)
        {
            if (f.Exists)
            {
                f.ResetProgress();
                f.State = VerifyState.Verifying;
            }
        }

        IsVerifying = true;
        VerifyStatus = deep ? "深度校验准备中…" : "准备校验书源文件…";
        AppendLog($"▶ 开始{(deep ? "深度" : "")}校验书源文件…");
    }

    /// <summary>校验事件路由: vfile/vprog/vfile_done/vdeep/vdone (EventRouter 只投 v* 到本 VM)。</summary>
    public void ApplyEvent(BackendEvent ev)
    {
        switch (ev)
        {
            case VerifyFileEvent vfile:
                ApplyVFile(vfile);
                break;

            case VerifyProgressEvent vprog:
                ApplyVProg(vprog);
                break;

            case VerifyFileDoneEvent fdone:
                ApplyVFileDone(fdone);
                break;

            case VerifyDeepEvent vdeep:
                ApplyVDeep(vdeep);
                break;

            case VerifyDoneEvent vdone:
                FinishVerify(vdone);
                break;

            default:
                break;
        }
    }

    /// <summary>校验被后端拒绝 (busy) / 进程退出: 回到空闲态并解锁按钮。</summary>
    public void AbortVerify(string reason)
    {
        IsVerifying = false;
        VerifyPercent = 0;
        VerifyStatus = reason;
        foreach (var f in SourceFiles)
        {
            if (f.State == VerifyState.Verifying)
            {
                f.State = VerifyState.Aborted;
            }
        }

        AppendLog("✘ " + reason);
    }

    // ------------------------------------------------------------ 校验事件处理 --

    private void ApplyVFile(VerifyFileEvent e)
    {
        SetFileState(e.FileName, VerifyState.Verifying);
        VerifyPercent = 0;
        VerifyStatus = $"文件 {e.FileIndex}/{e.FileCount} · {e.FileName}";
        AppendLog($"▶ 校验文件 [{e.FileIndex}/{e.FileCount}] {e.FileName}");
    }

    private void ApplyVProg(VerifyProgressEvent e)
    {
        VerifyPercent = e.Total > 0 ? (double)e.Done / e.Total * 100 : 0;
        VerifyStatus = $"文件 {e.FileIndex}/{e.FileCount} · 探测 {e.Done}/{e.Total} · 有效 {e.OkCount} · 失效 {e.BadCount}";

        var file = FindSourceFile(e.FileName);
        if (file is not null)
        {
            file.SourceCount = e.Total;
            file.GoodCount = e.OkCount;
            file.BadCount = e.BadCount;
            file.State = VerifyState.Verifying;
        }

        var vf = FindVerifyFile(e.FileName);
        if (vf is null)
        {
            vf = new VerifyFileItem { Index = e.FileIndex, Name = e.FileName };
            VerifyFiles.Add(vf);
            OnPropertyChanged(nameof(ShowVerifyingProgress));
        }

        vf.Done = e.Done;
        vf.Total = e.Total;
        vf.OkCount = e.OkCount;
        vf.BadCount = e.BadCount;
        vf.Status = "校验中";
    }

    private void ApplyVFileDone(VerifyFileDoneEvent e)
    {
        var file = FindSourceFile(e.FileName);
        if (file is not null)
        {
            file.GoodCount = e.OkCount;
            file.BadCount = e.BadCount;
            file.ElapsedSeconds = e.ElapsedSeconds;
            file.State = VerifyState.Done;
        }

        var vf = FindVerifyFile(e.FileName);
        if (vf is not null)
        {
            vf.OkCount = e.OkCount;
            vf.BadCount = e.BadCount;
            vf.Status = "完成";
        }

        VerifyStatus = $"{e.FileName} 完成 · 有效 {e.OkCount} · 失效 {e.BadCount} (耗时 {e.ElapsedSeconds:0.0}s)";
        AppendLog($"✔ {e.FileName} 校验完成: 有效 {e.OkCount} · 失效 {e.BadCount} (耗时 {e.ElapsedSeconds:0.0}s)");
    }

    private void ApplyVDeep(VerifyDeepEvent e)
    {
        VerifyPercent = e.Total > 0 ? (double)e.Done / e.Total * 100 : 0;
        VerifyStatus = $"深度校验 文件 {e.FileIndex}/{e.FileCount} · {e.FileName} · {e.Phase} ({e.Done}/{e.Total})";
    }

    /// <summary>vdone 结算: 退出校验态, 汇总; aborted 表示用户/后端中止。</summary>
    public void FinishVerify(VerifyDoneEvent e)
    {
        _totalOk = e.TotalOk;
        _totalBad = e.TotalBad;
        VerifyPercent = 0;
        IsVerifying = false;

        foreach (var f in SourceFiles)
        {
            if (f.State == VerifyState.Verifying)
            {
                f.State = e.Aborted ? VerifyState.Aborted : VerifyState.Done;
            }
        }

        if (e.Aborted)
        {
            VerifyStatus = $"校验已停止 · {e.FileCount} 文件 · 有效 {e.TotalOk} · 失效 {e.TotalBad}";
            AppendLog($"■ 校验已停止 · 有效 {e.TotalOk} · 失效 {e.TotalBad}");
        }
        else
        {
            VerifyStatus = $"校验结束 · {e.FileCount} 文件 · 有效 {e.TotalOk} · 失效 {e.TotalBad} (耗时 {e.ElapsedSeconds:0.0}s)";
            AppendLog($"■ 校验结束 · 有效 {e.TotalOk} · 失效 {e.TotalBad} (耗时 {e.ElapsedSeconds:0.0}s)");
        }
    }

    // --------------------------------------------------------------- 日志 --

    /// <summary>追加一行日志 (超上限丢最旧)。</summary>
    public void AppendLog(string? message)
    {
        if (string.IsNullOrEmpty(message))
        {
            return;
        }

        _log.Add(message);
        LogBuffer.Add(message);
        TrimLogs();
    }

    /// <summary>批量日志 (UiThrottle 排空路径, 校验日志镜像到书源页)。</summary>
    public void AppendLogs(IReadOnlyList<string> messages)
    {
        var added = false;
        foreach (var m in messages)
        {
            if (string.IsNullOrEmpty(m))
            {
                continue;
            }

            _log.Add(m);
            LogBuffer.Add(m);
            added = true;
        }

        if (added)
        {
            TrimLogs();
        }
    }

    /// <summary>日志快照 (便于断言/导出)。</summary>
    public IReadOnlyList<string> LogSnapshot() => _log.ToArray();

    private void TrimLogs()
    {
        while (_log.Count > LogCapacity)
        {
            _log.RemoveAt(0);
            LogBuffer.RemoveAt(0);
        }
    }

    // --------------------------------------------------------------- 助手 --

    private SourceFileItem? FindSourceFile(string name)
        => SourceFiles.FirstOrDefault(f => f.Name == name);

    private VerifyFileItem? FindVerifyFile(string name)
        => VerifyFiles.FirstOrDefault(f => f.Name == name);

    private void SetFileState(string name, VerifyState state)
    {
        var file = FindSourceFile(name);
        if (file is not null)
        {
            file.State = state;
        }
    }
}

/// <summary>单文件校验状态 (驱动状态点颜色与状态文字)。</summary>
public enum VerifyState
{
    /// <summary>未校验 (sources.list 初态)。</summary>
    Idle,

    /// <summary>校验中 (vfile/vprog 期间)。</summary>
    Verifying,

    /// <summary>校验完成 (vfile_done 或 vdone 落定)。</summary>
    Done,

    /// <summary>被中止 (停止/忙拒/进程退出)。</summary>
    Aborted,

    /// <summary>校验异常/失败 (备用, 当前后端未细分)。</summary>
    Failed,
}

/// <summary>书源文件清单行。由 sources.list ack 创建; 校验事件就地更新计数与状态。</summary>
public sealed class SourceFileItem : ObservableObject
{
    private bool _isChecked;
    private bool _exists;
    private int _sourceCount;
    private int _goodCount;
    private int _badCount;
    private double _elapsedSeconds;
    private VerifyState _state = VerifyState.Idle;

    public string Name { get; init; } = "";

    /// <summary>是否在搜索/校验清单内 (sources.list 的 checked)。</summary>
    public bool IsChecked
    {
        get => _isChecked;
        set
        {
            if (SetProperty(ref _isChecked, value))
            {
                OnPropertyChanged(nameof(CheckedText));
            }
        }
    }

    public string CheckedText => IsChecked ? "清单内" : "未入清单";

    /// <summary>磁盘上文件是否仍存在。</summary>
    public bool Exists
    {
        get => _exists;
        set
        {
            if (SetProperty(ref _exists, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    /// <summary>文件内书源总数 (vprog 的 total 就地更新, 未校验为 0)。</summary>
    public int SourceCount
    {
        get => _sourceCount;
        set
        {
            if (SetProperty(ref _sourceCount, value))
            {
                OnPropertyChanged(nameof(SourceCountText));
            }
        }
    }

    /// <summary>有效源数 (vprog / vfile_done 的 ok)。</summary>
    public int GoodCount
    {
        get => _goodCount;
        set
        {
            if (SetProperty(ref _goodCount, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    /// <summary>失效源数 (vprog / vfile_done 的 bad)。</summary>
    public int BadCount
    {
        get => _badCount;
        set
        {
            if (SetProperty(ref _badCount, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    /// <summary>上次校验耗时 (秒)。</summary>
    public double ElapsedSeconds
    {
        get => _elapsedSeconds;
        set
        {
            if (SetProperty(ref _elapsedSeconds, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    public VerifyState State
    {
        get => _state;
        set
        {
            if (SetProperty(ref _state, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    /// <summary>「源数」列: 未校验显示 "—"。</summary>
    public string SourceCountText => SourceCount > 0 ? SourceCount.ToString() : "—";

    /// <summary>状态文字: 缺失/未校验/校验中/有效·失效/已中止。</summary>
    public string StatusText
    {
        get
        {
            if (!Exists)
            {
                return "文件缺失";
            }

            return State switch
            {
                VerifyState.Verifying => "校验中…",
                VerifyState.Done => $"有效 {GoodCount} · 失效 {BadCount}"
                                    + (ElapsedSeconds > 0 ? $" · {ElapsedSeconds:0.0}s" : ""),
                VerifyState.Aborted => "已中止",
                VerifyState.Failed => "校验失败",
                _ => "未校验",
            };
        }
    }

    /// <summary>新一轮校验前重置计数 (保留存在性/清单标记)。</summary>
    public void ResetProgress()
    {
        SourceCount = 0;
        GoodCount = 0;
        BadCount = 0;
        ElapsedSeconds = 0;
    }
}

/// <summary>本次校验逐文件进度行 (VerifyFiles)。</summary>
public sealed class VerifyFileItem : ObservableObject
{
    private int _done;
    private int _total;
    private int _okCount;
    private int _badCount;
    private string _status = "等待";

    /// <summary>1-based 文件序号 (vprog 的 file_index)。</summary>
    public int Index { get; init; }

    public string Name { get; init; } = "";

    public int Done
    {
        get => _done;
        set
        {
            if (SetProperty(ref _done, value))
            {
                OnPropertyChanged(nameof(Line));
            }
        }
    }

    public int Total
    {
        get => _total;
        set
        {
            if (SetProperty(ref _total, value))
            {
                OnPropertyChanged(nameof(Line));
            }
        }
    }

    public int OkCount
    {
        get => _okCount;
        set
        {
            if (SetProperty(ref _okCount, value))
            {
                OnPropertyChanged(nameof(Line));
            }
        }
    }

    public int BadCount
    {
        get => _badCount;
        set
        {
            if (SetProperty(ref _badCount, value))
            {
                OnPropertyChanged(nameof(Line));
            }
        }
    }

    public string Status
    {
        get => _status;
        set
        {
            if (SetProperty(ref _status, value))
            {
                OnPropertyChanged(nameof(Line));
            }
        }
    }

    /// <summary>一行摘要: [i] 名 · 有效 a · 失效 b · 状态。</summary>
    public string Line => $"[{Index}] {Name} · 有效 {OkCount} · 失效 {BadCount} · {Status}";
}
