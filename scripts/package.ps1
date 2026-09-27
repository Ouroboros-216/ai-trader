$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$output = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$pythonExe = Join-Path (Split-Path $root -Parent) '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) { $pythonExe = 'python' }
Push-Location -LiteralPath $root
try {
@'
from pathlib import Path
import hashlib, json, sys, zipfile
root = Path.cwd()
target = root/'dist'/'AITrader-v0.8.4.zip'
files = [root/'README.md', root/'pyproject.toml', root/'config'/'example.json', *root.glob('*.cmd')]
for directory in ('src', 'scripts', 'docs', 'tests', 'mql5'):
    files.extend(p for p in (root/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.log','.pyc'})
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(files): z.write(p, 'ai-trader/'+p.relative_to(root).as_posix())
digest = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix('.sha256').write_text(digest+'  '+target.name+'\n', encoding='ascii')
print(json.dumps({'package':str(target),'files':len(files),'sha256':digest}))
'@ | & $pythonExe -
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
} finally { Pop-Location }
