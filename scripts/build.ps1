param(
    [string]$DistRoot = "",
    [string]$WorkRoot = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DistRoot) { $DistRoot = Join-Path $projectRoot 'dist' }
$appDir = Join-Path $DistRoot 'QYS RTOS'
$python = if ($PythonExe) { $PythonExe } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw '找不到项目虚拟环境。请先创建 .venv 并安装 requirements-build.txt。'
}
if (Test-Path -LiteralPath $appDir) {
    throw "输出目录已存在：$appDir。为避免覆盖已有文件，请选择新的 DistRoot。"
}
$workDir = if ($WorkRoot) { $WorkRoot } else { Join-Path $DistRoot '_pyinstaller-work' }
& $python -m PyInstaller --noconfirm --distpath $DistRoot --workpath $workDir `
    (Join-Path $projectRoot 'qys_rtos.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $appDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $appDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.txt') -Destination $appDir
Copy-Item -LiteralPath (Join-Path $projectRoot 'licenses') -Destination $appDir -Recurse
$source = Join-Path $appDir 'source'
$package = Join-Path $source 'qys_rtos'
New-Item -ItemType Directory -Path $package -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'qys_rtos') -File -Filter '*.py' | Copy-Item -Destination $package
foreach ($name in @('run_app.py', 'requirements.txt', 'requirements-build.txt')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $source
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'qys_rtos.spec') -Destination $source
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $source
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $source
Copy-Item -LiteralPath (Join-Path $projectRoot 'assets') -Destination $source -Recurse
New-Item -ItemType Directory -Path (Join-Path $source 'scripts') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\build.ps1') -Destination (Join-Path $source 'scripts')
Write-Host "QYS RTOS app built at: $appDir"
