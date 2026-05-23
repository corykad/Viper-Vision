param(
    [string]$HomeAssistantHost = "192.168.4.49",
    [int]$CorePort = 8123,
    [int]$ObserverPort = 4357,
    [int]$IntervalSeconds = 15,
    [string]$OutputPath = ".\ha_health_watch.csv"
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $OutputPath)) {
    "timestamp,core_status,observer_status,state,detail" | Out-File -FilePath $OutputPath -Encoding utf8
}

Write-Host "Watching Home Assistant health at $HomeAssistantHost."
Write-Host "Core: http://$HomeAssistantHost`:$CorePort/api/"
Write-Host "Observer: http://$HomeAssistantHost`:$ObserverPort/"
Write-Host "Writing CSV to $((Resolve-Path -Path (Split-Path $OutputPath -Parent) -ErrorAction SilentlyContinue).Path)\$(Split-Path $OutputPath -Leaf)"
Write-Host "Stop with Ctrl+C."

while ($true) {
    $timestamp = (Get-Date).ToString("s")
    $coreStatus = "timeout"
    $observerStatus = "timeout"
    $detail = ""

    try {
        $core = Invoke-WebRequest -Uri "http://$HomeAssistantHost`:$CorePort/api/" -UseBasicParsing -TimeoutSec 5
        $coreStatus = [string]$core.StatusCode
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $coreStatus = [string][int]$_.Exception.Response.StatusCode
        } else {
            $coreStatus = "timeout"
            $detail = $_.Exception.Message.Replace(",", " ")
        }
    }

    try {
        $observer = Invoke-WebRequest -Uri "http://$HomeAssistantHost`:$ObserverPort/" -UseBasicParsing -TimeoutSec 5
        $observerStatus = [string]$observer.StatusCode
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $observerStatus = [string][int]$_.Exception.Response.StatusCode
        } else {
            $observerStatus = "timeout"
            if (-not $detail) { $detail = $_.Exception.Message.Replace(",", " ") }
        }
    }

    if ($observerStatus -eq "200" -and $coreStatus -in @("200", "401", "403")) {
        $state = "healthy"
    } elseif ($observerStatus -eq "200" -and $coreStatus -eq "timeout") {
        $state = "core_hung_vm_alive"
    } elseif ($observerStatus -eq "timeout" -and $coreStatus -eq "timeout") {
        $state = "vm_or_network_down"
    } else {
        $state = "degraded"
    }

    "$timestamp,$coreStatus,$observerStatus,$state,$detail" | Add-Content -Path $OutputPath -Encoding utf8
    Write-Host "$timestamp Core=$coreStatus Observer=$observerStatus State=$state $detail"
    Start-Sleep -Seconds $IntervalSeconds
}
