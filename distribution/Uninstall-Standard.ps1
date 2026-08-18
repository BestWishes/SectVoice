$ErrorActionPreference = 'Stop'
$SectVoiceRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'source\pyproject.toml')) { $PSScriptRoot } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$env:SECTVOICE_ROOT = $SectVoiceRoot
& (Join-Path $SectVoiceRoot 'source\.venv\Scripts\python.exe') (Join-Path $SectVoiceRoot 'source\scripts\manage_package.py') uninstall standard
if ($LASTEXITCODE -ne 0) { throw 'Standard包卸载失败' }
