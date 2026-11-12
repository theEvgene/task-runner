# Fast fixture test for the same-repo managed-junction rule in
# skills/release/scripts/install.ps1 (Windows-only, like the installer).
# Loads the REAL functions from install.ps1 by name (no copies), builds
# throwaway git checkouts under $env:TEMP, and proves: same-repo links
# retarget, foreign/plain/dangling links stay conflicts, and the guarded
# removal refuses on a changed target. Touches nothing outside $scratch.
# Run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File this file.

$ErrorActionPreference = 'Stop'

$installPs1 = Join-Path $PSScriptRoot '..\..\skills\release\scripts\install.ps1'
$text = Get-Content -LiteralPath $installPs1 -Raw -Encoding UTF8

function Get-InstallerFunctionText {
    param([Parameter(Mandatory)][string]$Name)
    $m = [regex]::Match($text, "(?ms)^function\s+$Name\b.*?\r?\n\}")
    if (-not $m.Success) { throw "installer function '$Name' not found" }
    return $m.Value
}

$definitions = @(
    'Get-NormalizedPath', 'Get-JunctionTarget', 'Get-GitHubSlug',
    'Test-EquivalentRemote', 'Get-CheckoutProof', 'Get-DestinationEntry',
    'Test-OldPointerContent', 'Test-PatOwnership',
    'Test-ManagedJunctionFromSameRepository', 'Remove-ManagedJunctionSafely',
    'Stop-Install'
) | ForEach-Object { Get-InstallerFunctionText -Name $_ }
Invoke-Expression ($definitions -join "`n")

$ExpectedRemoteUrl = 'https://github.com/theEvgene/task-runner.git'
$ExpectedPatRemoteUrl = 'https://github.com/theEvgene/personal-agent-toolkit.git'

$script:failures = [System.Collections.Generic.List[string]]::new()
function Assert-True {
    param([bool]$Condition, [Parameter(Mandatory)][string]$Message)
    if ($Condition) { Write-Host "ok: $Message" }
    else { $script:failures.Add($Message); Write-Host "FAIL: $Message" }
}

function New-FixtureCheckout {
    param([Parameter(Mandatory)][string]$Directory, [Parameter(Mandatory)][string]$Remote)
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $ErrorActionPreference = 'Continue'
    & git -C $Directory init --quiet --initial-branch=main 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { & git -C $Directory init --quiet 2>$null | Out-Null }
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) { throw "git init failed in $Directory" }
    & git -C $Directory remote add origin $Remote 2>$null | Out-Null
    return $Directory
}

function Add-FixtureSkill {
    param([Parameter(Mandatory)][string]$Checkout, [Parameter(Mandatory)][string]$Relative,
        [Parameter(Mandatory)][string]$Name, [string]$Body = 'pointer')
    $dir = Join-Path $Checkout ($Relative -replace '/', '\')
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $dir 'SKILL.md') -Value "---`nname: $Name`n---`n$Body`n" -Encoding UTF8
    $ErrorActionPreference = 'Continue'
    & git -C $Checkout add -A 2>$null | Out-Null
    & git -C $Checkout -c user.name=Fixture -c user.email=fixture@example.invalid commit --quiet -m "skill $Relative" 2>$null | Out-Null
    $ErrorActionPreference = 'Stop'
}

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('tr-same-repo-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $scratch -Force | Out-Null
    $dest = Join-Path $scratch 'dest-skills'
    New-Item -ItemType Directory -Path $dest -Force | Out-Null

    # 1. Same-repo junction is recognized (exact skills/<name> + SKILL.md + origin slug).
    $sameA = New-FixtureCheckout (Join-Path $scratch 'sameA') $ExpectedRemoteUrl
    Add-FixtureSkill $sameA 'skills/release' 'release'
    $sameSkill = Join-Path $sameA 'skills\release'
    $j1 = Join-Path $dest 'release'
    New-Item -ItemType Junction -Path $j1 -Target $sameSkill | Out-Null
    Assert-True (Test-ManagedJunctionFromSameRepository -ExistingTarget $sameSkill -SkillName 'release') 'same-repo junction recognized'

    # 2. Grouped shape skills/<group>/<name> passes; wrong leaf name fails.
    Add-FixtureSkill $sameA 'skills/group/extra' 'extra'
    $grouped = Join-Path $sameA 'skills\group\extra'
    Assert-True (Test-ManagedJunctionFromSameRepository -ExistingTarget $grouped -SkillName 'extra') 'grouped same-repo junction recognized'
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget $grouped -SkillName 'release')) 'leaf-name mismatch rejected'

    # 3. SSH remote normalizes to the same slug.
    $sameSsh = New-FixtureCheckout (Join-Path $scratch 'sameSsh') 'git@github.com:theEvgene/task-runner.git'
    Add-FixtureSkill $sameSsh 'skills/release' 'release'
    Assert-True (Test-ManagedJunctionFromSameRepository -ExistingTarget (Join-Path $sameSsh 'skills\release') -SkillName 'release') 'ssh origin accepted via slug'

    # 4. Foreign repo junction is not managed.
    $foreign = New-FixtureCheckout (Join-Path $scratch 'foreign') 'https://example.invalid/other/toolkit.git'
    Add-FixtureSkill $foreign 'skills/release' 'release'
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget (Join-Path $foreign 'skills\release') -SkillName 'release')) 'foreign checkout rejected'

    # 5. Outside skills/, missing SKILL.md, and non-checkout targets are not managed.
    $outside = Join-Path $sameA 'misc\release'
    New-Item -ItemType Directory -Path $outside -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $outside 'SKILL.md') -Value "---`nname: release`n---`npointer`n" -Encoding UTF8
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget $outside -SkillName 'release')) 'target outside skills/ rejected'
    $noSkill = Join-Path $sameA 'skills\noskill'
    New-Item -ItemType Directory -Path $noSkill -Force | Out-Null
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget $noSkill -SkillName 'noskill')) 'target without SKILL.md rejected'
    $plain = Join-Path $scratch 'plain\skills\release'
    New-Item -ItemType Directory -Path $plain -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $plain 'SKILL.md') -Value "---`nname: release`n---`npointer`n" -Encoding UTF8
    Assert-True ($null -eq (Get-CheckoutProof -TargetDir $plain)) 'non-checkout proof is null'
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget $plain -SkillName 'release')) 'non-checkout target rejected'

    # 6. Plain directory entries are not junctions (installer conflict path).
    $plainEntry = Join-Path $dest 'plainskill'
    New-Item -ItemType Directory -Path $plainEntry -Force | Out-Null
    $plainItem = Get-Item -Force -LiteralPath $plainEntry
    Assert-True ($null -eq (Get-JunctionTarget -Item $plainItem)) 'plain directory has no junction target'

    # 7. Dangling junction is not managed.
    $dangling = Join-Path $dest 'dangling'
    $gone = Join-Path $scratch 'gone-target'
    New-Item -ItemType Directory -Path $gone -Force | Out-Null
    New-Item -ItemType Junction -Path $dangling -Target $gone | Out-Null
    Remove-Item -LiteralPath $gone -Force -Recurse
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget $gone -SkillName 'dangling')) 'dangling target rejected'

    # 8. Guarded swap: removes only the reparse point, keeps target contents.
    $sameB = New-FixtureCheckout (Join-Path $scratch 'sameB') $ExpectedRemoteUrl
    Add-FixtureSkill $sameB 'skills/release' 'release'
    $sameBTarget = [System.IO.Path]::GetFullPath((Join-Path $sameB 'skills\release'))
    Remove-ManagedJunctionSafely -Path $j1 -ExpectedTarget ([System.IO.Path]::GetFullPath($sameSkill))
    Assert-True (-not (Test-Path -LiteralPath $j1)) 'managed junction removed'
    Assert-True (Test-Path -LiteralPath (Join-Path $sameSkill 'SKILL.md') -PathType Leaf) 'target contents kept after removal'
    New-Item -ItemType Junction -Path $j1 -Target (Join-Path $sameB 'skills\release') | Out-Null
    $now = Get-JunctionTarget -Item (Get-Item -Force -LiteralPath $j1)
    Assert-True ([string]::Equals($now, $sameBTarget, [System.StringComparison]::OrdinalIgnoreCase)) 'junction retargeted to the new checkout'

    # 9. TOCTOU: changed target refuses, existing link untouched.
    $victim = Join-Path $dest 'victim'
    New-Item -ItemType Junction -Path $victim -Target $sameSkill | Out-Null
    $threw = $false
    try { Remove-ManagedJunctionSafely -Path $victim -ExpectedTarget $sameBTarget }
    catch { $threw = $true }
    Assert-True $threw 'removal with a stale expected target throws'
    $still = Get-JunctionTarget -Item (Get-Item -Force -LiteralPath $victim)
    Assert-True ([string]::Equals($still, ([System.IO.Path]::GetFullPath($sameSkill)), [System.StringComparison]::OrdinalIgnoreCase)) 'changed link left untouched'

    # 10. PAT rule is untouched: old pointer proves PAT ownership but is NOT same-repo.
    $pat = New-FixtureCheckout (Join-Path $scratch 'pat') $ExpectedPatRemoteUrl
    Add-FixtureSkill $pat 'skills/t-bank/lenovo-release' 'lenovo-release' 'see scripts/release/request.py'
    $patTarget = Join-Path $pat 'skills\t-bank\lenovo-release'
    $patProof = [pscustomobject]@{ frontmatterName = 'lenovo-release'; bodyMustContain = 'scripts/release/request.py' }
    Assert-True (Test-PatOwnership -TargetDir $patTarget -Proof $patProof) 'PAT ownership still proven by text+remote'
    Assert-True (-not (Test-ManagedJunctionFromSameRepository -ExistingTarget $patTarget -SkillName 'lenovo-release')) 'PAT checkout is not same-repo'
}
finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures.Count -gt 0) {
    Write-Host "FAILED: $($script:failures.Count) assertion(s)."
    exit 1
}
Write-Host 'PASS: same-repo managed-junction fixtures.'
exit 0
