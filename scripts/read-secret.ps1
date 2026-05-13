<#
.SYNOPSIS
    PowerShell counterpart to scripts/read-secret.sh — resolves a secret from
    Windows Credential Manager via the cross-platform `keyring` package.

.DESCRIPTION
    Resolution order:
      1. keyring (service "multifleet")         — Credential Manager
      2. keyring (service = key, account = $env:USERNAME) — legacy style
      3. %USERPROFILE%\.fleet-nerve\env file
      4. Process environment variable

    Returns empty string if not found anywhere.

.EXAMPLE
    .\read-secret.ps1 DEEPSEEK_API_KEY

.EXAMPLE
    $key = (.\read-secret.ps1 MULTIFLEET_HMAC_KEY)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$KeyName
)

$ErrorActionPreference = 'SilentlyContinue'

function Invoke-KeyringGet {
    param([string]$Service, [string]$Key)
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $py) { return '' }
    $script = "import keyring,sys; v=keyring.get_password('$Service','$Key'); sys.stdout.write(v if v else '')"
    $out = & $py.Source -c $script 2>$null
    if ($LASTEXITCODE -ne 0) { return '' }
    return $out
}

# 1. keyring under multifleet service
$val = Invoke-KeyringGet -Service 'multifleet' -Key $KeyName
if ($val) { Write-Output $val; exit 0 }

# 2. keyring under the key name itself (legacy)
$val = Invoke-KeyringGet -Service $KeyName -Key $env:USERNAME
if ($val) { Write-Output $val; exit 0 }

# 3. Env file fallback
$envFile = Join-Path $env:USERPROFILE '.fleet-nerve\env'
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern "^$KeyName=" | Select-Object -First 1
    if ($line) {
        $raw = ($line.Line -split '=', 2)[1]
        $raw = $raw.Trim('"').Trim("'")
        if ($raw) { Write-Output $raw; exit 0 }
    }
}

# 4. Shell environment
$envVal = [Environment]::GetEnvironmentVariable($KeyName, 'Process')
if (-not $envVal) {
    $envVal = [Environment]::GetEnvironmentVariable($KeyName, 'User')
}
if ($envVal) { Write-Output $envVal; exit 0 }

# Not found — empty output, exit 0 (same contract as read-secret.sh)
Write-Output ''
exit 0
