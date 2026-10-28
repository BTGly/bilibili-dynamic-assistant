$ErrorActionPreference = "Stop"
$root = (Get-Item -LiteralPath $env:BINGGO_ROOT).FullName
$py = Join-Path $root "bilibinggo\.venv\Scripts\pythonw.exe"
$script = Join-Path $root "autostart_binggo.py"
$argLine = "-u `"$script`""

if (-not (Test-Path -LiteralPath $py)) { throw "pythonw not found: $py" }
if (-not (Test-Path -LiteralPath $script)) { throw "guard script not found: $script" }

$action = New-ScheduledTaskAction -Execute $py -Argument $argLine -WorkingDirectory $root

$logon = New-ScheduledTaskTrigger -AtLogOn
$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "BinggoGuard" -Action $action `
    -Trigger @($logon, $every15) -Settings $settings `
    -Description "Keep the local Binggo dashboard running" -Force | Out-Null

$state = Get-ScheduledTask -TaskName "BinggoGuard"
Write-Output ("registered: " + $state.TaskName + " state=" + $state.State)
foreach ($t in $state.Triggers) { Write-Output ("trigger: " + $t.CimClass.CimClassName) }
Write-Output ("execute: " + $action.Execute)
Write-Output ("argument: " + $action.Arguments)
