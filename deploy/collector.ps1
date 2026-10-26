# Windows: register an hourly Task Scheduler job for the collector. Run once in PowerShell (as your user).
$repo = "C:\path\to\claude-usage-tracker"
$py   = (Get-Command python).Source
$act  = New-ScheduledTaskAction -Execute $py -Argument "collector\collect.py run" -WorkingDirectory $repo
$trg  = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "ClaudeUsageCollector" -Action $act -Trigger $trg -Description "Ship Claude Code usage to the tracker" -Force
