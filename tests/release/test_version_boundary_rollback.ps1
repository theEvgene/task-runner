# Disposable integration test for the real installed revision boundary and
# transaction rollback. No live skills directory or network is used.
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$oldRevision = 'bcd721586f24fe9f22be8e67da26ceb253fc8679'
$baseRevision = 'a9bb7254bc31f72ad913a713c6e9a62ba39ac947'
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('tr-version-boundary-' + [guid]::NewGuid().ToString('N'))
$failures = [System.Collections.Generic.List[string]]::new()

function Assert-True([bool]$Condition, [string]$Message) {
    if ($Condition) { Write-Host "ok: $Message" }
    else { $failures.Add($Message); Write-Host "FAIL: $Message" }
}

function Invoke-Git([string]$Repo, [string[]]$Arguments) {
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = @(& git -C $Repo @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $code = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    if ($code -ne 0) { throw "git $($Arguments -join ' ') failed: $($lines -join ' ')" }
    return ,$lines
}

function Get-Target([string]$Path) {
    $item = Get-Item -Force -LiteralPath $Path
    return [System.IO.Path]::GetFullPath(@($item.Target)[0])
}

function New-Destination([string]$Root, [string]$Checkout, [string]$Pat) {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    $targets = [ordered]@{
        'release' = (Join-Path $Checkout 'skills\release')
        'dell-lan-local-dev' = (Join-Path $Pat 'skills\dell-lan-local-dev')
        'lan-task-bridge' = (Join-Path $Pat 'skills\t-bank\lan-task-bridge')
        'lenovo-release' = (Join-Path $Pat 'skills\t-bank\lenovo-release')
    }
    foreach ($pair in $targets.GetEnumerator()) {
        New-Item -ItemType Junction -Path (Join-Path $Root $pair.Key) -Target $pair.Value | Out-Null
    }
    $foreign = Join-Path $Root 'foreign-entry'
    New-Item -ItemType Directory -Path $foreign | Out-Null
    Set-Content -LiteralPath (Join-Path $foreign 'keep.txt') -Value 'foreign' -Encoding UTF8
    return $targets
}

function Invoke-BootstrapScenario(
    [string]$Name, [string]$Checkout, [string]$Pat, [string]$Bootstrap,
    [string]$TargetRevision, [string]$FailureStage = '') {
    Invoke-Git $Checkout @('checkout', '--quiet', '--detach', $oldRevision) | Out-Null
    $destination = Join-Path $scratch "dest-$Name"
    $original = New-Destination $destination $Checkout $Pat
    $receipt = Join-Path $scratch "$Name-receipt.json"
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Bootstrap,
        '-CheckoutPath', $Checkout, '-Revision', $TargetRevision,
        '-SkillsDirectory', $destination, '-ReceiptPath', $receipt,
        '-AllowOldLinkMigration'
    )
    if ($FailureStage) { $arguments += @('-TestFailureStage', $FailureStage) }
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & powershell.exe @arguments 2>&1 | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference

    Assert-True (Test-Path -LiteralPath (Join-Path $destination 'foreign-entry\keep.txt') -PathType Leaf) "$Name preserves foreign entry"
    foreach ($target in $original.Values) {
        Assert-True (Test-Path -LiteralPath (Join-Path $target 'SKILL.md') -PathType Leaf) "$Name preserves source $target"
    }
    if (-not $FailureStage) {
        Assert-True ($code -eq 0) 'bootstrap succeeds across the real old revision boundary'
        Assert-True ((Invoke-Git $Checkout @('rev-parse', 'HEAD'))[0].Trim() -eq $TargetRevision) 'successful bootstrap installs target revision'
        foreach ($nameToCheck in @('release', 'dell-lan-local-dev', 'lan-task-bridge')) {
            $expected = [System.IO.Path]::GetFullPath((Join-Path $Checkout "skills\$nameToCheck"))
            Assert-True ([string]::Equals((Get-Target (Join-Path $destination $nameToCheck)), $expected,
                [System.StringComparison]::OrdinalIgnoreCase)) "$nameToCheck points at target checkout"
        }
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $destination 'lenovo-release'))) 'renamed old link is removed on success'
        return
    }

    Assert-True ($code -eq 1) "$Name fails at injected stage"
    Assert-True ((Invoke-Git $Checkout @('rev-parse', 'HEAD'))[0].Trim() -eq $oldRevision) "$Name restores old checkout revision"
    foreach ($pair in $original.GetEnumerator()) {
        $actual = Get-Target (Join-Path $destination $pair.Key)
        $expected = [System.IO.Path]::GetFullPath($pair.Value)
        Assert-True ([string]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)) "$Name restores $($pair.Key) junction"
    }
    $result = Get-Content -Raw -LiteralPath $receipt | ConvertFrom-Json
    Assert-True ($result.rollback -eq 'restored') "$Name receipt confirms checkout rollback"
    Assert-True ($result.junctionRollback -eq 'restored') "$Name receipt confirms junction rollback"
    Assert-True ([bool]$result.junctionRollbackReadback.restored) "$Name receipt confirms rollback readback"
}

try {
    New-Item -ItemType Directory -Path $scratch | Out-Null
    $checkout = Join-Path $scratch 'task-runner'
    $oldCloneProtection = $env:GIT_CLONE_PROTECTION_ACTIVE
    $env:GIT_CLONE_PROTECTION_ACTIVE = 'false'
    & git clone --quiet --no-local $sourceRoot $checkout
    if ($null -eq $oldCloneProtection) { Remove-Item Env:GIT_CLONE_PROTECTION_ACTIVE }
    else { $env:GIT_CLONE_PROTECTION_ACTIVE = $oldCloneProtection }
    if ($LASTEXITCODE -ne 0) { throw 'fixture clone failed' }
    Invoke-Git $checkout @('remote', 'set-url', 'origin', 'https://github.com/theEvgene/task-runner.git') | Out-Null
    Invoke-Git $checkout @('cat-file', '-e', "${oldRevision}^{commit}") | Out-Null
    Invoke-Git $checkout @('checkout', '--quiet', '--detach', $baseRevision) | Out-Null
    foreach ($name in @('install.ps1', 'migration.ps1', 'junction-transaction.ps1', 'bootstrap.ps1')) {
        Copy-Item -LiteralPath (Join-Path $sourceRoot "skills\release\scripts\$name") `
            -Destination (Join-Path $checkout "skills\release\scripts\$name") -Force
    }
    Invoke-Git $checkout @('add', 'skills/release/scripts') | Out-Null
    Invoke-Git $checkout @('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'fixture corrected target') | Out-Null
    $targetRevision = (Invoke-Git $checkout @('rev-parse', 'HEAD'))[0].Trim()
    $bootstrap = Join-Path $scratch 'bootstrap.ps1'
    $bootstrapText = Invoke-Git $checkout @('show', "${targetRevision}:skills/release/scripts/bootstrap.ps1")
    [System.IO.File]::WriteAllText($bootstrap, ($bootstrapText -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))

    $pat = Join-Path $scratch 'personal-agent-toolkit'
    New-Item -ItemType Directory -Path $pat | Out-Null
    & git -C $pat init --quiet --initial-branch=main
    & git -C $pat remote add origin https://github.com/theEvgene/personal-agent-toolkit.git
    foreach ($spec in @(
        @('skills\dell-lan-local-dev', 'dell-lan-local-dev', 'scripts/dell_lan/'),
        @('skills\t-bank\lan-task-bridge', 'lan-task-bridge', 'scripts/lan_bridge/lan_bridge.py'),
        @('skills\t-bank\lenovo-release', 'lenovo-release', 'scripts/release/request.py')
    )) {
        $directory = Join-Path $pat $spec[0]
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $directory 'SKILL.md') -Encoding UTF8 `
            -Value "---`nname: $($spec[1])`n---`n$($spec[2])`n"
    }
    & git -C $pat add -A
    & git -C $pat -c user.name=Fixture -c user.email=fixture@example.invalid commit --quiet -m 'fixture PAT skills'

    Invoke-BootstrapScenario 'success' $checkout $pat $bootstrap $targetRevision
    Invoke-BootstrapScenario 'after-removal' $checkout $pat $bootstrap $targetRevision 'after-junction-removal'
    Invoke-BootstrapScenario 'before-readback' $checkout $pat $bootstrap $targetRevision 'before-readback'
}
finally {
    if ($scratch.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($failures.Count -gt 0) { exit 1 }
Write-Host 'PASS: real version boundary and rollback fixtures.'
