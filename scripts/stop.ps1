$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$runtime = Join-Path $root 'runtime'
if (!(Test-Path -LiteralPath $runtime -PathType Container)) { throw 'Runtime directory does not exist' }
[IO.File]::WriteAllText((Join-Path $runtime 'stop.request'), 'stop')
$local = Join-Path $root 'config\local.json'
if (Test-Path -LiteralPath $local) {
    $settings = Get-Content -LiteralPath $local -Raw | ConvertFrom-Json
    $bridge = [Environment]::ExpandEnvironmentVariables($settings.bridge_dir)
    if (Test-Path -LiteralPath $bridge -PathType Container) {
        [IO.File]::WriteAllText((Join-Path $bridge 'stop.request'), 'stop')
    }
}
Write-Output 'Graceful stop requested for all accounts. Existing broker SL and EA protection remain active.'
