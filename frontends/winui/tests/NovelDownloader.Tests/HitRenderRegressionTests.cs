using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using NovelDownloader.Models;
using NovelDownloader.Services.Backend;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Tests;

/// <summary>
/// hit 渲染路径的回归测试。
/// 结构守卫 (<see cref="MainPage_ResultsArea_IsWinUITable_NotWpfGridView"/>) 确保结果区用
/// WinUI 3 的「ListView + ItemTemplate 定宽 Grid」表格写法, 且**绝不**再误引入 WPF 专有的
/// ListView.View / GridViewColumn —— 那正是曾导致 XamlCompiler 失败 (WinUI 3 无此类型) 的根因。
/// 其余用例覆盖: 空/缺字段 payload 的转换不抛、snake_case 字段与 bookSourceName 映射正确。
/// </summary>
public class HitRenderRegressionTests
{
    private static readonly JsonSerializerOptions Opts = new()
    {
        PropertyNameCaseInsensitive = false,
    };

    private static HitDto Dto(string json) => JsonSerializer.Deserialize<HitDto>(json, Opts)!;

    // ------------------------------------------------- HitDto 反序列化映射 --

    [Fact]
    public void HitDto_MapsSnakeCaseFieldsAndBookSourceName()
    {
        const string json = """
        {"source":{"bookSourceName":"起点中文网","searchUrl":"https://x/?q={key}"},
         "name":"诡秘之主","author":"爱潜水的乌贼","kind":"玄幻",
         "book_url":"https://x/book/1","last_chapter":"第1450章 结局","intro":"简介"}
        """;

        var dto = Dto(json);

        Assert.Equal("诡秘之主", dto.Name);
        Assert.Equal("爱潜水的乌贼", dto.Author);
        Assert.Equal("玄幻", dto.Kind);
        Assert.Equal("https://x/book/1", dto.BookUrl);
        Assert.Equal("第1450章 结局", dto.LastChapter);
        Assert.Equal("简介", dto.Intro);
        Assert.Equal("起点中文网", dto.SourceName);
    }

    [Fact]
    public void HitDto_MissingSource_YieldsEmptySourceName_NoThrow()
    {
        var dto = Dto("""{"name":"某书"}""");

        Assert.Equal("", dto.SourceName);
        Assert.Equal("", dto.LastChapter);
    }

    [Fact]
    public void HitDto_SourceIsNotObject_YieldsEmptySourceName_NoThrow()
    {
        var dto = Dto("""{"name":"某书","source":"起点"}""");

        Assert.Equal("", dto.SourceName);
    }

    // ------------------------------------------------------ HitItem 健壮性 --

    [Fact]
    public void FromDto_EmptyName_InitialIsPlaceholder_NoThrow()
    {
        var item = HitItem.FromDto(Dto("""{"name":""}"""));

        Assert.Equal("?", item.Initial);
    }

    [Fact]
    public void FromDto_MissingName_UsesDefaultEmpty_NoThrow()
    {
        // name 字段缺失 → record 缺省 ""。
        var item = HitItem.FromDto(Dto("""{"author":"作者"}"""));

        Assert.Equal("", item.Name);
        Assert.Equal("?", item.Initial);
        Assert.Equal("作者", item.SubtitleLine);
    }

    [Fact]
    public void Initial_TakesFirstCharForNonEmptyName()
    {
        var item = HitItem.FromDto(Dto("""{"name":"诡秘之主"}"""));

        Assert.Equal("诡", item.Initial);
    }

    [Fact]
    public void SubtitleAndMeta_OmitEmptySegments()
    {
        var item = HitItem.FromDto(Dto("""{"name":"某书","author":"作者"}"""));

        Assert.Equal("作者", item.SubtitleLine);       // 无最新章 → 只剩作者
        Assert.Equal("", item.MetaLine);               // 无分类/书源 → 空串
    }

    [Fact]
    public void AddHit_EmptyNameItems_DoesNotThrow()
    {
        var vm = new MainViewModel();

        var ex = Record.Exception(() =>
        {
            vm.AddHit(HitItem.FromDto(Dto("""{"name":""}""")));
            vm.AddHit(HitItem.FromDto(Dto("""{"name":"","author":"A"}""")));
            vm.AddHit(HitItem.FromDto(Dto("""{"name":"诡秘之主"}""")));
        });

        Assert.Null(ex);
        Assert.Equal(3, vm.Hits.Count);
    }

    // ------------------------------- 结构守卫: 防止模板结构回归 --

    [Fact]
    public void MainPage_ResultsArea_IsWinUITable_NotWpfGridView()
    {
        var xaml = File.ReadAllText(LocateMainPageXaml());

        // 根因守卫: WinUI 3 (UWP XAML) 没有 WPF 的表格 API, 一旦误用 XamlCompiler 必挂。
        Assert.DoesNotContain("GridViewColumn", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("ListView.View", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("CellTemplate", xaml, StringComparison.Ordinal);

        // 正确写法: 结果区是 ListView + ItemTemplate 内嵌定宽 Grid。
        Assert.Contains("ListView", xaml, StringComparison.Ordinal);
        Assert.Contains("<ListView.ItemTemplate>", xaml, StringComparison.Ordinal);
        Assert.Contains("DataTemplate", xaml, StringComparison.Ordinal);

        // 5 列表头文案齐全 (表头 Grid 与行模板逐列对齐)。
        Assert.Contains("Text=\"书名\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Text=\"作者\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Text=\"分类\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Text=\"最新章节\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Text=\"书源\"", xaml, StringComparison.Ordinal);

        // 被封源标红: 书源列前景走 BlockedToForegroundConverter。
        Assert.Contains("BlockedToForegroundConverter", xaml, StringComparison.Ordinal);
    }

    /// <summary>从测试输出目录向上定位仓库内的 MainPage.xaml 源文件。</summary>
    private static string LocateMainPageXaml()
    {
        const string relative = "src/NovelDownloader/Views/MainPage.xaml";
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(dir.FullName, "src", "NovelDownloader", "Views", "MainPage.xaml");
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException($"未能从 {AppContext.BaseDirectory} 向上找到 {relative}");
    }
}
