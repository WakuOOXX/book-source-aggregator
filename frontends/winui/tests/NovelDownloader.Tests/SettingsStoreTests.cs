using System;
using System.IO;
using NovelDownloader.Services;

namespace NovelDownloader.Tests;

/// <summary>
/// 本地设置存储单测: 主题 / 首启引导标记的默认值、跨实例持久化。
/// 目录注入临时路径, 不触碰真实 %LOCALAPPDATA%。
/// </summary>
public class SettingsStoreTests
{
    private static string TempDir()
    {
        var dir = Path.Combine(Path.GetTempPath(), "novel-settings-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    [Fact]
    public void MissingFile_DefaultsToSystemThemeAndNotSeen()
    {
        var dir = TempDir();
        var store = new SettingsStore(dir);

        Assert.Equal(ThemeMode.System, store.Theme);
        Assert.False(store.SeenOnboarding);
        Assert.False(File.Exists(store.SettingsFilePath));
    }

    [Fact]
    public void SetTheme_PersistsAcrossInstances()
    {
        var dir = TempDir();

        var store1 = new SettingsStore(dir);
        store1.SetTheme(ThemeMode.Dark);

        var store2 = new SettingsStore(dir);
        Assert.Equal(ThemeMode.Dark, store2.Theme);

        // 落盘文件存在且可反序列化。
        Assert.True(File.Exists(store1.SettingsFilePath));
    }

    [Fact]
    public void MarkOnboardingSeen_PersistsAcrossInstances()
    {
        var dir = TempDir();

        var store1 = new SettingsStore(dir);
        store1.MarkOnboardingSeen();

        var store2 = new SettingsStore(dir);
        Assert.True(store2.SeenOnboarding);
    }

    [Fact]
    public void ThemeAndOnboarding_ShareOneFile()
    {
        var dir = TempDir();

        var store1 = new SettingsStore(dir);
        store1.SetTheme(ThemeMode.Light);
        store1.MarkOnboardingSeen();

        var store2 = new SettingsStore(dir);
        Assert.Equal(ThemeMode.Light, store2.Theme);
        Assert.True(store2.SeenOnboarding);
    }

    [Fact]
    public void CorruptedFile_FallsBackToDefaultsWithoutThrowing()
    {
        var dir = TempDir();
        File.WriteAllText(Path.Combine(dir, "settings.json"), "{ not valid json !!!");

        var store = new SettingsStore(dir);

        Assert.Equal(ThemeMode.System, store.Theme);
        Assert.False(store.SeenOnboarding);
    }
}
