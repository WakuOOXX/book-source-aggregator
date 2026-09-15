namespace NovelDownloader.ViewModels;

/// <summary>日志与设置页 VM。M0 占位, M1/M5 实现 (log 追加 + 主题/目录设置)。</summary>
public sealed class LogSettingsViewModel : ObservableObject
{
    private string _logText = string.Empty;

    public string LogText
    {
        get => _logText;
        set => SetProperty(ref _logText, value);
    }
}
