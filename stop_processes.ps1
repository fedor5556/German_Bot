# Surgical kill: only python processes that are BOTH inside this project folder
# (CommandLine OR ExecutablePath) AND running german_bot.py. Never name alone --
# a name-only kill once destroyed unrelated Python across the shared PC (guide s7).
$proj = (Split-Path -Parent $MyInvocation.MyCommand.Path).ToLower()

Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' } | ForEach-Object {
    $cmd = ([string]$_.CommandLine).ToLower()
    $exe = ([string]$_.ExecutablePath).ToLower()
    $inProject = $cmd.Contains($proj) -or $exe.Contains($proj)   # folder-path scope
    $isOurs    = $cmd.Contains('german_bot.py')                  # unique script name
    if ($inProject -and $isOurs) {
        Write-Host ("Stopping PID {0}" -f $_.ProcessId)
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { Write-Host $_ }
    }
}
Write-Host "Done."
