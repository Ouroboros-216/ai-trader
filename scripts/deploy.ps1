param([Parameter(Mandatory=$true)][string]$TerminalDataDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path (Resolve-Path -LiteralPath $TerminalDataDirectory).Path 'MQL5\Experts\AITrader'
if (!(Test-Path -LiteralPath (Join-Path $TerminalDataDirectory 'MQL5') -PathType Container)) { throw 'Choose MT5 File > Open Data Folder, not the program folder.' }
$binary = Join-Path $root 'mql5\AITrader.ex5'
if (!(Test-Path -LiteralPath $binary)) { throw 'Compile first.' }
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -LiteralPath $binary -Destination (Join-Path $target 'AITrader.ex5')
Write-Output "Copied EA to $target. Attach manually in a dedicated demo terminal; no chart/account settings were changed."
