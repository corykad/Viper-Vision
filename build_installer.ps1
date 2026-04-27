$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

.\build_exe.ps1

$isccCommand = Get-Command iscc.exe -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { "" }
if (-not $isccPath) {
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
  )
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
      $isccPath = $candidate
      break
    }
  }
}

if (-not $isccPath) {
  throw "Inno Setup 6 compiler was not found. Install it with: winget install --id JRSoftware.InnoSetup -e"
}

if (Test-Path -LiteralPath .\installer) {
  Remove-Item -LiteralPath .\installer -Recurse -Force
}

& $isccPath .\ViperVision.iss

$installer = Join-Path $PSScriptRoot "installer\ViperVision-v1.2-Setup.exe"
if (-not (Test-Path -LiteralPath $installer)) {
  throw "Installer build finished but $installer was not found."
}

Write-Host ""
Write-Host "Installer complete:"
Write-Host $installer
