param(
    [int]$Hwnd = 0,
    [string]$By = "name",
    [string]$Value = "",
    [string]$Action = "invoke",
    [int]$MaxDepth = 16
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]

$root = $UIA::FromHandle([IntPtr]$Hwnd)
if ($null -eq $root) { Write-Output "NO ROOT"; exit 1 }

# Only match interactive controls (skip pure Text so we hit the ListItem, not its inner label).
function IsInteractive($el) {
    $n = $el.Current.ControlType.ProgrammaticName
    if ($n -match "Text|Group|Custom|Pane|ScrollBar|Tree") { return $false }
    return $true
}

function BfsFind {
    param($start, [string]$by, [string]$val, [int]$maxDepth)
    $q = New-Object System.Collections.Queue
    $q.Enqueue(@($start, 0))
    $count = 0
    while ($q.Count -gt 0) {
        $item = $q.Dequeue()
        $el = $item[0]
        $depth = $item[1]
        if ($depth -gt $maxDepth) { continue }
        $count++
        if ($count -gt 20000) { break }
        $n = $el.Current.Name
        $id = $el.Current.AutomationId
        if ($by -eq "name" -and $n -eq $val -and (IsInteractive $el)) { return $el }
        if ($by -eq "id" -and $id -eq $val -and (IsInteractive $el)) { return $el }
        $kids = $el.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($k in $kids) { $q.Enqueue(@($k, $depth + 1)) }
    }
    return $null
}

$found = BfsFind $root $By $Value $MaxDepth
if ($null -eq $found) { Write-Output "NOT FOUND ($By=$Value)"; exit 1 }
Write-Output "FOUND: name='$($found.Current.Name)' type=$($found.Current.ControlType.ProgrammaticName)"

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$dummy = $null
$target = $found
$hop = 0
while ($null -ne $target -and $hop -lt 10) {
    $selOk = $target.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$dummy)
    if ($selOk) { break }
    $invOk = $target.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$dummy)
    if ($invOk) { break }
    $target = $walker.GetParent($target)
    $hop++
}

if ($null -eq $target) { Write-Output "NO PATTERN PARENT"; exit 1 }

switch ($Action) {
    "invoke" {
        $p = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $p.Invoke()
        Write-Output "invoked"
    }
    "select" {
        $p = $target.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
        $p.Select()
        Write-Output "selected"
    }
    "info" {
        Write-Output "INFO: name='$($target.Current.Name)'"
    }
}
