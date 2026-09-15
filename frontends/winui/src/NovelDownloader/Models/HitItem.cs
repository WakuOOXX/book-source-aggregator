using System.Collections.Generic;
using System.Linq;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.Models;

/// <summary>
/// 结果卡片数据项 (绑定到 ItemsView)。由 hit 事件的 HitDto 转换而来。
/// 分组注记 (IsGroupContinuation / IsAltGroup) 由 MainViewModel 在 AddHit 时写入:
/// 增量 hit 流不带引擎的同书分组字段, 前端按"相邻同名"自建分组 (§6.2 同书分组色条)。
/// </summary>
public sealed class HitItem
{
    public required string Name { get; init; }
    public string Author { get; init; } = "";
    public string Kind { get; init; } = "";
    public string BookUrl { get; init; } = "";
    public string LastChapter { get; init; } = "";
    public string SourceName { get; init; } = "";

    /// <summary>被封源 (深度判定试搜未通过)。M1 后端 hit payload 无该字段, 恒 false。</summary>
    public bool Blocked { get; init; }

    /// <summary>是否为同书分组的非首项 (左侧加色条 + 缩进)。</summary>
    public bool IsGroupContinuation { get; set; }

    /// <summary>同书分组的交替色 (替代旧 tk 版的 grp1/grp2 交替底色)。</summary>
    public bool IsAltGroup { get; set; }

    /// <summary>非首项 + 交替组 → 左侧 Accent 色条。</summary>
    public bool IsStripeAlt => IsGroupContinuation && IsAltGroup;

    /// <summary>非首项 + 非交替组 → 左侧中性色条。</summary>
    public bool IsStripePlain => IsGroupContinuation && !IsAltGroup;

    /// <summary>首字母封面块 (按书名取首字)。</summary>
    public string Initial => string.IsNullOrEmpty(Name) ? "?" : Name[..1];

    /// <summary>作者 · 最新章节。</summary>
    public string SubtitleLine => string.IsNullOrEmpty(LastChapter)
        ? Author
        : $"{Author} · 最新:{LastChapter}";

    /// <summary>分类 · 书源 (空段自动省略)。</summary>
    public string MetaLine => string.Join(" · ", new[] { Kind, SourceName }.Where(s => !string.IsNullOrEmpty(s)));

    public static HitItem FromDto(HitDto dto) => new()
    {
        Name = dto.Name,
        Author = dto.Author,
        Kind = dto.Kind,
        BookUrl = dto.BookUrl,
        LastChapter = dto.LastChapter,
        SourceName = dto.SourceName,
        Blocked = dto.Blocked,
    };

    public static IReadOnlyList<HitItem> FromDtos(IEnumerable<HitDto> dtos)
    {
        var list = new List<HitItem>();
        foreach (var d in dtos)
        {
            list.Add(FromDto(d));
        }

        return list;
    }
}
