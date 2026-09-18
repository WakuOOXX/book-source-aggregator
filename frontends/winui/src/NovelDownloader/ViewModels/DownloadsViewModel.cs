using System;
using System.Collections.ObjectModel;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 下载页 VM: 展示下载目录里的 TXT/EPUB 文件 (downloads.list ack)。
/// 纯逻辑, 不引用 WinUI 类型, 所有写入由 EventRouter 在 UI 线程调用。
///
/// 路由 (EventRouter):
///   downloads.list ack  → ApplyList / FinishList
///   downloads.delete ack → OnDeleteAck (就地移除该行)
///   downloads.* error/busy → OnBackendError (解锁 + 状态栏显示原因)
/// </summary>
public sealed class DownloadsViewModel : ObservableObject
{
    private string _dir = "";
    private bool _isBusy;
    private string _status = "就绪";

    /// <summary>已下载文件行 (按下载时间倒序, 后端排好)。</summary>
    public ObservableCollection<DownloadBookRow> Files { get; } = new();

    public DownloadsViewModel()
    {
        Files.CollectionChanged += (_, _) => OnPropertyChanged(nameof(IsEmpty));
    }

    /// <summary>空目录提示 (列表为空时显示)。</summary>
    public bool IsEmpty => Files.Count == 0;

    /// <summary>下载目录 (downloads.list ack 的 dir)。</summary>
    public string Dir
    {
        get => _dir;
        private set => SetProperty(ref _dir, value);
    }

    /// <summary>是否有 downloads.* 命令在途 (刷新/删除期间锁按钮)。</summary>
    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            if (SetProperty(ref _isBusy, value))
            {
                OnPropertyChanged(nameof(CanEdit));
            }
        }
    }

    public bool CanEdit => !_isBusy;

    /// <summary>状态栏文字。</summary>
    public string Status
    {
        get => _status;
        private set => SetProperty(ref _status, value);
    }

    /// <summary>发出 downloads.list 前调用。</summary>
    public void BeginList()
    {
        IsBusy = true;
        Status = "正在读取下载目录…";
    }

    /// <summary>ack 形状不符 (解析失败) 时兜底解锁。</summary>
    public void FinishList()
    {
        IsBusy = false;
        if (Status == "正在读取下载目录…")
        {
            Status = "下载目录读取失败";
        }
    }

    public void ApplyList(DownloadsListResult result)
    {
        Dir = result.Dir;
        Files.Clear();
        foreach (var dto in result.Items)
        {
            Files.Add(DownloadBookRow.From(dto));
        }

        IsBusy = false;
        Status = Files.Count == 0 ? "下载目录里还没有书" : $"共 {Files.Count} 本";
    }

    /// <summary>downloads.delete ack: 按路径移除对应行。</summary>
    public void OnDeleteAck(string path)
    {
        IsBusy = false;
        for (var i = 0; i < Files.Count; i++)
        {
            if (string.Equals(Files[i].Path, path, StringComparison.OrdinalIgnoreCase))
            {
                Status = $"已删除「{Files[i].Name}」";
                Files.RemoveAt(i);
                return;
            }
        }
    }

    public void OnBackendError(string cmd, string message)
    {
        IsBusy = false;
        Status = $"✘ 操作失败: {message}";
    }

    /// <summary>后端进程退出等全局中断: 复位忙碌态。</summary>
    public void AbortAll(string reason)
    {
        IsBusy = false;
        Status = reason;
    }
}

/// <summary>下载页列表一行: 书名 / 格式徽标 / 大小 / 下载时间。</summary>
public sealed record DownloadBookRow(string Name, string File, string Path, string Ext, string SizeText, string ModifiedText)
{
    public static DownloadBookRow From(DownloadItemDto dto) => new(
        dto.Name, dto.File, dto.Path, dto.Ext, FormatSize(dto.Size), dto.Modified.ToString("yyyy-MM-dd HH:mm"));

    private static string FormatSize(long bytes)
    {
        if (bytes >= 1024 * 1024)
        {
            return $"{bytes / 1024.0 / 1024.0:F1} MB";
        }

        return bytes >= 1024 ? $"{bytes / 1024.0:F0} KB" : $"{bytes} B";
    }
}
