using Microsoft.UI.Xaml.Controls;

namespace NovelDownloader.Controls;

/// <summary>
/// 结果卡片 (计划 §6.2 行高 56: 左首字母封面块 / 中书名·作者·最新章节 / 右书源·状态标)。
/// M0: 空占位控件, 保证 Controls/ 目录与命名空间就位; M2 实现模板与选择态。
/// </summary>
public sealed partial class BookResultCard : Control
{
    public BookResultCard()
    {
        DefaultStyleKey = typeof(BookResultCard);
    }
}
