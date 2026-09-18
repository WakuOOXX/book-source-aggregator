using System;
using System.IO;
using NovelDownloader.Services;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// 日志与设置页 VM 单测: 主题初值 / SetTheme 持久化 / ApplyHello 关于区字段 /
/// 数据目录 ack 状态机 (忙态、迁移、清理、错误解锁)。
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
    public void AppVersion_IsAssemblyMajorMinor()
    {
        // csproj <Version> 驱动 (需求 12/13): 显示 v主.次。
        var vm = new LogSettingsViewModel();

        Assert.Matches(@"^v\d+\.\d+$", vm.AppVersion);
    }

    [Fact]
    public void DataDir_EmptyUntilHello()
    {
        var vm = new LogSettingsViewModel();

        Assert.Equal("", vm.DataDir);
        Assert.Equal("后端未连接", vm.DataDirDisplay);
    }

    [Fact]
    public void ApplyHello_FillsDataDirFromHello()
    {
        var vm = new LogSettingsViewModel();

        vm.ApplyHello(new BackendHello("1.31", Js: true, Sources: 1, Files: 1, Checked: 1,
            Auth: 0, SourceDir: "shuyuan", DataDir: @"D:\bookdata", DefaultOut: @"D:\bookdata\downloads"));

        Assert.Equal(@"D:\bookdata", vm.DataDir);
        Assert.Equal(@"D:\bookdata", vm.DataDirDisplay);
    }

    [Fact]
    public void BeginCommand_LocksButtons_UntilAck()
    {
        var vm = new LogSettingsViewModel();

        vm.BeginCommand("cache.clear");
        Assert.True(vm.IsBusy);
        Assert.False(vm.CanEdit);
        Assert.Equal("正在清除缓存…", vm.BusyHint);

        vm.OnClearAck("cache.clear", new ClearResult { Deleted = new[] { "a.good.json" }, Note = "done" });
        Assert.False(vm.IsBusy);
        Assert.True(vm.CanEdit);
    }

    [Fact]
    public void SetDirAck_UpdatesDir_Persists_FiresEvent()
    {
        var dir = TempDir();
        var vm = new LogSettingsViewModel(new SettingsStore(dir));
        DataSetDirResult? migrated = null;
        vm.DirMigrated += r => migrated = r;

        vm.BeginCommand("data.setdir");
        vm.OnSetDirAck(new DataSetDirResult
        {
            Moved = new[] { "shuyuan", "downloads" },
            Paths = new DataDirPaths { DataDir = @"E:\newdata", DefaultOut = @"E:\newdata\downloads" },
        });

        Assert.False(vm.IsBusy);
        Assert.Equal(@"E:\newdata", vm.DataDir);
        Assert.NotNull(migrated);
        // 页面 ack 回调里持久化 (与真实页面同路径):
        vm.SaveDataDir(@"E:\newdata");
        Assert.Equal(@"E:\newdata", new SettingsStore(dir).DataDir);
    }

    [Fact]
    public void BackendError_UnlocksOnlyMatchingCommand()
    {
        var vm = new LogSettingsViewModel();
        vm.BeginCommand("data.setdir");

        // 别的命令的错误不应误解锁在途的 data.setdir。
        vm.OnBackendError("cache.clear", "不相关错误");
        Assert.True(vm.IsBusy);

        vm.OnBackendError("data.setdir", "迁移失败");
        Assert.False(vm.IsBusy);
    }

    // ------------------------------------------------- EventRouter 投递链路 --

    [Fact]
    public void EventRouter_DataSetDirAck_UnlocksAndUpdatesDir()
    {
        using var router = new EventRouter(new BackendClient(), dispatcher: null);
        var vm = new LogSettingsViewModel();
        router.Attach(vm);
        vm.BeginCommand("data.setdir");

        router.RouteRawLine(
            """{"type":"ack","cmd":"data.setdir","moved":["shuyuan"],"kept_old":[],"migrate":true,"data_dir":"E:\\new","source_dir":"","default_source":"","default_out":"E:\\new\\downloads","state_file":"","auth_profile_dir":""}""");

        Assert.False(vm.IsBusy);
        Assert.Equal(@"E:\new", vm.DataDir);
    }

    [Fact]
    public void EventRouter_BusyOnSettingsCmd_Unlocks()
    {
        using var router = new EventRouter(new BackendClient(), dispatcher: null);
        var vm = new LogSettingsViewModel();
        router.Attach(vm);
        vm.BeginCommand("data.clear");

        router.RouteRawLine("""{"type":"busy","cmd":"data.clear","running":"search"}""");

        Assert.False(vm.IsBusy);
    }

    [Fact]
    public void EventRouter_ErrorOnSettingsCmd_Unlocks()
    {
        using var router = new EventRouter(new BackendClient(), dispatcher: null);
        var vm = new LogSettingsViewModel();
        router.Attach(vm);
        vm.BeginCommand("data.setdir");

        router.RouteRawLine("""{"type":"error","cmd":"data.setdir","message":"迁移失败已回滚"}""");

        Assert.False(vm.IsBusy);
    }
}
