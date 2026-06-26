$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "Cleaning previous PyInstaller output..."
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue

Write-Host "Compiling source files..."
python -m py_compile `
  .\main.pyw `
  .\viper_config.py `
  .\viper_audio.py `
  .\viper_vision.py `
  .\viper_discovery.py `
  .\viper_diagnostics.py `
  .\viper_ha_listener.py `
  .\viper_ring_discovery.py `
  .\viper_ha_package.py `
  .\viper_hvac.py `
  .\viper_ha_client.py `
  .\viper_matter.py `
  .\viper_runtime.py `
  .\viper_system_health.py `
  .\viper_ui_diagnostics.py `
  .\viper_ui_fridge.py `
  .\viper_ui_hvac.py `
  .\viper_ui_setup_wizard.py `
  .\viper_ui_vacuum.py

Write-Host "Building ViperVision.exe..."
python -m PyInstaller .\ViperVision.spec --clean --noconfirm

$exe = Join-Path $PSScriptRoot "dist\ViperVision\ViperVision.exe"
if (-not (Test-Path -LiteralPath $exe)) {
  throw "Build finished but ViperVision.exe was not found."
}

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($ffmpeg) {
  $ffmpegDest = Join-Path $PSScriptRoot "dist\ViperVision\_internal\ffmpeg.exe"
  Copy-Item -LiteralPath $ffmpeg.Source -Destination $ffmpegDest -Force
  Write-Host "Bundled FFmpeg:"
  Write-Host $ffmpegDest
} else {
  Write-Warning "ffmpeg.exe was not found on PATH. Doorbell RTSP processing will require the user to install FFmpeg or set FFMPEG_BIN."
}

Write-Host ""
Write-Host "Build complete:"
Write-Host $exe
Write-Host ""
Write-Host "Distribute the whole folder:"
Write-Host (Join-Path $PSScriptRoot "dist\ViperVision")
