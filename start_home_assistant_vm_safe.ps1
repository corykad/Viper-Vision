param(
    [string]$VMName = "Home Assistant",
    [string]$VBoxManage = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [string]$VBoxSupInf = "C:\Program Files\Oracle\VirtualBox\drivers\vboxsup\VBoxSup.inf",
    [string]$HardeningScript = "",
    [string]$LogPath = "$env:APPDATA\viper_vision_1.0\ha_vm_boot.log",
    [int]$DriverWaitSeconds = 45,
    [int]$InitialDelaySeconds = 45
)

$ErrorActionPreference = "Stop"

if (-not $HardeningScript) {
    $HardeningScript = Join-Path $PSScriptRoot "harden_home_assistant_virtualbox_host.ps1"
}

function Write-BootLog {
    param([string]$Message)

    $directory = Split-Path -Parent $LogPath
    if ($directory -and -not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$stamp $Message"
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    $output = & $FilePath @Arguments 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = @($output)
    }
}

function Get-ServiceStateText {
    param([string]$Name)

    $result = Invoke-Native "sc.exe" @("query", $Name)
    $text = $result.Output -join "`n"
    if ($result.ExitCode -ne 0) {
        return "missing"
    }

    if ($text -match "STATE\s+:\s+\d+\s+(\w+)") {
        return $Matches[1].ToLowerInvariant()
    }

    return "unknown"
}

function Wait-ServiceState {
    param(
        [string]$Name,
        [string]$Wanted = "running",
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $state = Get-ServiceStateText $Name
        if ($state -eq $Wanted) {
            return $true
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Start-VBoxService {
    param([string]$Name)

    $state = Get-ServiceStateText $Name
    if ($state -eq "running") {
        Write-BootLog "$Name is already running."
        return $true
    }

    Write-BootLog "$Name is $state. Starting it."
    $result = Invoke-Native "sc.exe" @("start", $Name)
    if ($result.ExitCode -ne 0) {
        Write-BootLog "Could not start $Name. Exit $($result.ExitCode). $($result.Output -join ' ')"
    }

    if (Wait-ServiceState $Name "running" $DriverWaitSeconds) {
        Write-BootLog "$Name is running."
        return $true
    }

    $finalState = Get-ServiceStateText $Name
    Write-BootLog "$Name did not reach running state. Current state: $finalState."
    return $false
}

function Repair-VBoxSupportDriver {
    if (-not (Test-Path $VBoxSupInf)) {
        Write-BootLog "VBoxSup INF not found at $VBoxSupInf."
        return $false
    }

    Write-BootLog "Reinstalling VirtualBox support driver with pnputil."
    $result = Invoke-Native "pnputil.exe" @("/add-driver", $VBoxSupInf, "/install")
    Write-BootLog "pnputil exit $($result.ExitCode). $($result.Output -join ' ')"
    return $result.ExitCode -eq 0
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-HostHardening {
    if (-not (Test-Path $HardeningScript)) {
        Write-BootLog "Host hardening script not found: $HardeningScript"
        return
    }

    if (Test-IsAdmin) {
        Write-BootLog "Running elevated VirtualBox host hardening before VM start."
        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HardeningScript 2>&1
    } else {
        Write-BootLog "Running VirtualBox host hardening diagnostics before VM start."
        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HardeningScript -CheckOnly 2>&1
    }

    foreach ($line in @($output | Where-Object { $_ })) {
        Write-BootLog "hardening: $line"
    }
}

function Ensure-VirtualBoxReady {
    if (-not (Test-Path $VBoxManage)) {
        throw "VBoxManage.exe was not found at $VBoxManage"
    }

    Start-VBoxService "VBoxSDS" | Out-Null
    if (Start-VBoxService "vboxsup") {
        return $true
    }

    Repair-VBoxSupportDriver | Out-Null
    Start-VBoxService "VBoxSDS" | Out-Null
    return Start-VBoxService "vboxsup"
}

function Get-VMState {
    $result = Invoke-Native $VBoxManage @("showvminfo", $VMName, "--machinereadable")
    if ($result.ExitCode -ne 0) {
        throw "Could not read VM state for '$VMName': $($result.Output -join ' ')"
    }

    $line = $result.Output | Where-Object { $_ -match '^VMState=' } | Select-Object -First 1
    if ($line -match '^VMState="([^"]+)"') {
        return $Matches[1]
    }

    return "unknown"
}

try {
    Write-BootLog "Safe Home Assistant VM startup beginning."
    if ($InitialDelaySeconds -gt 0) {
        Write-BootLog "Waiting $InitialDelaySeconds seconds for Windows startup services to settle."
        Start-Sleep -Seconds $InitialDelaySeconds
    }

    Invoke-HostHardening

    if (-not (Ensure-VirtualBoxReady)) {
        throw "VirtualBox support driver vboxsup is not running. Start this script from an elevated scheduled task or repair VirtualBox."
    }

    $state = Get-VMState
    Write-BootLog "VM state is $state."
    if ($state -eq "running") {
        Write-BootLog "Home Assistant VM is already running."
        exit 0
    }

    Write-BootLog "Starting Home Assistant VM."
    $start = Invoke-Native $VBoxManage @("startvm", $VMName, "--type", "headless")
    Write-BootLog "VBoxManage startvm exit $($start.ExitCode). $($start.Output -join ' ')"
    exit $start.ExitCode
} catch {
    Write-BootLog "Safe startup failed: $($_.Exception.Message)"
    exit 1
}
