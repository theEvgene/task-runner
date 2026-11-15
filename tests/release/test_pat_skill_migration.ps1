# Disposable integration test for opt-in migration of same-named PAT links.
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('tr-pat-migration-' + [guid]::NewGuid().ToString('N'))
$failures = [System.Collections.Generic.List[string]]::new()

function Assert-True([bool]$Condition, [string]$Message) {
    if ($Condition) { Write-Host "ok: $Message" }
    else { $failures.Add($Message); Write-Host "FAIL: $Message" }
}

function Initialize-Repo([string]$Path, [string]$Remote) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    & git -C $Path init --quiet --initial-branch=main
    if ($LASTEXITCODE -ne 0) { throw "git init failed: $Path" }
    & git -C $Path remote add origin $Remote
}

function Commit-All([string]$Path, [string]$Message) {
    & git -C $Path add -A
    & git -C $Path -c user.name=Fixture -c user.email=fixture@example.invalid commit --quiet -m $Message
    if ($LASTEXITCODE -ne 0) { throw "git commit failed: $Path" }
}

try {
    New-Item -ItemType Directory -Path $scratch | Out-Null
    $taskRunner = Join-Path $scratch 'task-runner'
    Initialize-Repo $taskRunner 'https://github.com/theEvgene/task-runner.git'
    foreach ($relative in @(
        'skills\release\SKILL.md',
        'skills\release\migrations.json',
        'skills\release\scripts\install.ps1',
        'skills\release\scripts\migration.ps1',
        'skills\release\scripts\junction-transaction.ps1',
        'skills\release\scripts\verify-install.ps1',
        'skills\dell-lan-local-dev\SKILL.md',
        'skills\lan-task-bridge\SKILL.md'
    )) {
        $destination = Join-Path $taskRunner $relative
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceRoot $relative) -Destination $destination
    }
    Commit-All $taskRunner 'fixture task-runner skills'
    $revision = (& git -C $taskRunner rev-parse HEAD).Trim()

    $pat = Join-Path $scratch 'personal-agent-toolkit'
    Initialize-Repo $pat 'https://github.com/theEvgene/personal-agent-toolkit.git'
    $patDell = Join-Path $pat 'skills\dell-lan-local-dev'
    $patBridge = Join-Path $pat 'skills\t-bank\lan-task-bridge'
    $patRelease = Join-Path $pat 'skills\t-bank\lenovo-release'
    New-Item -ItemType Directory -Path $patDell,$patBridge,$patRelease -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $patDell 'SKILL.md') -Encoding UTF8 -Value "---`nname: dell-lan-local-dev`n---`ncanonical scripts/dell_lan/ pointer`n"
    Set-Content -LiteralPath (Join-Path $patBridge 'SKILL.md') -Encoding UTF8 -Value "---`nname: lan-task-bridge`n---`ncanonical scripts/lan_bridge/lan_bridge.py pointer`n"
    Set-Content -LiteralPath (Join-Path $patRelease 'SKILL.md') -Encoding UTF8 -Value "---`nname: lenovo-release`n---`ncanonical scripts/release/request.py pointer`n"
    Commit-All $pat 'fixture PAT skills'

    $destination = Join-Path $scratch 'installed-skills'
    New-Item -ItemType Directory -Path $destination | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $destination 'dell-lan-local-dev') -Target $patDell | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $destination 'lan-task-bridge') -Target $patBridge | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $destination 'lenovo-release') -Target $patRelease | Out-Null

    $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $taskRunner 'skills\release\scripts\install.ps1') `
        -Revision $revision -SkillsDirectory $destination -AllowOldLinkMigration 2>&1)
    if ($LASTEXITCODE -ne 0) { Write-Host ($output -join "`n") }
    Assert-True ($LASTEXITCODE -eq 0) 'installer accepts the proven PAT junctions'
    $receipt = $output[-1] | ConvertFrom-Json
    Assert-True ([bool]$receipt.ok) 'installer receipt is successful'
    Assert-True (@($receipt.migratedOldLinks).Count -eq 3) 'receipt records same-name and renamed migrations'

    foreach ($name in @('dell-lan-local-dev', 'lan-task-bridge')) {
        $item = Get-Item -Force -LiteralPath (Join-Path $destination $name)
        $expected = [System.IO.Path]::GetFullPath((Join-Path $taskRunner "skills\$name"))
        $actual = [System.IO.Path]::GetFullPath(@($item.Target)[0])
        Assert-True ($item.LinkType -eq 'Junction' -and $actual -eq $expected) "$name points at task-runner"
    }
    Assert-True (Test-Path -LiteralPath (Join-Path $patDell 'SKILL.md') -PathType Leaf) 'old Dell source is retained'
    Assert-True (Test-Path -LiteralPath (Join-Path $patBridge 'SKILL.md') -PathType Leaf) 'old bridge source is retained'
    Assert-True (Test-Path -LiteralPath (Join-Path $patRelease 'SKILL.md') -PathType Leaf) 'old release source is retained'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $destination 'lenovo-release'))) 'renamed PAT junction is removed'

    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $taskRunner 'skills\release\scripts\verify-install.ps1') `
        -Revision $revision -SkillsDirectory $destination | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) 'readback verifier accepts the installed revision'
}
finally {
    if ($scratch.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($failures.Count -gt 0) { exit 1 }
Write-Host 'PASS: PAT skill migration fixture.'
