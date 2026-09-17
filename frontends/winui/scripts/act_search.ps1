param(
    [int]$Hwnd = 0,
    [string]$Query = "诡秘之主",
    [int]$WaitMs = 1500
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]

$root = $UIA::FromHandle([IntPtr]$Hwnd)

$cond = [System.Windows.Automation.PropertyCondition]::new($UIA::AutomationIdProperty, "TextBox")
$box = $root.FindFirst($Tree::Descendants, $cond)
if ($null -ne $box) {
    $vp = $box.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    $vp.SetValue($Query)
    Write-Output "set search text: $Query"
} else {
    Write-Output "WARN: search TextBox not found"
}

$bcond = [System.Windows.Automation.PropertyCondition]::new($UIA::AutomationIdProperty, "SearchButton")
$btn = $root.FindFirst($Tree::Descendants, $bcond)
if ($null -ne $btn) {
    $inv = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $inv.Invoke()
    Write-Output "invoked search button"
} else {
    Write-Output "WARN: SearchButton not found"
}

Start-Sleep -Milliseconds $WaitMs
Write-Output "waited ${WaitMs}ms"
