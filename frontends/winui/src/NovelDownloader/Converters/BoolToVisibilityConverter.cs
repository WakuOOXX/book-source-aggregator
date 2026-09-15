using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;

namespace NovelDownloader.Converters;

/// <summary>bool → Visibility。parameter 为 "invert" 时取反。</summary>
public sealed class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object value, System.Type targetType, object parameter, string language)
    {
        var b = value is bool v && v;
        if (parameter is string p && p.Equals("invert", System.StringComparison.OrdinalIgnoreCase))
        {
            b = !b;
        }

        return b ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object value, System.Type targetType, object parameter, string language)
        => value is Visibility vis && vis == Visibility.Visible;
}
