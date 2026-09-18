using System;
using System.IO;
using System.Text.Json;

namespace NovelDownloader.Services;

/// <summary>主题三选: 跟随系统 / 浅色 / 深色。纯逻辑层枚举 (不依赖 WinUI 类型, 可单测)。</summary>
public enum ThemeMode
{
    /// <summary>跟随系统 (ElementTheme.Default)。</summary>
    System = 0,

    /// <summary>浅色 (ElementTheme.Light)。</summary>
    Light = 1,

    /// <summary>深色 (ElementTheme.Dark)。</summary>
    Dark = 2,
}

/// <summary>
/// 本地设置存储: 主题 / 首启引导标记 / 自定义数据目录。
/// 落盘 <c>%LOCALAPPDATA%\NovelDownloader\settings.json</c> (UTF-8, 缩进)。
/// 取舍说明: 不用 Windows.Storage (ApplicationData 在 WindowsPackageType=None 下不可用),
/// 也不引第三方配置库 —— 设置项极少, 一个 JsonSerializer 足够;
/// 文件不存在 / 读损坏时全部回默认值, 绝不因配置问题挡启动。
/// 目录可注入 (directory 参数) —— 单测指向临时目录, 生产默认走 LOCALAPPDATA。
/// </summary>
public sealed class SettingsStore
{
    /// <summary>应用目录名 (LOCALAPPDATA 下)。</summary>
    public const string AppFolderName = "NovelDownloader";

    private readonly string _path;
    private ThemeMode _theme = ThemeMode.System;
    private bool _seenOnboarding;
    private string? _dataDir;

    /// <summary>持久化目录 (测试注入; 生产为 LOCALAPPDATA\NovelDownloader)。</summary>
    public string DirectoryPath { get; }

    public ThemeMode Theme
    {
        get => _theme;
        private set => _theme = value;
    }

    /// <summary>首启引导是否已展示过。</summary>
    public bool SeenOnboarding
    {
        get => _seenOnboarding;
        private set => _seenOnboarding = value;
    }

    /// <summary>用户自定义的 Python 侧数据存储目录; null = 用后端默认。</summary>
    public string? DataDir
    {
        get => _dataDir;
        private set => _dataDir = value;
    }

    public SettingsStore(string? directory = null)
    {
        DirectoryPath = directory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            AppFolderName);
        _path = Path.Combine(DirectoryPath, "settings.json");
        Load();
    }

    /// <summary>设置主题并立即持久化。</summary>
    public void SetTheme(ThemeMode theme)
    {
        if (_theme == theme)
        {
            return;
        }

        _theme = theme;
        Save();
    }

    /// <summary>标记首启引导已展示并立即持久化。</summary>
    public void MarkOnboardingSeen()
    {
        if (_seenOnboarding)
        {
            return;
        }

        _seenOnboarding = true;
        Save();
    }

    /// <summary>设置数据存储目录并立即持久化 (null/空白 = 恢复后端默认)。</summary>
    public void SetDataDir(string? path)
    {
        var norm = string.IsNullOrWhiteSpace(path) ? null : path.Trim();
        if (_dataDir == norm)
        {
            return;
        }

        _dataDir = norm;
        Save();
    }

    /// <summary>持久化文件的绝对路径 (诊断/展示用)。</summary>
    public string SettingsFilePath => _path;

    // ------------------------------------------------------------ 读写 --

    private void Load()
    {
        try
        {
            if (!File.Exists(_path))
            {
                return;
            }

            using var fs = File.OpenRead(_path);
            using var doc = JsonDocument.Parse(fs);
            var root = doc.RootElement;

            if (root.TryGetProperty("theme", out var themeEl) && themeEl.ValueKind == JsonValueKind.Number)
            {
                _theme = (ThemeMode)themeEl.GetInt32();
            }

            if (root.TryGetProperty("seen_onboarding", out var seenEl) && seenEl.ValueKind == JsonValueKind.True)
            {
                _seenOnboarding = true;
            }

            if (root.TryGetProperty("data_dir", out var dirEl) && dirEl.ValueKind == JsonValueKind.String)
            {
                var s = dirEl.GetString();
                _dataDir = string.IsNullOrWhiteSpace(s) ? null : s!.Trim();
            }
        }
        catch
        {
            // 配置损坏/不可读: 全部回默认值, 不挡启动。
            _theme = ThemeMode.System;
            _seenOnboarding = false;
            _dataDir = null;
        }
    }

    private void Save()
    {
        try
        {
            Directory.CreateDirectory(DirectoryPath);
            var json = JsonSerializer.Serialize(new
            {
                theme = (int)_theme,
                seen_onboarding = _seenOnboarding,
                data_dir = _dataDir,
            }, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(_path, json);
        }
        catch
        {
            // 写失败不致命 (只影响设置记忆), 静默。
        }
    }
}
