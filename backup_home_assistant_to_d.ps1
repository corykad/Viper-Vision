param(
    [string]$HomeAssistantHost = "192.168.4.49",
    [string]$BackupDirectory = "D:\ha_backups",
    [int]$RetentionDays = 30,
    [string]$LogPath = "$env:APPDATA\viper_vision_1.0\ha_backup.log",
    [switch]$NotifyOnSuccess
)

$ErrorActionPreference = "Stop"

function Write-BackupLog {
    param([string]$Message)

    $logDirectory = Split-Path -Parent $LogPath
    if ($logDirectory -and -not (Test-Path $logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$stamp $Message"
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Invoke-Native {
    param(
        [Parameter(Mandatory=$true)]
        [string]$FilePath,
        [string[]]$Arguments
    )

    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    [pscustomobject]@{
        ExitCode = $exitCode
        Output = @($output)
    }
}

function Invoke-HaSsh {
    param([string]$Command)

    $arguments = @(
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=NUL",
        "-o", "LogLevel=ERROR",
        "root@$HomeAssistantHost",
        $Command
    )
    Invoke-Native -FilePath "ssh.exe" -Arguments $arguments
}

function Get-EnvValue {
    param([string[]]$Names)

    foreach ($name in $Names) {
        foreach ($target in @("Process", "User", "Machine")) {
            $value = [Environment]::GetEnvironmentVariable($name, $target)
            if ($value) {
                return $value
            }
        }
    }

    return $null
}

function Send-Pushover {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Title,
        [Parameter(Mandatory=$true)]
        [string]$Message,
        [int]$Priority = 0
    )

    $user = Get-EnvValue @("PUSHOVER_USER", "PUSHOVER_USER_KEY", "VIPER_PUSHOVER_USER")
    $token = Get-EnvValue @("PUSHOVER_TOKEN", "PUSHOVER_API_TOKEN", "VIPER_PUSHOVER_TOKEN")
    if (-not $user -or -not $token) {
        Write-BackupLog "Pushover skipped because PUSHOVER_USER or PUSHOVER_TOKEN is missing."
        return
    }

    try {
        $body = @{
            token = $token
            user = $user
            title = $Title
            message = $Message
            priority = $Priority
        }
        Invoke-RestMethod -Uri "https://api.pushover.net/1/messages.json" -Method Post -Body $body -TimeoutSec 20 | Out-Null
        Write-BackupLog "Pushover notification sent: $Title"
    } catch {
        Write-BackupLog "Pushover notification failed: $($_.Exception.Message)"
    }
}

function New-HomeAssistantBackup {
    $stamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
    $filename = "viper_ha_backup_$stamp"
    $archiveName = "$filename.tar"
    $name = "Viper HA Backup $stamp"
    $remotePath = "/backup/$archiveName"

    Write-BackupLog "Creating Home Assistant backup '$name'."
    $command = "ha backups new --name '$name' --filename '$archiveName' --raw-json --no-progress"
    $result = Invoke-HaSsh -Command $command
    if ($result.ExitCode -ne 0) {
        throw "Home Assistant backup command failed: $($result.Output -join ' ')"
    }

    $jsonLine = $result.Output | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
    if ($jsonLine) {
        $parsed = $null
        try {
            $parsed = $jsonLine | ConvertFrom-Json
        } catch {
            Write-BackupLog "Could not parse backup JSON response cleanly: $($_.Exception.Message)"
        }
        if ($parsed -and $parsed.result -and $parsed.result -ne "ok") {
            throw "Home Assistant returned result '$($parsed.result)': $($parsed.message)"
        }
    }

    $check = Invoke-HaSsh -Command "test -s '$remotePath'"
    if ($check.ExitCode -ne 0) {
        throw "Home Assistant did not create the expected backup file at $remotePath."
    }

    [pscustomobject]@{
        FileName = $archiveName
        RemotePath = $remotePath
    }
}

function Copy-BackupFromHomeAssistant {
    param(
        [Parameter(Mandatory=$true)]
        [string]$RemotePath,
        [Parameter(Mandatory=$true)]
        [string]$FileName
    )

    if (-not (Test-Path $BackupDirectory)) {
        New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
    }

    $destinationPath = Join-Path $BackupDirectory $FileName
    Write-BackupLog "Copying backup to $destinationPath."
    $arguments = @(
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=NUL",
        "-o", "LogLevel=ERROR",
        "root@$HomeAssistantHost`:$RemotePath",
        $destinationPath
    )
    $copy = Invoke-Native -FilePath "scp.exe" -Arguments $arguments
    if ($copy.ExitCode -ne 0) {
        throw "Backup copy failed: $($copy.Output -join ' ')"
    }

    $file = Get-Item -LiteralPath $destinationPath
    if ($file.Length -le 0) {
        throw "Copied backup is empty: $destinationPath"
    }

    Write-BackupLog "Copied backup successfully. Size: $([math]::Round($file.Length / 1MB, 2)) MB."
}

function Remove-ExportCopyFromHomeAssistant {
    param(
        [Parameter(Mandatory=$true)]
        [string]$RemotePath
    )

    Write-BackupLog "Removing temporary Home Assistant backup copy at $RemotePath."
    $remove = Invoke-HaSsh -Command "rm -f '$RemotePath' && ha backups reload >/dev/null 2>&1"
    if ($remove.ExitCode -ne 0) {
        Write-BackupLog "Could not remove temporary Home Assistant backup copy: $($remove.Output -join ' ')"
        return
    }

    Write-BackupLog "Temporary Home Assistant backup copy removed."
}

function Remove-OldBackups {
    if ($RetentionDays -lt 1) {
        Write-BackupLog "Retention is disabled because RetentionDays is less than 1."
        return
    }

    $cutoff = (Get-Date).AddDays(-$RetentionDays)
    $oldBackups = Get-ChildItem -LiteralPath $BackupDirectory -Filter "viper_ha_backup_*.tar" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff }

    foreach ($backup in $oldBackups) {
        Write-BackupLog "Deleting old backup: $($backup.FullName)"
        Remove-Item -LiteralPath $backup.FullName -Force
    }

    Write-BackupLog "Retention cleanup complete. Deleted $($oldBackups.Count) backup(s) older than $RetentionDays day(s)."
}

try {
    Write-BackupLog "Home Assistant backup run started. Destination=$BackupDirectory retention=$RetentionDays days."
    $backup = New-HomeAssistantBackup
    Copy-BackupFromHomeAssistant -RemotePath $backup.RemotePath -FileName $backup.FileName
    Remove-ExportCopyFromHomeAssistant -RemotePath $backup.RemotePath
    Remove-OldBackups
    Write-BackupLog "Home Assistant backup run finished successfully."
    if ($NotifyOnSuccess) {
        Send-Pushover -Title "HA backup succeeded" -Message "Home Assistant backup copied to $BackupDirectory as $($backup.FileName)." -Priority 0
    }
    exit 0
} catch {
    $failureMessage = $_.Exception.Message
    Write-BackupLog "Home Assistant backup run failed: $failureMessage"
    Send-Pushover -Title "HA backup failed" -Message "Home Assistant backup to $BackupDirectory failed: $failureMessage" -Priority 1
    exit 1
}
