<#
Runs the broadest safe Viper Vision release check from the source tree.

Default mode is non-destructive:
- compiles Python files
- runs the automated unit/regression suite
- checks required release files and metadata
- verifies help/templates/build scripts are present
- checks for common private/generated files in the source folder
- runs Viper's safe automated event routing audit
- writes a timestamped report under release_check_reports

Optional switches:
-BuildInstaller       Rebuilds dist\ViperVision and installer\ViperVision-v1.2.4-Setup.exe
-SmokeInstaller      Runs the silent installer smoke test. Requires the installer to exist.
-LiveHomeAssistant   Runs read-only HA diagnostics if HA settings are available.
-All                 Runs BuildInstaller, SmokeInstaller, and LiveHomeAssistant.
#>

param(
  [switch]$BuildInstaller,
  [switch]$SmokeInstaller,
  [switch]$LiveHomeAssistant,
  [switch]$All
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if ($All) {
  $BuildInstaller = $true
  $SmokeInstaller = $true
  $LiveHomeAssistant = $true
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$reportDir = Join-Path $PSScriptRoot "release_check_reports"
$reportPath = Join-Path $reportDir "release_check_$stamp.log"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null

$script:Failures = New-Object System.Collections.Generic.List[string]
$script:Warnings = New-Object System.Collections.Generic.List[string]

function Write-Step($Message) {
  $line = "[$(Get-Date -Format 'HH:mm:ss')] $Message"
  Write-Host $line
  Add-Content -LiteralPath $reportPath -Value $line
}

function Add-Failure($Message) {
  $script:Failures.Add($Message) | Out-Null
  Write-Step "FAIL: $Message"
}

function Add-Warning($Message) {
  $script:Warnings.Add($Message) | Out-Null
  Write-Step "WARN: $Message"
}

function Invoke-LoggedCommand {
  param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][scriptblock]$Command
  )
  Write-Step "START: $Name"
  $oldErrorAction = $ErrorActionPreference
  $oldLastExitCode = $global:LASTEXITCODE
  try {
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    $output = & $Command 2>&1
    foreach ($line in $output) {
      $text = [string]$line
      Write-Host $text
      Add-Content -LiteralPath $reportPath -Value $text
    }
    $exitCode = $global:LASTEXITCODE
    if ($exitCode -ne $null -and $exitCode -ne 0) {
      throw "$Name exited with code $exitCode"
    }
    Write-Step "PASS: $Name"
  } catch {
    Add-Failure "$Name failed: $($_.Exception.Message)"
  } finally {
    $ErrorActionPreference = $oldErrorAction
    $global:LASTEXITCODE = $oldLastExitCode
  }
}

function Invoke-LoggedProcess {
  param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$FilePath,
    [Parameter(Mandatory=$true)][string[]]$Arguments
  )
  Write-Step "START: $Name"
  $safeName = ($Name -replace '[^A-Za-z0-9_-]', '_')
  $stdoutPath = Join-Path $env:TEMP "viper_${safeName}_stdout_$stamp.log"
  $stderrPath = Join-Path $env:TEMP "viper_${safeName}_stderr_$stamp.log"
  try {
    $process = Start-Process `
      -FilePath $FilePath `
      -ArgumentList $Arguments `
      -NoNewWindow `
      -Wait `
      -PassThru `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError $stderrPath

    foreach ($path in @($stdoutPath, $stderrPath)) {
      if (Test-Path -LiteralPath $path) {
        foreach ($line in Get-Content -LiteralPath $path -ErrorAction SilentlyContinue) {
          Write-Host $line
          Add-Content -LiteralPath $reportPath -Value $line
        }
      }
    }

    if ($process.ExitCode -ne 0) {
      throw "$Name exited with code $($process.ExitCode)"
    }
    Write-Step "PASS: $Name"
  } catch {
    Add-Failure "$Name failed: $($_.Exception.Message)"
  } finally {
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
  }
}

function Assert-PathExists {
  param([string]$Path, [string]$Label)
  if (Test-Path -LiteralPath $Path) {
    Write-Step "PASS: Found $Label at $Path"
  } else {
    Add-Failure "Missing $Label at $Path"
  }
}

function Assert-FileContains {
  param([string]$Path, [string]$Pattern, [string]$Label)
  if (-not (Test-Path -LiteralPath $Path)) {
    Add-Failure "Cannot check $Label because file is missing: $Path"
    return
  }
  $text = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
  if ($text -match $Pattern) {
    Write-Step "PASS: $Label"
  } else {
    Add-Failure "Expected pattern not found for $Label in $Path"
  }
}

function Assert-BuildArtifacts {
  param([switch]$Required)
  foreach ($item in @(
    @($installer, "versioned installer"),
    @($distExe, "built executable"),
    @($distFfmpeg, "bundled FFmpeg"),
    @($distHelp, "bundled help index")
  )) {
    if (Test-Path -LiteralPath $item[0]) {
      $file = Get-Item -LiteralPath $item[0]
      Write-Step "PASS: Found $($item[1]) ($($file.Length) bytes): $($item[0])"
    } elseif ($Required) {
      Add-Failure "Missing required built artifact after build: $($item[1]) at $($item[0])"
    } else {
      Add-Warning "Built artifact not present yet: $($item[1]) at $($item[0]). Use -BuildInstaller to create it."
    }
  }
}

Write-Step "Viper Vision release check started."
Write-Step "Working folder: $PSScriptRoot"
Write-Step "Report: $reportPath"

Invoke-LoggedCommand "Python compile check" {
  python -m py_compile `
    main.pyw `
    viper_audio.py `
    viper_config.py `
    viper_discovery.py `
    viper_diagnostics.py `
    viper_ha_listener.py `
    viper_ha_package.py `
    viper_ha_vm.py `
    accessibility_report.py `
    viper_release_audit.py `
    viper_secrets.py `
    viper_ring_discovery.py `
    viper_vision.py `
    tests\test_viper_release.py
}

Invoke-LoggedCommand "Automated unit and regression tests" {
  cmd /d /c "python -m unittest discover -s tests -v 2>&1"
}

Invoke-LoggedCommand "Safe event routing audit" {
  python viper_release_audit.py
}

Invoke-LoggedCommand "Accessibility control inventory" {
  python accessibility_report.py --root $PSScriptRoot --output (Join-Path $reportDir "accessibility_report_$stamp.txt")
}

Write-Step "Checking required project files."
foreach ($item in @(
  @("main.pyw", "main app"),
  @("viper_audio.py", "audio module"),
  @("viper_config.py", "config module"),
  @("viper_config.example.json", "example config"),
  @("viper_ha_listener.py", "Home Assistant listener module"),
  @("viper_ha_vm.py", "Home Assistant VM install engine"),
  @("viper_diagnostics.py", "diagnostics module"),
  @("viper_release_audit.py", "release audit module"),
  @("viper_secrets.py", "secret storage helper"),
  @("accessibility_report.py", "accessibility report generator"),
  @("templates\remote.html", "remote web UI template"),
  @("help\index.html", "help index"),
  @("help\setup.html", "setup help"),
  @("help\ring-mqtt-setup.html", "Ring-MQTT setup help"),
  @("ViperVision.spec", "PyInstaller spec"),
  @("ViperVision.iss", "Inno Setup script"),
  @("build_installer.ps1", "installer build script"),
  @("smoke_installer.ps1", "installer smoke script"),
  @("RELEASE_CHECKLIST.md", "release checklist")
)) {
  Assert-PathExists -Path (Join-Path $PSScriptRoot $item[0]) -Label $item[1]
}

Write-Step "Checking release metadata."
Assert-FileContains -Path (Join-Path $PSScriptRoot "ViperVision.iss") -Pattern '#define MyAppVersion "1\.2\.4"' -Label "Inno version is 1.2.4"
Assert-FileContains -Path (Join-Path $PSScriptRoot "viper_diagnostics.py") -Pattern 'APP_VERSION = "1\.2\.4"' -Label "Diagnostics version is 1.2.4"
Assert-FileContains -Path (Join-Path $PSScriptRoot "ViperVision.spec") -Pattern '\("help", "help"\)' -Label "Help folder is packaged"
Assert-FileContains -Path (Join-Path $PSScriptRoot "ViperVision.spec") -Pattern '\("chimes", "chimes"\)' -Label "Chimes folder is packaged"
Assert-FileContains -Path (Join-Path $PSScriptRoot "build_exe.ps1") -Pattern 'ffmpeg\.exe' -Label "Build script copies FFmpeg when available"

Write-Step "Checking for private/generated files in source root."
$privateFiles = @(
  "viper_config.json",
  ".env",
  "ha_health_watch.csv",
  "viper_support_bundle.zip"
)
foreach ($file in $privateFiles) {
  $path = Join-Path $PSScriptRoot $file
  if (Test-Path -LiteralPath $path) {
    Add-Warning "Private or runtime file exists in source folder: $file"
  }
}

$runtimePatterns = @("*.mp3", "*.wav", "*.csv")
foreach ($pattern in $runtimePatterns) {
  $matches = Get-ChildItem -Path $PSScriptRoot -Filter $pattern -File -ErrorAction SilentlyContinue
  foreach ($match in $matches) {
    Add-Warning "Runtime-like file in source root: $($match.Name)"
  }
}

Write-Step "Checking help docs for current setup language."
Assert-FileContains -Path (Join-Path $PSScriptRoot "help\setup.html") -Pattern "Follow The Setup Wizard" -Label "Setup help mentions beginner setup"
Assert-FileContains -Path (Join-Path $PSScriptRoot "help\ring-mqtt-setup.html") -Pattern "Ring-MQTT" -Label "Ring-MQTT help exists and names the integration"
Assert-FileContains -Path (Join-Path $PSScriptRoot "README.md") -Pattern "Home Assistant" -Label "README covers Home Assistant"

if ($BuildInstaller) {
  Invoke-LoggedProcess `
    -Name "Build installer" `
    -FilePath "powershell.exe" `
    -Arguments @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "build_installer.ps1"))
}

$installer = Join-Path $PSScriptRoot "installer\ViperVision-v1.2.4-Setup.exe"
$distExe = Join-Path $PSScriptRoot "dist\ViperVision\ViperVision.exe"
$distFfmpeg = Join-Path $PSScriptRoot "dist\ViperVision\_internal\ffmpeg.exe"
$distHelp = Join-Path $PSScriptRoot "dist\ViperVision\_internal\help\index.html"

Write-Step "Checking built artifacts if present."
Assert-BuildArtifacts -Required:$BuildInstaller

if ($SmokeInstaller) {
  if (-not (Test-Path -LiteralPath $installer)) {
    Add-Failure "Cannot smoke test installer because it does not exist: $installer"
  } else {
    Invoke-LoggedProcess `
      -Name "Installer smoke test" `
      -FilePath "powershell.exe" `
      -Arguments @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "smoke_installer.ps1"))
  }
}

if ($LiveHomeAssistant) {
  Invoke-LoggedCommand "Live Home Assistant event routing audit" {
    python viper_release_audit.py --live-ha
  }

  Invoke-LoggedCommand "Optional read-only Home Assistant diagnostic probe" {
    @'
import json
import viper_config as cfg
import viper_discovery as discovery

settings = cfg.get_ha_settings(include_env=True)
host = settings.get("ha_ip", "")
port = settings.get("ha_port", "8123")
token = settings.get("ha_token", "")
print(f"HA host configured: {bool(host)}")
print(f"HA token configured: {bool(token)}")
if host and token:
    result = discovery.test_ha_connection(token=token, ha_ip=host, ha_port=port, timeout=8)
    safe = {k: v for k, v in result.items() if k.lower() not in {"token", "ha_token"}}
    print(json.dumps(safe, indent=2, default=str))
else:
    print("Skipping live HA probe because host or token is missing.")
'@ | python -
  }
}

Write-Step "Release check finished."
Write-Step "Warnings: $($script:Warnings.Count)"
Write-Step "Failures: $($script:Failures.Count)"

if ($script:Warnings.Count -gt 0) {
  Write-Step "Warning list:"
  foreach ($warning in $script:Warnings) {
    Write-Step " - $warning"
  }
}

if ($script:Failures.Count -gt 0) {
  Write-Step "Failure list:"
  foreach ($failure in $script:Failures) {
    Write-Step " - $failure"
  }
  Write-Host ""
  Write-Host "Release checks FAILED. See report: $reportPath"
  exit 1
}

Write-Host ""
Write-Host "Release checks PASSED."
Write-Host "Report saved to: $reportPath"
if (-not $SmokeInstaller) {
  Write-Host "For the strongest pre-release check, run: .\run_release_checks.ps1 -All"
}
