# Run locally. Inputs are hidden and never echoed or written to project files.
param([switch]$SessionOnly)
$ErrorActionPreference = 'Stop'
foreach ($entry in @(@('GEMINI_API_KEY', 'Gemini API key'), @('OPENAI_API_KEY', 'OpenAI API key'), @('AI_TRADER_TELEGRAM_TOKEN', 'Telegram bot token'))) {
    $secret = Read-Host "$($entry[1]) (blank to keep existing)" -AsSecureString
    if ($secret.Length -eq 0) { continue }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        [Environment]::SetEnvironmentVariable($entry[0], $plain, 'Process')
        if (!$SessionOnly) { [Environment]::SetEnvironmentVariable($entry[0], $plain, 'User') }
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $plain = $null
        $secret.Dispose()
    }
}
Write-Output 'Credentials set. Restart an already running service to load them. No credentials were printed.'
