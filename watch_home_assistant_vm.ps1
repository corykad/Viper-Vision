param(
    [string]$VMName = "Home Assistant",
    [string]$HomeAssistantHost = "192.168.4.49",
    [int]$CorePort = 8123,
    [int]$ObserverPort = 4357,
    [int]$TcpTimeoutMs = 1500,
    [int]$ResetAfterFailures = 5,
    [switch]$Once,
    [switch]$NoReset,
    [string]$VBoxManage = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [string]$LogPath = "$env:APPDATA\viper_vision_1.0\ha_vm_watchdog.log",
    [string]$FailureStatePath = "$env:APPDATA\viper_vision_1.0\ha_vm_watchdog_failures.txt",
    [string]$PausePath = "$env:APPDATA\viper_vision_1.0\ha_watchdog_paused.txt",
    [string]$LastRunPath = "$env:APPDATA\viper_vision_1.0\ha_vm_watchdog_last_run.txt",
    [string]$LockPath = "$env:APPDATA\viper_vision_1.0\ha_vm_watchdog.lock",
    [int]$MinimumRunIntervalSeconds = 300,
    [int]$LockStaleMinutes = 30
)

$ErrorActionPreference = "Stop"
$RecoveryScript = Join-Path $PSScriptRoot "viper_ha_recovery.py"
$RecoveryExe = Join-Path $PSScriptRoot "ViperVision.exe"
$PythonExe = "python"

function Ensure-ParentDirectory {
    param([string]$Path)

    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

function Write-WatchdogLog {
    param([string]$Message)

    Ensure-ParentDirectory $LogPath

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$stamp $Message"
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-PathAgeSeconds {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    $item = Get-Item $Path -ErrorAction SilentlyContinue
    if (-not $item) {
        return $null
    }

    return ((Get-Date).ToUniversalTime() - $item.LastWriteTimeUtc).TotalSeconds
}

function Test-WatchdogPaused {
    if (-not (Test-Path $PausePath)) {
        return $false
    }

    $raw = Get-Content $PausePath -ErrorAction SilentlyContinue | Select-Object -First 1
    $until = [datetime]::MinValue
    if ([datetime]::TryParse($raw, [ref]$until)) {
        if ($until.ToUniversalTime() -gt (Get-Date).ToUniversalTime()) {
            Write-WatchdogLog "Watchdog is paused until $($until.ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss'))."
            return $true
        }

        Remove-Item $PausePath -Force -ErrorAction SilentlyContinue
        return $false
    }

    Write-WatchdogLog "Watchdog is paused by $PausePath."
    return $true
}

function Test-CooldownActive {
    $age = Get-PathAgeSeconds $LastRunPath
    if ($null -eq $age) {
        return $false
    }

    if ($age -lt $MinimumRunIntervalSeconds) {
        $remaining = [math]::Ceiling($MinimumRunIntervalSeconds - $age)
        Write-WatchdogLog "Skipping watchdog run. Last run was $([math]::Round($age)) seconds ago; cooldown has $remaining seconds left."
        return $true
    }

    return $false
}

function Set-LastRunNow {
    Ensure-ParentDirectory $LastRunPath
    Set-Content -Path $LastRunPath -Value ((Get-Date).ToUniversalTime().ToString("o")) -Encoding ASCII
}

function Acquire-WatchdogLock {
    Ensure-ParentDirectory $LockPath

    if (Test-Path $LockPath) {
        $age = Get-PathAgeSeconds $LockPath
        if ($null -ne $age -and $age -lt ($LockStaleMinutes * 60)) {
            Write-WatchdogLog "Skipping watchdog run. Another watchdog run appears active."
            return $false
        }

        Write-WatchdogLog "Removing stale watchdog lock."
        Remove-Item $LockPath -Force -ErrorAction SilentlyContinue
    }

    New-Item -Path $LockPath -ItemType File -Value ((Get-Date).ToUniversalTime().ToString("o")) -ErrorAction Stop | Out-Null
    return $true
}

function Invoke-VBox {
    param([string[]]$Arguments)

    if (-not (Test-Path $VBoxManage)) {
        throw "VBoxManage.exe was not found at $VBoxManage"
    }

    $output = & $VBoxManage @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    [pscustomobject]@{
        ExitCode = $exitCode
        Output = @($output)
    }
}

function Get-VMState {
    $result = Invoke-VBox @("showvminfo", $VMName, "--machinereadable")
    if ($result.ExitCode -ne 0) {
        throw "Could not read VM state for '$VMName': $($result.Output -join ' ')"
    }

    $line = $result.Output | Where-Object { $_ -match '^VMState=' } | Select-Object -First 1
    if ($line -match '^VMState="([^"]+)"') {
        return $Matches[1]
    }

    return "unknown"
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMs
    )

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            return $false
        }

        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-FailureCount {
    if (-not (Test-Path $FailureStatePath)) {
        return 0
    }

    $raw = Get-Content $FailureStatePath -ErrorAction SilentlyContinue | Select-Object -First 1
    $value = 0
    if ([int]::TryParse($raw, [ref]$value)) {
        return $value
    }

    return 0
}

function Set-FailureCount {
    param([int]$Count)

    Ensure-ParentDirectory $FailureStatePath

    Set-Content -Path $FailureStatePath -Value $Count
}

function Invoke-WatchdogCycle {
    if (Test-Path $RecoveryExe) {
        Write-WatchdogLog "Running Viper HA recovery engine from ViperVision.exe."
        $output = & $RecoveryExe --ha-recovery-once --compact 2>&1
        $exitCode = $LASTEXITCODE
        $lines = @($output | Where-Object { $_ })
        if ($lines.Count -gt 0) {
            Write-WatchdogLog "recovery: $($lines[0])"
        }
        if ($exitCode -ne 0) {
            foreach ($line in $lines | Select-Object -Skip 1) {
                Write-WatchdogLog "recovery detail: $line"
            }
        }
        if ($exitCode -eq 0) {
            Set-FailureCount 0
        } else {
            Set-FailureCount ((Get-FailureCount) + 1)
        }
        return
    }

    if (Test-Path $RecoveryScript) {
        Write-WatchdogLog "Running Viper HA recovery engine from Python source."
        $output = & $PythonExe $RecoveryScript --compact 2>&1
        $exitCode = $LASTEXITCODE
        $lines = @($output | Where-Object { $_ })
        if ($lines.Count -gt 0) {
            Write-WatchdogLog "recovery: $($lines[0])"
        }
        if ($exitCode -ne 0) {
            foreach ($line in $lines | Select-Object -Skip 1) {
                Write-WatchdogLog "recovery detail: $line"
            }
        }
        if ($exitCode -eq 0) {
            Set-FailureCount 0
        } else {
            Set-FailureCount ((Get-FailureCount) + 1)
        }
        return
    }

    $state = Get-VMState
    Write-WatchdogLog "VM state: $state"

    if ($state -ne "running") {
        Write-WatchdogLog "VM is not running. Starting '$VMName'."
        $start = Invoke-VBox @("startvm", $VMName, "--type", "headless")
        if ($start.ExitCode -ne 0) {
            Write-WatchdogLog "Start failed: $($start.Output -join ' ')"
            Set-FailureCount ((Get-FailureCount) + 1)
            return
        }

        Set-FailureCount 0
        Write-WatchdogLog "Start command succeeded."
        return
    }

    $coreOk = Test-TcpPort -HostName $HomeAssistantHost -Port $CorePort -TimeoutMs $TcpTimeoutMs
    $observerOk = Test-TcpPort -HostName $HomeAssistantHost -Port $ObserverPort -TimeoutMs $TcpTimeoutMs
    Write-WatchdogLog "HA check: core=$coreOk observer=$observerOk host=$HomeAssistantHost"

    if ($coreOk -or $observerOk) {
        Set-FailureCount 0
        return
    }

    $failures = (Get-FailureCount) + 1
    Set-FailureCount $failures
    Write-WatchdogLog "HA is unreachable. Consecutive failures: $failures."

    if ($NoReset -or $failures -lt $ResetAfterFailures) {
        return
    }

    Write-WatchdogLog "Failure limit reached. Resetting '$VMName'."
    $reset = Invoke-VBox @("controlvm", $VMName, "reset")
    if ($reset.ExitCode -eq 0) {
        Set-FailureCount 0
        Write-WatchdogLog "Reset command succeeded."
    } else {
        Write-WatchdogLog "Reset failed: $($reset.Output -join ' ')"
    }
}

$lockAcquired = $false

try {
    if (Test-WatchdogPaused) {
        return
    }

    if (Test-CooldownActive) {
        return
    }

    $lockAcquired = Acquire-WatchdogLock
    if (-not $lockAcquired) {
        return
    }

    Set-LastRunNow

    do {
        try {
            Invoke-WatchdogCycle
        } catch {
            Write-WatchdogLog "Watchdog error: $($_.Exception.Message)"
            Set-FailureCount ((Get-FailureCount) + 1)
        }

        if ($Once) {
            break
        }

        Start-Sleep -Seconds 60
    } while ($true)
} finally {
    if ($lockAcquired) {
        Remove-Item $LockPath -Force -ErrorAction SilentlyContinue
    }
}
