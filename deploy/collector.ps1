# Windows: register an hourly Task Scheduler job for the collector. Run once in PowerShell (as your user):
#   powershell -ExecutionPolicy Bypass -File deploy\collector.ps1
$repo = Split-Path $PSScriptRoot -Parent
$py   = (Get-Command python).Source
$act  = New-ScheduledTaskAction -Execute $py -Argument "collector\collect.py run" -WorkingDirectory $repo
# explicit duration: left out, some Windows builds stop repeating after one day
$trg  = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
# catch up after sleep/reboot; a stuck run can't block the next one
$set  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
          -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ClaudeUsageCollector" -Action $act -Trigger $trg -Settings $set -Description "Ship Claude Code usage to the tracker" -Force
Get-ScheduledTaskInfo ClaudeUsageCollector | Format-List LastRunTime, NextRunTime
