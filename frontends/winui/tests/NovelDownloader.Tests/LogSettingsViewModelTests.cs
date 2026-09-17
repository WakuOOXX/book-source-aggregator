using System;
using System.IO;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// 日志与设置页 VM 单测 (M5): 主题初值 / SetTheme 持久化 / ApplyHello 关于区字段。
/// 纯逻辑 VM, 不依赖 UI 线程。
/// </summary>
public class LogSettingsViewModelTests
{
    private static string TempDir()
    {
        var dir = Path.Combine(Path.GetTempPath(), "novel-vm-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    [Fact]
    public void DefaultTheme_IsSystem()
    {
        var vm = new LogSettingsViewModel();

        Assert.Equal(ThemeMode.System, vm.Theme);
    }

    [Fact]
    public void Constructor_ReadsThemeFromStore()
    {
        var dir = TempDir();
        var store = new SettingsStore(dir);
        store.SetTheme(ThemeMode.Dark);

        var vm = new LogSettingsViewModel(new SettingsStore(dir));

        Assert.Equal(ThemeMode.Dark, vm.Theme);
    }

    [Fact]
    public void SetTheme_UpdatesPropertyAndPersists()
    {
        var dir = TempDir();
        var store = new SettingsStore(dir);
        var vm = new LogSettingsViewModel(store);
        var raised = 0;
        vm.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(LogSettingsViewModel.Theme)) raised++; };

        vm.SetTheme(ThemeMode.Dark);

        Assert.Equal(ThemeMode.Dark, vm.Theme);
        Assert.Equal(1, raised);
        Assert.Equal(ThemeMode.Dark, new SettingsStore(dir).Theme);
    }

    [Fact]
    public void SetTheme_SameValue_NoOp()
    {
        var vm = new LogSettingsViewModel();

        vm.SetTheme(ThemeMode.System);

        Assert.Equal(ThemeMode.System, vm.Theme);
    }

    [Fact]
    public void ApplyHello_UpdatesBackendVersionAndJsStatus()
    {
        var vm = new LogSettingsViewModel();
        var hello = new BackendHello("1.9.1", Js: true, Sources: 1292, Files: 7, Checked: 7, Auth: 835, SourceDir: "shuyuan");

        vm.ApplyHello(hello);

        Assert.Equal("v1.9.1", vm.BackendVersion);
        Assert.Contains("可用", vm.JsEngineStatus);
        Assert.Contains("v1.9.1", vm.BackendSummary);
        Assert.Contains("1292", vm.BackendSummary);
    }

    [Fact]
    public void ApplyHello_JsDisabled_MarksUnavailable()
    {
        var vm = new LogSettingsViewModel();

        vm.ApplyHello(new BackendHello("1.9.1", Js: false, Sources: 0, Files: 0, Checked: 0, Auth: 0, SourceDir: ""));

        Assert.Contains("不可用", vm.JsEngineStatus);
    }

    [Fact]
    public void AppVersion_FallsBackToV19xPlaceholder()
    {
        // 主工程 csproj 未设 Version → 程序集默认 1.0.0.0 → 应回退 v1.9.x 占位。
        var vm = new LogSettingsViewModel();

        Assert.StartsWith("v1.9", vm.AppVersion);
    }

    [Fact]
    public void DataDir_IsDownloadsUnderAppBase()
    {
        var vm = new LogSettingsViewModel();

        Assert.Equal(Path.Combine(AppContext.BaseDirectory, "downloads"), vm.DataDir);
    }
}
