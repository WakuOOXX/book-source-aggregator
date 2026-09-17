param([int]$Hwnd = 0)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class RespCheck {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool SendMessageTimeout(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam, uint fuFlags, uint uTimeout, out IntPtr lpdwResult);
    [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
}
"@
$r = [IntPtr]::Zero
$ok = [RespCheck]::SendMessageTimeout([IntPtr]$Hwnd, 0, [IntPtr]::Zero, [IntPtr]::Zero, 2, 800, [ref]$r)
$hung = [RespCheck]::IsHungAppWindow([IntPtr]$Hwnd)
Write-Output "SendMessageTimeout(WM_NULL 800ms) ok=$ok  hung=$hung"
