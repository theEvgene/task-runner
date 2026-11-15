function New-JunctionInventory {
    Write-Output -NoEnumerate ([System.Collections.Generic.List[object]]::new())
}

function Remove-ManagedJunctionSafely {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ExpectedTarget)

    $current = Get-Item -Force -LiteralPath $Path
    $currentTarget = Get-JunctionTarget -Item $current
    if (-not $currentTarget -or -not [string]::Equals(
        $currentTarget, $ExpectedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Install 'migration-unsafe' 'target-changed' "Managed junction '$Path' changed; left unchanged."
    }
    [System.IO.Directory]::Delete($current.FullName, $false)
}

function Register-JunctionMutation {
    param(
        [Parameter(Mandatory)]$Inventory,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][scriptblock]$GetTarget
    )

    $normalized = [System.IO.Path]::GetFullPath($Path)
    if (@($Inventory | Where-Object {
        [string]::Equals($_.path, $normalized, [System.StringComparison]::OrdinalIgnoreCase)
    }).Count -gt 0) { return }

    $item = Get-Item -Force -LiteralPath $normalized -ErrorAction SilentlyContinue
    $target = if ($item) { & $GetTarget $item } else { $null }
    if ($item -and -not $target) {
        throw "Refusing to inventory non-junction entry '$normalized'."
    }
    $Inventory.Add([pscustomobject]@{
        path = $normalized
        existed = [bool]$item
        originalTarget = $target
        expectedPresent = [bool]$item
        expectedTarget = $target
    })
}

function Set-JunctionExpectedState {
    param(
        [Parameter(Mandatory)]$Inventory,
        [Parameter(Mandatory)][string]$Path,
        [bool]$Present,
        [string]$Target = ''
    )

    $normalized = [System.IO.Path]::GetFullPath($Path)
    $entry = $Inventory | Where-Object {
        [string]::Equals($_.path, $normalized, [System.StringComparison]::OrdinalIgnoreCase)
    } | Select-Object -First 1
    if (-not $entry) { throw "Junction mutation was not inventoried: $normalized" }
    $entry.expectedPresent = $Present
    $entry.expectedTarget = if ($Present) { [System.IO.Path]::GetFullPath($Target) } else { $null }
}

function Test-JunctionState {
    param(
        [Parameter(Mandatory)]$Entry,
        [Parameter(Mandatory)][scriptblock]$GetTarget,
        [switch]$Original
    )

    $present = if ($Original) { [bool]$Entry.existed } else { [bool]$Entry.expectedPresent }
    $target = if ($Original) { $Entry.originalTarget } else { $Entry.expectedTarget }
    $item = Get-Item -Force -LiteralPath $Entry.path -ErrorAction SilentlyContinue
    if (-not $present) { return $null -eq $item }
    if (-not $item) { return $false }
    $actual = & $GetTarget $item
    return $actual -and [string]::Equals(
        $actual, $target, [System.StringComparison]::OrdinalIgnoreCase)
}

function Restore-JunctionInventory {
    param(
        [Parameter(Mandatory)]$Inventory,
        [Parameter(Mandatory)][scriptblock]$GetTarget
    )

    # Validate the whole owned post-state before changing any destination.
    foreach ($entry in $Inventory) {
        if (-not (Test-JunctionState -Entry $entry -GetTarget $GetTarget)) {
            throw "Junction '$($entry.path)' changed after installer mutation; rollback refused."
        }
    }
    $entries = @($Inventory.ToArray())
    [array]::Reverse($entries)
    foreach ($entry in $entries) {
        $current = Get-Item -Force -LiteralPath $entry.path -ErrorAction SilentlyContinue
        if ($current) { [System.IO.Directory]::Delete($current.FullName, $false) }
        if ($entry.existed) {
            New-Item -ItemType Junction -Path $entry.path -Target $entry.originalTarget | Out-Null
        }
    }
    foreach ($entry in $Inventory) {
        if (-not (Test-JunctionState -Entry $entry -GetTarget $GetTarget -Original)) {
            throw "Junction rollback readback failed for '$($entry.path)'."
        }
    }
}

function Set-ManagedJunction {
    param(
        [Parameter(Mandatory)]$Inventory,
        [Parameter(Mandatory)][string]$Path,
        [string]$ExpectedTarget = '',
        [Parameter(Mandatory)][string]$NewTarget,
        [string]$FailureStage = ''
    )

    Register-JunctionMutation -Inventory $Inventory -Path $Path -GetTarget ${function:Get-JunctionTarget}
    $temporary = "$Path.task-runner-$([guid]::NewGuid().ToString('N'))"
    try {
        New-Item -ItemType Junction -Path $temporary -Target $NewTarget | Out-Null
        $temporaryTarget = Get-JunctionTarget -Item (Get-Item -Force -LiteralPath $temporary)
        if (-not $temporaryTarget -or -not [string]::Equals(
            $temporaryTarget, (Get-NormalizedPath -Path $NewTarget),
            [System.StringComparison]::OrdinalIgnoreCase)) {
            Stop-Install 'migration-unsafe' 'temporary-junction-mismatch' "Temporary junction readback failed for '$Path'."
        }
        if ($ExpectedTarget) {
            Remove-ManagedJunctionSafely -Path $Path -ExpectedTarget $ExpectedTarget
            Set-JunctionExpectedState -Inventory $Inventory -Path $Path -Present $false
            if ($FailureStage -eq 'after-junction-removal') {
                Stop-Install 'injected-failure' 'after-junction-removal' 'Injected failure after junction removal.'
            }
        }
        [System.IO.Directory]::Move($temporary, $Path)
        Set-JunctionExpectedState -Inventory $Inventory -Path $Path -Present $true -Target $NewTarget
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            [System.IO.Directory]::Delete($temporary, $false)
        }
    }
}

function Remove-ManagedJunctionTransactionally {
    param(
        [Parameter(Mandatory)]$Inventory,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ExpectedTarget,
        [string]$FailureStage = ''
    )

    Register-JunctionMutation -Inventory $Inventory -Path $Path -GetTarget ${function:Get-JunctionTarget}
    Remove-ManagedJunctionSafely -Path $Path -ExpectedTarget $ExpectedTarget
    Set-JunctionExpectedState -Inventory $Inventory -Path $Path -Present $false
    if ($FailureStage -eq 'after-junction-removal') {
        Stop-Install 'injected-failure' 'after-junction-removal' 'Injected failure after junction removal.'
    }
}
