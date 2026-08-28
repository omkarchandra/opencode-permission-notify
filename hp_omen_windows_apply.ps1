#Requires -Version 5.1
#Requires -RunAsAdministrator

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$transcriptPath = Join-Path $PSScriptRoot "hp-omen-apply-$timestamp.txt"
$backupPath = Join-Path $PSScriptRoot "hp-omen-bcd-backup-$timestamp.bcd"
$beforePath = Join-Path $PSScriptRoot "hp-omen-bcd-before-$timestamp.txt"
$afterPath = Join-Path $PSScriptRoot "hp-omen-bcd-after-$timestamp.txt"
$originalLoader = "\EFI\Microsoft\Boot\bootmgfw.efi"
$ubuntuLoader = "\EFI\ubuntu\shimx64.efi"

function Invoke-BcdEdit {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & "$env:SystemRoot\System32\bcdedit.exe" @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "bcdedit $($Arguments -join ' ') failed with exit code $LASTEXITCODE`n$($output -join "`n")"
    }
    return @($output)
}

Start-Transcript -Path $transcriptPath -Force
try {
    Write-Host "HP Omen BCD path test"
    Write-Host "This will export BCD, then change only {bootmgr}.path."
    Write-Host "No EFI file, partition, firmware key, or BitLocker setting will be changed."

    $bitLocker = Get-BitLockerVolume -MountPoint "C:"
    Write-Host "`nBitLocker protection status: $($bitLocker.ProtectionStatus)"
    if ($bitLocker.ProtectionStatus.ToString() -ne "Off") {
        throw "BitLocker protection is not Off. Stop and review before changing BCD."
    }

    $before = Invoke-BcdEdit -Arguments @("/enum", "{bootmgr}", "/v")
    $before | Set-Content -LiteralPath $beforePath -Encoding UTF8
    Write-Host "`nCurrent Windows Boot Manager object:"
    $before | ForEach-Object { Write-Host $_ }

    $beforeText = $before -join "`n"
    $originalPattern = "(?im)^\s*path\s+" + [regex]::Escape($originalLoader) + "\s*$"
    $ubuntuPattern = "(?im)^\s*path\s+" + [regex]::Escape($ubuntuLoader) + "\s*$"

    if ($beforeText -match $ubuntuPattern) {
        Write-Host "`n{bootmgr} already targets $ubuntuLoader; no change was made."
        return
    }
    if ($beforeText -notmatch $originalPattern) {
        throw "Unexpected {bootmgr} path. Expected $originalLoader; no change was made."
    }

    Write-Host "`nRecovery key checkpoint: confirmed outside this script."
    $confirmation = Read-Host "Type APPLY to export BCD and set the Ubuntu shim path"
    if ($confirmation -cne "APPLY") {
        Write-Host "Confirmation did not match; no change was made."
        return
    }

    Write-Host "`nExporting BCD to $backupPath"
    Invoke-BcdEdit -Arguments @("/export", $backupPath) | ForEach-Object { Write-Host $_ }
    if (-not (Test-Path -LiteralPath $backupPath) -or (Get-Item -LiteralPath $backupPath).Length -eq 0) {
        throw "BCD export was not created or is empty; no path change was attempted."
    }

    Write-Host "Setting {bootmgr}.path to $ubuntuLoader"
    Invoke-BcdEdit -Arguments @("/set", "{bootmgr}", "path", $ubuntuLoader) |
        ForEach-Object { Write-Host $_ }

    $after = Invoke-BcdEdit -Arguments @("/enum", "{bootmgr}", "/v")
    $firmware = Invoke-BcdEdit -Arguments @("/enum", "firmware", "/v")
    @($after; ""; "=== Firmware entries ==="; $firmware) |
        Set-Content -LiteralPath $afterPath -Encoding UTF8

    Write-Host "`nUpdated Windows Boot Manager object:"
    $after | ForEach-Object { Write-Host $_ }
    if (($after -join "`n") -notmatch $ubuntuPattern) {
        Write-Warning "Post-change validation failed. Restoring the Microsoft path now."
        Invoke-BcdEdit -Arguments @("/set", "{bootmgr}", "path", $originalLoader) |
            ForEach-Object { Write-Host $_ }
        throw "The Ubuntu path did not persist in BCD; the original path was restored."
    }

    Write-Host "`nBCD path update validated."
    Write-Host "Backup: $backupPath"
    Write-Host "Rollback script: $(Join-Path $PSScriptRoot 'hp_omen_windows_rollback.ps1')"
    Write-Host "Restart normally without F9. If GRUB appears, select Windows first."
    Write-Host "If normal boot fails, use F9 to launch EFI\Microsoft\Boot\bootmgfw.efi."
}
catch {
    Write-Error $_
    Write-Host "No partition or EFI file was modified. Review $transcriptPath"
    exit 1
}
finally {
    Stop-Transcript
}
