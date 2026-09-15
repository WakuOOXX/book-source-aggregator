namespace NovelDownloader.ViewModels;

/// <summary>登录头页 VM。M0 占位, M4 实现 (auth 命令族 + CDP 抓取事件)。</summary>
public sealed class AuthViewModel : ObservableObject
{
    private bool _isCapturing;

    public bool IsCapturing
    {
        get => _isCapturing;
        set => SetProperty(ref _isCapturing, value);
    }
}
