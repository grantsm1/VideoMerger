# Build dist\VideoMerger.exe
#
#   .\build.ps1              tests + build
#   .\build.ps1 -SkipTests   build only
#   .\build.ps1 -CleanPcTest also run the exe end to end as on a PC without FFmpeg
#                            (downloads FFmpeg, ~110 MB, and clicks through the app on screen)

param([switch]$SkipTests, [switch]$CleanPcTest)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    if (-not $?) { throw "Python 3.12+ is needed: winget install Python.Python.3.12" }
}
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-dev.txt
if ($LASTEXITCODE) { throw "pip install failed" }

if (-not (Test-Path assets\icon.ico)) { & $py tools\make_icon.py }

if (-not $SkipTests) {
    Write-Host "Running tests..."
    & $py -m pytest -q tests
    if ($LASTEXITCODE) { throw "Tests failed - not building." }
}

Write-Host "Building..."
& $py -m PyInstaller --noconfirm --clean --log-level WARN VideoMerger.spec
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$exe = Get-Item dist\VideoMerger.exe
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"{0:N1} MB  sha256 {1}" -f ($exe.Length / 1MB), $hash
"$hash  VideoMerger.exe" | Set-Content dist\VideoMerger.exe.sha256 -Encoding ascii
Write-Host "Built $($exe.FullName)"

if ($CleanPcTest) {
    & $py tools\cleanpc_test.py dist\VideoMerger.exe "$env:TEMP\vm_cleanpc"
    if ($LASTEXITCODE) { throw "Clean-PC test failed" }
}
