using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;

namespace NovelDownloader.Converters;

/// <summary>
/// bool → SolidColorBrush: true → DangerBrush (红), false → TextPrimaryBrush (默认)。
/// 用于 GridView 列 DataTemplate 中书源列，被封源标红。
/// </summary>
public sealed class BlockedToForegroundConverter : IValueConverter
{
    private static readonly SolidColorBrush DangerBrush = new(Microsoft.UI.ColorHelper.FromArgb(255, 196, 43, 28));
    private static readonly SolidColorBrush DefaultBrush = new(Microsoft.UI.ColorHelper.FromArgb(255, 27, 27, 27));

    public object Convert(object value, Type targetType, object parameter, string language)
    {
        return value is true ? DangerBrush : DefaultBrush;
    }

    public object ConvertBack(object value, Type targetType, object parameter, string language)
        => throw new NotSupportedException();
}
