param([switch]$Foreground)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
if ($Foreground) { & $pythonExe -m aitrader.multi run --root $root; exit $LASTEXITCODE }
Remove-Item -LiteralPath (Join-Path $runtime 'stop.request') -ErrorAction SilentlyContinue
$argsList = @('-m', 'aitrader.multi', 'run', '--root', "`"$root`"")
$process = Start-Process -FilePath $pythonExe -ArgumentList $argsList -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'service.out.log') -RedirectStandardError (Join-Path $runtime 'service.err.log')
Write-Output "Service launched with PID $($process.Id); check logs for startup validation."
