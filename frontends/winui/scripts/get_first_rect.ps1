param([int]$Hwnd = 0)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
$root = $UIA::FromHandle([IntPtr]$Hwnd)
$condList = [System.Windows.Automation.PropertyCondition]::new($UIA::AutomationIdProperty, "AuthSourceList")
$list = $root.FindFirst($Tree::Descendants, $condList)
if ($null -eq $list) { Write-Output "NO LIST"; exit 1 }
$first = $list.FindFirst($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition)
if ($null -eq $first) { Write-Output "NO ITEM"; exit 1 }
$pt = [System.Windows.Point]::new(0,0)
$ok = $first.TryGetClickablePoint([ref]$pt)
Write-Output ("clickable ok=$ok pt=$($pt.X),$($pt.Y)")
$r = $first.Current.BoundingRectangle
Write-Output ("rect=$r")
