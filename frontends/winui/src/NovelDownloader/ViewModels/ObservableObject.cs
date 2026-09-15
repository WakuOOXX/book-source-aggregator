using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace NovelDownloader.ViewModels;

/// <summary>
/// 极简 MVVM 基类 (不引入 CommunityToolkit.Mvvm, 减少 NuGet 面)。
/// M0 仅需属性变更通知; x:Bind 默认 Mode=OneTime, 绑定需写 Mode=OneWay。
/// </summary>
public abstract class ObservableObject : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;

    protected void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

    protected bool SetProperty<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value))
        {
            return false;
        }

        field = value;
        OnPropertyChanged(name);
        return true;
    }
}
