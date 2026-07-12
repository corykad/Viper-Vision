param(
    [string]$VMName = "Home Assistant",
    [string]$PreferredAdapterDescription = "Realtek PCIe GbE Family Controller",
    [string]$VBoxManage = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [string]$VBoxSupInf = "C:\Program Files\Oracle\VirtualBox\drivers\vboxsup\VBoxSup.inf",
    [string]$VBoxNetLwfInf = "C:\Program Files\Oracle\VirtualBox\drivers\network\netlwf\VBoxNetLwf.inf",
    [string]$LogPath = "$env:APPDATA\viper_vision_1.0\ha_virtualbox_hardening.log",
    [switch]$CheckOnly,
    [switch]$RegisterTasks
)

$ErrorActionPreference = "Stop"

function Write-HardenLog {
    param([string]$Message)

    $directory = Split-Path -Parent $LogPath
    if ($directory -and -not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$stamp $Message"
    $wrote = $false
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Add-Content -Path $LogPath -Value $line -Encoding UTF8
            $wrote = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }

    Write-Output $line
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
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

function Start-ServiceIfNeeded {
    param([string]$Name)

    $state = Get-ServiceStateText $Name
    if ($state -eq "running") {
        Write-HardenLog "$Name is already running."
        return
    }

    if ($CheckOnly) {
        Write-HardenLog "$Name is $state. CheckOnly would start it."
        return
    }

    Write-HardenLog "$Name is $state. Starting it."
    $result = Invoke-Native "sc.exe" @("start", $Name)
    foreach ($line in $result.Output) {
        Write-HardenLog "sc $Name`: $line"
    }
}

function Install-DriverInf {
    param(
        [string]$InfPath,
        [string]$Label
    )

    if (-not (Test-Path $InfPath)) {
        Write-HardenLog "$Label driver INF not found at $InfPath."
        return
    }

    if ($CheckOnly) {
        Write-HardenLog "CheckOnly would install $Label driver from $InfPath."
        return
    }

    Write-HardenLog "Installing $Label driver from $InfPath."
    $result = Invoke-Native "pnputil.exe" @("/add-driver", $InfPath, "/install")
    foreach ($line in $result.Output) {
        Write-HardenLog "pnputil $Label`: $line"
    }
}

function Install-BridgeService {
    if (-not (Test-Path $VBoxNetLwfInf)) {
        Write-HardenLog "VirtualBox bridge INF not found at $VBoxNetLwfInf."
        return
    }

    $bridgeState = Get-ServiceStateText "VBoxNetLwf"
    if ($bridgeState -ne "missing") {
        Write-HardenLog "VBoxNetLwf service is $bridgeState."
        return
    }

    if ($CheckOnly) {
        Write-HardenLog "CheckOnly would register VBoxNetLwf with netcfg."
        return
    }

    Write-HardenLog "Registering VBoxNetLwf bridge service with netcfg."
    $result = Invoke-Native "netcfg.exe" @("-l", $VBoxNetLwfInf, "-c", "s", "-i", "oracle_VBoxNetLwf")
    foreach ($line in $result.Output) {
        Write-HardenLog "netcfg VBoxNetLwf`: $line"
    }
}

function Get-PreferredAdapter {
    $adapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
        $_.Status -eq "Up" -and (
            $_.InterfaceDescription -eq $PreferredAdapterDescription -or
            $_.Name -eq $PreferredAdapterDescription
        )
    })

    if ($adapters.Count -gt 0) {
        return $adapters[0]
    }

    $fallback = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
        $_.Status -eq "Up" -and $_.HardwareInterface -eq $true -and $_.InterfaceDescription -notmatch "Virtual|VPN|TAP|Loopback"
    })

    if ($fallback.Count -gt 0) {
        Write-HardenLog "Preferred adapter was not found; using $($fallback[0].Name) / $($fallback[0].InterfaceDescription)."
        return $fallback[0]
    }

    throw "No suitable physical network adapter is up."
}

function Set-FastStartupDisabled {
    $path = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
    $current = (Get-ItemProperty -Path $path -Name "HiberbootEnabled" -ErrorAction SilentlyContinue).HiberbootEnabled
    Write-HardenLog "Fast Startup HiberbootEnabled is $current."

    if ($current -eq 0) {
        return
    }

    if ($CheckOnly) {
        Write-HardenLog "CheckOnly would disable Windows Fast Startup."
        return
    }

    Set-ItemProperty -Path $path -Name "HiberbootEnabled" -Type DWord -Value 0
    Write-HardenLog "Disabled Windows Fast Startup."
}

function Set-EthernetPowerSavingDisabled {
    param([string]$AdapterName)

    try {
        if ($CheckOnly) {
            Write-HardenLog "CheckOnly would disable power saving on network adapter ${AdapterName}."
            return
        }

        Set-NetAdapterPowerManagement -Name $AdapterName -AllowComputerToTurnOffDevice Disabled -ErrorAction Stop
        Write-HardenLog "Disabled power saving on network adapter ${AdapterName}."
    }
    catch {
        Write-HardenLog "Could not change network adapter power saving for ${AdapterName}: $($_.Exception.Message)"
    }
}

function Set-BridgeBindings {
    param([string]$AdapterName)

    $bindings = @(Get-NetAdapterBinding -Name "*" -ComponentID "oracle_VBoxNetLwf" -ErrorAction SilentlyContinue)
    if ($bindings.Count -eq 0) {
        Write-HardenLog "No VirtualBox bridge bindings are visible yet."
        return
    }

    foreach ($binding in $bindings) {
        $shouldEnable = $binding.Name -eq $AdapterName
        $wanted = if ($shouldEnable) { "enabled" } else { "disabled" }
        Write-HardenLog "Bridge binding $($binding.Name) currently Enabled=$($binding.Enabled); wanted $wanted."

        if ($CheckOnly) {
            continue
        }

        if ($shouldEnable -and -not $binding.Enabled) {
            Enable-NetAdapterBinding -Name $binding.Name -ComponentID "oracle_VBoxNetLwf" -ErrorAction Stop
            Write-HardenLog "Enabled VirtualBox bridge binding on $($binding.Name)."
        }
        elseif (-not $shouldEnable -and $binding.Enabled) {
            Disable-NetAdapterBinding -Name $binding.Name -ComponentID "oracle_VBoxNetLwf" -ErrorAction Stop
            Write-HardenLog "Disabled VirtualBox bridge binding on $($binding.Name)."
        }
    }
}

function Set-VMBridgeAdapter {
    param([string]$AdapterDescription)

    if (-not (Test-Path $VBoxManage)) {
        Write-HardenLog "VBoxManage not found at $VBoxManage."
        return
    }

    $info = Invoke-Native $VBoxManage @("showvminfo", $VMName, "--machinereadable")
    $infoText = $info.Output -join "`n"
    $vmState = "unknown"
    $currentBridge = ""

    if ($infoText -match '(?m)^VMState="([^"]+)"') {
        $vmState = $Matches[1]
    }
    if ($infoText -match '(?m)^bridgeadapter1="([^"]*)"') {
        $currentBridge = $Matches[1]
    }

    Write-HardenLog "VM '$VMName' state is $vmState; bridgeadapter1 is '$currentBridge'."

    if ($CheckOnly) {
        Write-HardenLog "CheckOnly would set VM '$VMName' to bridged adapter '$AdapterDescription'."
        return
    }

    if ($vmState -eq "running" -and $currentBridge -eq $AdapterDescription) {
        Write-HardenLog "VM '$VMName' is already running on the preferred bridge adapter; skipping modifyvm."
        return
    }

    if ($vmState -eq "running") {
        Write-HardenLog "VM '$VMName' is running. Skipping modifyvm to avoid locking a live VM; apply bridge adapter change while HA is stopped."
        return
    }

    $result = Invoke-Native $VBoxManage @("modifyvm", $VMName, "--nic1", "bridged", "--bridgeadapter1", $AdapterDescription, "--nictype1", "82540EM")
    foreach ($line in $result.Output) {
        Write-HardenLog "VBoxManage bridge`: $line"
    }
    Write-HardenLog "Set VM '$VMName' bridge adapter to '$AdapterDescription'."
}

function Register-StartupTasks {
    if (-not $RegisterTasks) {
        return
    }

    if ($CheckOnly) {
        Write-HardenLog "CheckOnly would register startup scheduled tasks."
        return
    }

    $safeStartScript = Join-Path $PSScriptRoot "start_home_assistant_vm_safe.ps1"
    $watchdogScript = Join-Path $PSScriptRoot "run_ha_watchdog_hidden.vbs"

    if (Test-Path $safeStartScript) {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$safeStartScript`""
        $triggers = @(
            (New-ScheduledTaskTrigger -AtStartup),
            (New-ScheduledTaskTrigger -AtLogOn)
        )
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
        Register-ScheduledTask -TaskName "Home Assistant Boot" -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
        Write-HardenLog "Registered Home Assistant Boot scheduled task."
    }
    else {
        Write-HardenLog "Safe startup script not found at $safeStartScript."
    }

    if (Test-Path $watchdogScript) {
        $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$watchdogScript`""
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName "Viper Home Assistant Watchdog" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Write-HardenLog "Registered Viper Home Assistant Watchdog scheduled task."
    }
    else {
        Write-HardenLog "Watchdog hidden launcher not found at $watchdogScript."
    }
}

Write-HardenLog "Starting Home Assistant VirtualBox host hardening. CheckOnly=$CheckOnly RegisterTasks=$RegisterTasks"
$isAdmin = Test-IsAdmin
Write-HardenLog "Administrator=$isAdmin"

if (-not $isAdmin -and -not $CheckOnly) {
    throw "Administrator PowerShell is required to repair VirtualBox drivers, network bindings, services, and scheduled tasks."
}

$adapter = Get-PreferredAdapter
Write-HardenLog "Selected adapter: $($adapter.Name) / $($adapter.InterfaceDescription)"

Set-FastStartupDisabled
Install-DriverInf -InfPath $VBoxSupInf -Label "VBoxSup"
Install-DriverInf -InfPath $VBoxNetLwfInf -Label "VBoxNetLwf"
Install-BridgeService
Start-ServiceIfNeeded -Name "VBoxSDS"
Start-ServiceIfNeeded -Name "vboxsup"
Start-ServiceIfNeeded -Name "VBoxNetLwf"
Set-EthernetPowerSavingDisabled -AdapterName $adapter.Name
Set-BridgeBindings -AdapterName $adapter.Name
Set-VMBridgeAdapter -AdapterDescription $adapter.InterfaceDescription
Register-StartupTasks

Write-HardenLog "Home Assistant VirtualBox host hardening finished."
