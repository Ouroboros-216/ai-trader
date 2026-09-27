param([string]$MetaEditor = 'C:\Program Files\MetaTrader 5\MetaEditor64.exe')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$source = Join-Path $root 'mql5\AITrader.mq5'
$log = Join-Path $root 'mql5\AITrader.compile.log'
Start-Process -FilePath $MetaEditor -ArgumentList @("/compile:`"$source`"", "/log:`"$log`"") -WindowStyle Hidden -Wait
if (!(Test-Path -LiteralPath $log)) { throw 'Compiler did not produce a log' }
$result = Get-Content -LiteralPath $log -Raw
if ($result -notmatch '0 errors, 0 warnings') { throw $result }
Write-Output 'AITrader compiled: 0 errors, 0 warnings'
