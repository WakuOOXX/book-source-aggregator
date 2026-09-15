using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;

namespace NovelDownloader.Converters;

/// <summary>null / 空字符串 → Collapsed, 否则 Visible。用于占位元素的显隐。</summary>
public sealed class NullToVisibilityConverter : IValueConverter
{
    public object Convert(object value, System.Type targetType, object parameter, string language)
    {
        var visible = value is not null && value is not string s || (value is string str && str.Length > 0);
        return visible ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object value, System.Type targetType, object parameter, string language)
        => throw new System.NotSupportedException();
}
