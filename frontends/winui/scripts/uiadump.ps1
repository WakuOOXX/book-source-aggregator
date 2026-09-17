param(
    [int]$Hwnd = 0,
    [int]$MaxDepth = 8
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
if ($null -eq $root) { Write-Output "NO ROOT"; exit 1 }

function Dump([System.Windows.Automation.AutomationElement]$el, [int]$depth) {
    if ($depth -gt $MaxDepth) { return }
    $ct = $el.Current.ControlType.ProgrammaticName
    $name = $el.Current.Name
    $aid = $el.Current.AutomationId
    $ctr = $el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$null)
    $val = ""
    if ($ctr) {
        try { $val = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch {}
    }
    if ($name -or $aid -or $val) {
        $line = ("  " * $depth) + "[$ct] name='$name' id='$aid'"
        if ($val) { $line += " value='$val'" }
        Write-Output $line
    }
    $kids = $el.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($k in $kids) { Dump $k ($depth + 1) }
}

Dump $root 0
