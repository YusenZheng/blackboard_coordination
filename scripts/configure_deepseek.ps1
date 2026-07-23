param(
    [ValidateSet("deepseek-v4-flash", "deepseek-v4-pro")]
    [string]$Model = "deepseek-v4-flash"
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path $repoRoot ".env.local"
$credential = Get-Credential `
    -UserName "deepseek" `
    -Message "Paste the complete DeepSeek sk-... API key into the Password field."

if ($null -eq $credential) {
    throw "DeepSeek configuration cancelled"
}

$plainKey = $credential.GetNetworkCredential().Password

try {
    $plainKey = $plainKey.Trim()
    if ($plainKey -notmatch '^sk-[A-Za-z0-9_-]{20,}$') {
        throw "DeepSeek API Key is incomplete; paste the complete sk-... value"
    }
    $lines = @(
        "DEEPSEEK_API_KEY=$plainKey",
        "DEEPSEEK_BASE_URL=https://api.deepseek.com",
        "DEEPSEEK_MODEL=$Model",
        "DEEPSEEK_TIMEOUT_S=30",
        "DEEPSEEK_MAX_TOKENS=2048"
    )
    [IO.File]::WriteAllLines(
        $target,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )
    Write-Host "DeepSeek local configuration saved to .env.local (Git ignored)."
}
finally {
    $plainKey = $null
    $credential = $null
}
