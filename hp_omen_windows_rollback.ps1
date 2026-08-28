#Requires -Version 5.1
#Requires -RunAsAdministrator

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$transcriptPath = Join-Path $PSScriptRoot "hp-omen-rollback-$timestamp.txt"
$safetyBackup = Join-Path $PSScriptRoot "hp-omen-bcd-before-rollback-$timestamp.bcd"
$originalLoader = "\EFI\Microsoft\Boot\bootmgfw.efi"

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
    Write-Host "HP Omen BCD rollback"
    Write-Host "This restores only {bootmgr}.path to Microsoft's original loader."
    Write-Host "It does not alter EFI files, partitions, or BitLocker state."

    Write-Host "`nSaving the current BCD state to $safetyBackup"
    Invoke-BcdEdit -Arguments @("/export", $safetyBackup) | ForEach-Object { Write-Host $_ }
    if (-not (Test-Path -LiteralPath $safetyBackup) -or (Get-Item -LiteralPath $safetyBackup).Length -eq 0) {
        throw "Safety BCD export was not created or is empty; rollback was not attempted."
    }

    Write-Host "Restoring {bootmgr}.path to $originalLoader"
    Invoke-BcdEdit -Arguments @("/set", "{bootmgr}", "path", $originalLoader) |
        ForEach-Object { Write-Host $_ }

    $after = Invoke-BcdEdit -Arguments @("/enum", "{bootmgr}", "/v")
    $after | ForEach-Object { Write-Host $_ }
    $pattern = "(?im)^\s*path\s+" + [regex]::Escape($originalLoader) + "\s*$"
    if (($after -join "`n") -notmatch $pattern) {
        throw "Rollback validation failed: {bootmgr} does not show the Microsoft path."
    }

    Write-Host "`nRollback validated. Restart normally to test Windows."
    Write-Host "BitLocker state was left unchanged."
}
catch {
    Write-Error $_
    Write-Host "Review $transcriptPath"
    exit 1
}
finally {
    Stop-Transcript
}
