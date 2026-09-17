param([int]$Hwnd = 0, [string]$Out = "clicked.txt")
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
$root = $UIA::FromHandle([IntPtr]$Hwnd)

# 用 bounded FindFirst (不做全树 BFS) 定位列表与首项
$list = $null
$condList = [System.Windows.Automation.PropertyCondition]::new($UIA::AutomationIdProperty, "AuthSourceList")
for ($i = 0; $i -lt 3 -and $null -eq $list; $i++) {
    try { $list = $root.FindFirst($Tree::Descendants, $condList) } catch { Start-Sleep -Milliseconds 300 }
}
if ($null -eq $list) { Write-Output "NO LIST"; exit 1 }
Write-Output "list found"

$first = $null
for ($i = 0; $i -lt 3 -and $null -eq $first; $i++) {
    try { $first = $list.FindFirst($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition) } catch { Start-Sleep -Milliseconds 300 }
}
if ($null -eq $first) { Write-Output "NO ITEM"; exit 1 }
$r = $first.Current.BoundingRectangle
Write-Output ("rect x={0} y={1} w={2} h={3}" -f $r.X, $r.Y, $r.Width, $r.Height)
if ($r.Width -le 0) { Write-Output "BAD RECT"; exit 1 }

$cx = [int]($r.X + $r.Width / 2)
$cy = [int]($r.Y + 10)
[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($cx, $cy)
$sig = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);' -Name M2 -Namespace W2 -PassThru
$sig::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
$sig::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Write-Output "clicked ($cx,$cy)"
Start-Sleep -Milliseconds 1000
Write-Output "done"
