$ErrorActionPreference = 'Stop'
$SectVoiceRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'source\pyproject.toml')) {
    $PSScriptRoot
} else {
    Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$SourceRoot = Join-Path $SectVoiceRoot 'source'
$ReaderPython = Join-Path $SourceRoot '.venv\Scripts\python.exe'
$Uv = Join-Path $SectVoiceRoot 'runtime\tools\uv\Scripts\uv.exe'

$env:SECTVOICE_ROOT = $SectVoiceRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = Join-Path $SectVoiceRoot 'cache\uv'
$env:HF_HOME = Join-Path $SectVoiceRoot 'cache\huggingface'
$env:TORCH_HOME = Join-Path $SectVoiceRoot 'cache\torch'
$env:TEMP = Join-Path $SectVoiceRoot 'temp'
$env:TMP = Join-Path $SectVoiceRoot 'temp'

if (-not (Test-Path -LiteralPath $Uv)) {
    throw "缺少项目局部uv：$Uv"
}
if (-not (Test-Path -LiteralPath $ReaderPython)) {
    & $Uv venv --python 3.10 (Join-Path $SourceRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建Reader Python环境失败' }
}
& $Uv pip install --python $ReaderPython -e "$SourceRoot[dev]"
if ($LASTEXITCODE -ne 0) { throw '安装Reader依赖失败' }

& $ReaderPython (Join-Path $SourceRoot 'scripts\manage_package.py') install basic
if ($LASTEXITCODE -ne 0) { throw '登记Basic包失败' }
& $ReaderPython (Join-Path $SourceRoot 'scripts\manage_package.py') install standard
if ($LASTEXITCODE -ne 0) { throw '登记Standard包失败' }
$StandardPython = Join-Path $SectVoiceRoot 'runtime\engines\standard\gpt-sovits\d523079f\.venv\Scripts\python.exe'
$env:NLTK_DATA = Join-Path $SectVoiceRoot 'cache\nltk_data'
& $StandardPython -c "import nltk; raise SystemExit(0 if nltk.download('averaged_perceptron_tagger_eng', download_dir=r'$($env:NLTK_DATA)', quiet=True) else 2)"
if ($LASTEXITCODE -ne 0) { throw '安装Standard中英混读词性资源失败' }
& $ReaderPython -m pytest -q $SourceRoot
if ($LASTEXITCODE -ne 0) { throw '自动化测试失败，未报告安装成功' }

Write-Host 'SectVoice安装检查通过。双击 Start-SectVoice.cmd 启动。' -ForegroundColor Green
