$ErrorActionPreference = 'Stop'
$SectVoiceRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'source\pyproject.toml')) {
    $PSScriptRoot
} else {
    Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$ReaderPythonw = Join-Path $SectVoiceRoot 'source\.venv\Scripts\pythonw.exe'
$ReaderPython = Join-Path $SectVoiceRoot 'source\.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $ReaderPythonw)) {
    throw "SectVoice Reader环境不存在。请先运行 $SectVoiceRoot\Install-SectVoice.ps1"
}

$env:SECTVOICE_ROOT = $SectVoiceRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = Join-Path $SectVoiceRoot 'cache\uv'
$env:HF_HOME = Join-Path $SectVoiceRoot 'cache\huggingface'
$env:TORCH_HOME = Join-Path $SectVoiceRoot 'cache\torch'
$env:TEMP = Join-Path $SectVoiceRoot 'temp'
$env:TMP = Join-Path $SectVoiceRoot 'temp'

Start-Process -FilePath $ReaderPythonw -ArgumentList '-m','sectvoice' -WorkingDirectory (Join-Path $SectVoiceRoot 'source') -WindowStyle Hidden
