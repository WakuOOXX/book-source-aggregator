param([int]$Hwnd = 0)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
function Dump($el, $d) {
    if ($d -gt 3) { return }
    $pad = "  " * $d
    Write-Output ("$pad type={0} name='{1}' id='{2}'" -f $el.Current.ControlType.ProgrammaticName, $el.Current.Name, $el.Current.AutomationId)
    $kids = $el.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($k in $kids) { Dump $k ($d+1) }
}
Dump $root 0
