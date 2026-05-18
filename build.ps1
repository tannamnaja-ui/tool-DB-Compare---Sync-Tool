$ErrorActionPreference = 'Stop'
$InnoSetup = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

Write-Host "=== DB Compare & Sync Tool - Build Installer ===" -ForegroundColor Cyan

# 1. Install dependencies
Write-Host "`n[1/4] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet
pip install pyinstaller --upgrade --quiet
Write-Host "Dependencies installed." -ForegroundColor Green

# 2. Clean previous build
Write-Host "`n[2/4] Cleaning previous build..." -ForegroundColor Yellow
if (Test-Path "dist")             { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build")            { Remove-Item -Recurse -Force "build" }
if (Test-Path "installer_output") { Remove-Item -Recurse -Force "installer_output" }
Write-Host "Clean done." -ForegroundColor Green

# 3. Build exe with PyInstaller
Write-Host "`n[3/4] Building exe with PyInstaller (this may take a few minutes)..." -ForegroundColor Yellow
pyinstaller db_compare_sync.spec

if (-not (Test-Path "dist\tool-DB-Compare-Sync.exe")) {
    Write-Host "ERROR: PyInstaller build failed!" -ForegroundColor Red
    exit 1
}
$exeSize = [math]::Round((Get-Item "dist\tool-DB-Compare-Sync.exe").Length / 1MB, 1)
Write-Host "Exe built: dist\tool-DB-Compare-Sync.exe ($exeSize MB)" -ForegroundColor Green

# 4. Build installer with Inno Setup
Write-Host "`n[4/4] Building installer with Inno Setup..." -ForegroundColor Yellow
if (-not (Test-Path $InnoSetup)) {
    Write-Host "WARNING: Inno Setup not found at $InnoSetup" -ForegroundColor Yellow
    Write-Host "Skipping installer build. You can distribute dist\tool-DB-Compare-Sync.exe directly." -ForegroundColor Yellow
} else {
    New-Item -ItemType Directory -Path "installer_output" -Force | Out-Null
    & $InnoSetup "installer\setup.iss"

    $setupExe = "installer_output\tool-DB-Compare-Sync-Setup.exe"
    if (Test-Path $setupExe) {
        $setupSize = [math]::Round((Get-Item $setupExe).Length / 1MB, 1)
        Write-Host "Installer built: $setupExe ($setupSize MB)" -ForegroundColor Green
    } else {
        Write-Host "ERROR: Inno Setup build failed!" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`n=== Build Complete ===" -ForegroundColor Green
Write-Host "Output files:" -ForegroundColor Cyan
if (Test-Path "dist\tool-DB-Compare-Sync.exe") {
    Write-Host "  App:       dist\tool-DB-Compare-Sync.exe" -ForegroundColor White
}
if (Test-Path "installer_output\tool-DB-Compare-Sync-Setup.exe") {
    Write-Host "  Installer: installer_output\tool-DB-Compare-Sync-Setup.exe" -ForegroundColor White
}
