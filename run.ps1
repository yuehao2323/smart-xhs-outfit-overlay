# Bootstrap a local runtime without changing global Python packages.
$ErrorActionPreference = 'Stop'
$runtimePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败' }
}
& $runtimePython -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("PIL") else 1)'
if ($LASTEXITCODE -ne 0) {
    & $runtimePython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '安装 Pillow 失败，请检查网络后重试' }
}
& $runtimePython (Join-Path $PSScriptRoot 'scripts\overlay.py') @args
exit $LASTEXITCODE
