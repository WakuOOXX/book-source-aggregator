param([int]$Hwnd = 0, [string]$Id = "StatusText")
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
$root = $UIA::FromHandle([IntPtr]$Hwnd)
$cond = [System.Windows.Automation.PropertyCondition]::new($UIA::AutomationIdProperty, $Id)
$el = $root.FindFirst($Tree::Descendants, $cond)
if ($null -ne $el) { Write-Output "$Id = $($el.Current.Name)" } else { Write-Output "$Id = NOT FOUND" }
