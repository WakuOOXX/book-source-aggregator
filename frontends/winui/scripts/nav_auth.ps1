param([int]$Hwnd = 0)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
if ($null -eq $root) { Write-Output "NO ROOT"; exit 1 }
$Tree = [System.Windows.Automation.TreeScope]

# Bounded children-only walk (small nodes at shallow depth), find by AutomationId.
function FindByIdShallow($el, [string]$id, [int]$maxDepth) {
    $q = New-Object System.Collections.Queue
    $q.Enqueue(@($el, 0))
    while ($q.Count -gt 0) {
        $item = $q.Dequeue()
        $c = $item[0]; $d = $item[1]
        if ($d -gt $maxDepth) { continue }
        if ($c.Current.AutomationId -eq $id) { return $c }
        $kids = $c.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($k in $kids) { $q.Enqueue(@($k, $d + 1)) }
    }
    return $null
}

$nv = FindByIdShallow $root "NavView" 5
if ($null -eq $nv) { Write-Output "NO NAVVIEW"; exit 1 }
$pr = FindByIdShallow $nv "PaneRoot" 3
if ($null -eq $pr) { Write-Output "NO PANEROOT"; exit 1 }
$msv = FindByIdShallow $pr "MenuItemsScrollViewer" 3
if ($null -eq $msv) { Write-Output "NO SCROLLER"; exit 1 }
$hostEl = FindByIdShallow $msv "MenuItemsHost" 3
if ($null -eq $hostEl) { Write-Output "NO HOST"; exit 1 }

$kids = $hostEl.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition)
Write-Output ("nav item count=" + $kids.Count)
for ($i = 0; $i -lt $kids.Count; $i++) {
    Write-Output ("  [$i] name='{0}' type={1}" -f $kids[$i].Current.Name, $kids[$i].Current.ControlType.ProgrammaticName)
}
if ($kids.Count -lt 3) { Write-Output "TOO FEW NAV ITEMS"; exit 1 }
$item = $kids[2]
$p = $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
$p.Select()
Write-Output "SELECTED nav item[2] (auth)"
