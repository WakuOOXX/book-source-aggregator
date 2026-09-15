namespace NovelDownloader.ViewModels;

/// <summary>书源管理页 VM。M0 占位, M3 实现 (vfile/vprog/vfile_done/vdeep/vdone)。</summary>
public sealed class SourcesViewModel : ObservableObject
{
    private int _okCount;
    private int _badCount;
    private bool _isVerifying;

    public int OkCount
    {
        get => _okCount;
        set => SetProperty(ref _okCount, value);
    }

    public int BadCount
    {
        get => _badCount;
        set => SetProperty(ref _badCount, value);
    }

    public bool IsVerifying
    {
        get => _isVerifying;
        set => SetProperty(ref _isVerifying, value);
    }
}
