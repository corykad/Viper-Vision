param(
    [string]$VmName = "Home Assistant",
    [switch]$UseVirtioNet
)

$vbox = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
if (-not (Test-Path -LiteralPath $vbox)) {
    throw "VBoxManage.exe was not found at $vbox"
}

$info = & $vbox showvminfo $VmName --machinereadable 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "VirtualBox VM '$VmName' was not found."
}

$stateLine = $info | Where-Object { $_ -like "VMState=*" } | Select-Object -First 1
if ($stateLine -notmatch '"poweroff"') {
    throw "Refusing to harden while the VM is not powered off. Shut down Home Assistant first."
}

Write-Host "Hardening VirtualBox settings for $VmName."
Write-Host "Note: if VirtualBox is using Windows Hypervisor/Hyper-V mode, consider disabling the Windows hypervisor stack separately."

& $vbox storagectl $VmName --name "SATA" --hostiocache on

if ($UseVirtioNet) {
    & $vbox modifyvm $VmName --nictype1 virtio
    Write-Host "NIC adapter type set to virtio."
}

Write-Host "Done. Start the VM and monitor Home Assistant health."
