param([Parameter(Mandatory=$true)][string]$PackageRoot)
$ErrorActionPreference = 'Stop'

# Configuration stays outside the public ZIP. Never emit config values or a
# DPAPI plaintext into task output or logs.
$configRoot = [Environment]::GetEnvironmentVariable('JY_CONFIG_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($configRoot)) { $configRoot = Join-Path $PackageRoot 'config' }
$configFile = [Environment]::GetEnvironmentVariable('JY_ENV_CONFIG_PATH', 'Process')
if ([string]::IsNullOrWhiteSpace($configFile)) { $configFile = Join-Path $configRoot 'runtime.json' }
if (Test-Path -LiteralPath $configFile) {
  try {
    $runtimeConfig = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $runtimeConfig.environment) { throw 'environment section missing' }
    foreach ($property in $runtimeConfig.environment.PSObject.Properties) {
      $name = [string]$property.Name
      if ($name -notmatch '^(JY_|LARK_)' -or $name -eq 'NAS_PASSWORD') { continue }
      if ($null -eq $property.Value -or $property.Value -is [System.Collections.IDictionary] -or
          $property.Value -is [System.Array]) { continue }
      if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
        [Environment]::SetEnvironmentVariable($name, [string]$property.Value, 'Process')
      }
    }
  } catch { throw 'RUNTIME_CONFIG_INVALID' }
}

# This blob uses CurrentUser DPAPI; JyPoll and JyRun must use its Windows task
# identity. Decrypted data is held only in this process environment.
$nasLoaded = -not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('NAS_PASSWORD', 'Process'))
if (-not $nasLoaded) {
  $secretFile = [Environment]::GetEnvironmentVariable('JY_NAS_SECRET_FILE', 'Process')
  if ([string]::IsNullOrWhiteSpace($secretFile)) { $secretFile = Join-Path $configRoot 'nas-password.dpapi' }
  if (Test-Path -LiteralPath $secretFile) {
    $secretPtr = [IntPtr]::Zero
    $secure = $null
    try {
      $cipher = Get-Content -LiteralPath $secretFile -Raw -Encoding UTF8
      $secure = ConvertTo-SecureString $cipher
      $secretPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
      [Environment]::SetEnvironmentVariable('NAS_PASSWORD',
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPtr), 'Process')
      $nasLoaded = $true
    } catch { throw 'NAS_DPAPI_DECRYPT_FAILED' }
    finally {
      if ($secretPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPtr) }
      if ($secure) { $secure.Dispose() }
    }
  }
}
return [pscustomobject]@{ config_path = $configFile; nas_password_loaded = $nasLoaded }
