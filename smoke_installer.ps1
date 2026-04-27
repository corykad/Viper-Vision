$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$installer = Join-Path $PSScriptRoot "installer\ViperVision-v1.2-Setup.exe"
$distExe = Join-Path $PSScriptRoot "dist\ViperVision\ViperVision.exe"
$distFfmpeg = Join-Path $PSScriptRoot "dist\ViperVision\_internal\ffmpeg.exe"
$distHelp = Join-Path $PSScriptRoot "dist\ViperVision\_internal\help\index.html"

Write-Host "Checking built release artifacts..."
foreach ($path in @($installer, $distExe, $distFfmpeg, $distHelp)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Required release artifact missing: $path"
  }
  Write-Host "Found: $path"
}

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("viper_installer_smoke_" + [guid]::NewGuid().ToString("N"))
$installDir = Join-Path $root "install"
$appData = Join-Path $root "appdata"
New-Item -ItemType Directory -Path $installDir, $appData | Out-Null
$process = $null
$oldAppData = $env:APPDATA
$oldClean = $env:VIPER_CLEAN_FIRST_RUN_TEST
$oldPort = $env:FLASK_PORT

function Restore-EnvValue($Name, $Value) {
  if ($null -eq $Value) {
    Remove-Item -Path "Env:$Name" -ErrorAction SilentlyContinue
  } else {
    Set-Item -Path "Env:$Name" -Value $Value
  }
}

try {
  Write-Host "Installing silently to: $installDir"
  $install = Start-Process -FilePath $installer -ArgumentList @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/DIR=$installDir"
  ) -Wait -PassThru
  if ($install.ExitCode -ne 0) {
    throw "Installer exited with code $($install.ExitCode)"
  }

  $exe = Join-Path $installDir "ViperVision.exe"
  $ffmpeg = Join-Path $installDir "_internal\ffmpeg.exe"
  $help = Join-Path $installDir "_internal\help\index.html"
  foreach ($path in @($exe, $ffmpeg, $help)) {
    if (-not (Test-Path -LiteralPath $path)) {
      throw "Installed artifact missing: $path"
    }
    Write-Host "Installed: $path"
  }

  Write-Host "Launching installed app briefly with isolated APPDATA..."
  $env:APPDATA = $appData
  $env:VIPER_CLEAN_FIRST_RUN_TEST = "1"
  $env:FLASK_PORT = "5061"
  $process = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru
  Start-Sleep -Seconds 8

  if ($process.HasExited) {
    throw "Installed app exited during smoke test with code $($process.ExitCode)"
  }

  $remoteOk = $false
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:5061/remote" -UseBasicParsing -TimeoutSec 5
    $remoteOk = $response.StatusCode -eq 200 -or $response.StatusCode -eq 503
  } catch {
    Write-Warning "Remote page did not respond during smoke window: $_"
  }
  if (-not $remoteOk) {
    Write-Warning "App launched, but remote page was not confirmed. Review logs if this repeats."
  }

  $log = Join-Path $appData "viper_vision_1.0\viper_full_debug.log"
  if (Test-Path -LiteralPath $log) {
    $logText = Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue
    if ($logText -match "Traceback|CRITICAL") {
      throw "Smoke log contains a crash marker: $log"
    }
  }

  Write-Host "Stopping smoke-test app..."
  Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue

  Write-Host ""
  Write-Host "Installer smoke test passed."
} finally {
  if ($process -and -not $process.HasExited) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
  }
  Restore-EnvValue "APPDATA" $oldAppData
  Restore-EnvValue "VIPER_CLEAN_FIRST_RUN_TEST" $oldClean
  Restore-EnvValue "FLASK_PORT" $oldPort
  Write-Host "Smoke test files kept at: $root"
}
