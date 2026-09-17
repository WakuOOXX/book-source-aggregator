param([int]$Hwnd = 0)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WinRect {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[WinRect+RECT]$r = New-Object WinRect+RECT
[WinRect]::GetWindowRect([IntPtr]$Hwnd, [ref]$r) | Out-Null
Write-Output ("window rect: {0},{1} - {2},{3}  size={4}x{5}" -f $r.Left, $r.Top, $r.Right, $r.Bottom, ($r.Right - $r.Left), ($r.Bottom - $r.Top))
[WinRect]::SetForegroundWindow([IntPtr]$Hwnd) | Out-Null
Write-Output "foreground set"
