param([int]$X = 0, [int]$Y = 0, [string]$Out = "")
Add-Type -AssemblyName System.Windows.Forms
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);' -Name MC -Namespace W -PassThru | Out-Null
[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($X, $Y)
Start-Sleep -Milliseconds 200
$sig = [W.MC]
$sig::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
$sig::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Write-Output "clicked ($X,$Y)"
