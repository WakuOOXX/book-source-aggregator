using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;
using NovelDownloader.ViewModels;

namespace NovelDownloader.Converters;

/// <summary>
/// VerifyState → 状态点颜色 (主题感知): 校验中=琥珀, 完成=绿, 中止/失败=红, 未校验=灰。
/// 从 Application.Current.Resources 取 ThemeResource 对应的 Brush, 深浅主题自动适配。
/// </summary>
public sealed class VerifyStateToBrushConverter : IValueConverter
{
    public object Convert(object value, System.Type targetType, object parameter, string language)
    {
        var state = value is VerifyState s ? s : VerifyState.Idle;
        var key = state switch
        {
            VerifyState.Verifying => "WarningBrush",
            VerifyState.Done => "SuccessBrush",
            VerifyState.Aborted or VerifyState.Failed => "DangerBrush",
            _ => "TextTertiaryBrush",
        };

        if (Application.Current?.Resources.TryGetValue(key, out var res) == true && res is Brush brush)
        {
            return brush;
        }

        return new SolidColorBrush(Windows.UI.Color.FromArgb(255, 140, 140, 140));
    }

    public object ConvertBack(object value, System.Type targetType, object parameter, string language)
        => throw new System.NotSupportedException();
}
