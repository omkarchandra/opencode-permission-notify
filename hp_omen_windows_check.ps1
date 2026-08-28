#Requires -Version 5.1
#Requires -RunAsAdministrator

$ErrorActionPreference = "Continue"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputPath = Join-Path $PSScriptRoot "hp-omen-windows-check-$timestamp.txt"

Start-Transcript -Path $outputPath -Force
try {
    Write-Host "HP Omen read-only boot checkpoint"
    Write-Host "No BCD, BitLocker, firmware, partition, or EFI setting will be changed."

    Write-Host "`n=== Windows version ==="
    cmd.exe /c ver
    (Get-Item "$env:SystemRoot\System32\bcdedit.exe").VersionInfo |
        Select-Object FileVersion, ProductVersion |
        Format-List

    Write-Host "`n=== Firmware BCD entries ==="
    bcdedit.exe /enum firmware /v

    Write-Host "`n=== Windows Boot Manager BCD object ==="
    bcdedit.exe /enum "{bootmgr}" /v

    Write-Host "`n=== Firmware Boot Manager BCD object ==="
    bcdedit.exe /enum "{fwbootmgr}" /v

    Write-Host "`n=== BitLocker status (no recovery keys are requested) ==="
    Get-BitLockerVolume -MountPoint "C:" |
        Select-Object MountPoint, VolumeStatus, ProtectionStatus, EncryptionMethod, LockStatus |
        Format-List
    manage-bde.exe -status C:

    Write-Host "`n=== EFI System Partition inventory (read-only) ==="
    $espType = "{C12A7328-F81F-11D2-BA4B-00A0C93EC93B}"
    Get-Partition |
        Where-Object { $_.GptType -eq $espType } |
        Select-Object DiskNumber, PartitionNumber, DriveLetter, Size, GptType |
        Format-List
}
finally {
    Stop-Transcript
}

Write-Host "`nSaved checkpoint to: $outputPath"
Write-Host "Do not run a bcdedit /set command yet. Return to Ubuntu for review."
