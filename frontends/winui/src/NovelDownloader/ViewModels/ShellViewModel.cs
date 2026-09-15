namespace NovelDownloader.ViewModels;

/// <summary>
/// 外壳 VM: 当前页面标识、窗口标题、全局忙碌/进度状态。
/// M0 为占位实现。
/// </summary>
public sealed class ShellViewModel : ObservableObject
{
    private string _currentTag = "search";
    private string _title = "小说下载器";

    /// <summary>当前导航目的地 Tag: search / sources / auth / log / settings。</summary>
    public string CurrentTag
    {
        get => _currentTag;
        set => SetProperty(ref _currentTag, value);
    }

    public string Title
    {
        get => _title;
        set => SetProperty(ref _title, value);
    }
}
