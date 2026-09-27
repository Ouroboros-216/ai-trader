param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
& $Python -m venv --without-pip (Join-Path $root '.venv')
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
# Runtime uses only Python's standard library; install the source path offline.
Push-Location -LiteralPath $root
try {
@'
from pathlib import Path
import sys, sysconfig
source = Path.cwd().resolve() / 'src'
destination = Path(sysconfig.get_path('purelib')) / 'ai_trader_local.pth'
destination.write_text('import sys; sys.path.insert(0, '+ascii(str(source))+')\n', encoding='ascii')
'@ | & $pythonExe -
if ($LASTEXITCODE -ne 0) { throw 'installation failed' }
} finally { Pop-Location }
$local = Join-Path $root 'config\local.json'
if (!(Test-Path -LiteralPath $local)) { Copy-Item -LiteralPath (Join-Path $root 'config\example.json') -Destination $local }
Write-Output 'Installed. Configure local.json and environment variables, then compile and deploy to a dedicated demo terminal.'
