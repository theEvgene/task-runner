<# Runs the pinned installer implementation from a target Git object against
an older installed checkout. It does not fetch or install by itself. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CheckoutPath,
    [Parameter(Mandatory)][string]$Revision,
    [string]$SkillsDirectory = (Join-Path $HOME '.agents\skills'),
    [string]$ReceiptPath = '',
    [switch]$AllowOldLinkMigration,
    [ValidateSet('', 'after-junction-removal', 'before-readback')]
    [string]$TestFailureStage = ''
)

$ErrorActionPreference = 'Stop'
$checkout = [System.IO.Path]::GetFullPath($CheckoutPath)
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('task-runner-bootstrap-' + [guid]::NewGuid().ToString('N'))

function Invoke-CheckoutGit {
    param([string[]]$Arguments)
    $lines = @(& git -C $checkout @Arguments 2>&1 | ForEach-Object { [string]$_ })
    if ($LASTEXITCODE -ne 0) { throw "git $($Arguments -join ' ') failed: $($lines -join ' ')" }
    return $lines
}

try {
    $resolved = @(Invoke-CheckoutGit @('rev-parse', '--verify', '--end-of-options', "$($Revision.Trim())^{commit}"))[0].Trim()
    if ($resolved -notmatch '^[0-9a-f]{40}$') { throw "Revision '$Revision' did not resolve to a commit." }
    New-Item -ItemType Directory -Path $scratch | Out-Null
    foreach ($name in @('install.ps1', 'migration.ps1', 'junction-transaction.ps1')) {
        $content = Invoke-CheckoutGit @('show', "${resolved}:skills/release/scripts/$name")
        [System.IO.File]::WriteAllText(
            (Join-Path $scratch $name), ($content -join "`n") + "`n",
            [System.Text.UTF8Encoding]::new($false))
    }

    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $scratch 'install.ps1'),
        '-CheckoutPath', $checkout, '-Revision', $resolved, '-SkillsDirectory', $SkillsDirectory
    )
    if ($ReceiptPath) { $arguments += @('-ReceiptPath', $ReceiptPath) }
    if ($AllowOldLinkMigration) { $arguments += '-AllowOldLinkMigration' }
    if ($TestFailureStage) { $arguments += @('-TestFailureStage', $TestFailureStage) }
    & powershell.exe @arguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if ($scratch.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}
