<#
Standalone installer for the task-runner skills of the pinned revision.

- Refuses on a dirty checkout before changing anything; no network, no
  fetch, no deploy. The revision must resolve locally.
- Advances this checkout (detached) to the pinned revision when needed.
- Links every skills/*/SKILL.md of the PINNED revision as a managed
  junction under the skills directory. An existing foreign entry
  (including any personal-agent-toolkit path) is never overwritten:
  it is reported as a conflict and left unchanged.
- Migrates the old 'lenovo-release' pointer link only with
  -AllowOldLinkMigration and only on proven ownership (see
  migrations.json): the target must hold the old pointer content.
- Prints a JSON receipt (schema task-runner/skill-rollout@1) and fails
  when readback does not match the pinned revision.

This installer intentionally writes NO global agent-guidance blocks:
this repo assumes no global auto-discovery, and global files may be
managed by another checkout. Skill discovery is the reason cross-repo
use works: link names come from `git ls-tree <pinned> skills/`, so the
installed links always reflect the pinned revision, never a dirty tree.
#>
[CmdletBinding()]
param(
    [string]$Revision = 'origin/main',
    [string]$SkillsDirectory = (Join-Path $HOME '.agents\skills'),
    [string]$ReceiptPath = '',
    [switch]$AllowOldLinkMigration
)

$ErrorActionPreference = 'Stop'

$ExpectedRemoteUrl = 'https://github.com/theEvgene/task-runner.git'

$stages = [System.Collections.Generic.List[object]]::new()
$checkout = ''
$pinned = ''
$skills = @()
$installedRevision = ''
$headBefore = ''
$mutation = 'none'
$verification = $null

function Stop-Install {
    param([string]$Reason, [string]$Code, [string]$Message, $Verification)

    $exception = New-Object System.InvalidOperationException($Message)
    $exception.Data['rolloutReason'] = $Reason
    $exception.Data['rolloutCode'] = $Code
    if ($null -ne $Verification) { $exception.Data['rolloutVerification'] = $Verification }
    throw $exception
}

function Get-NormalizedPath {
    param([Parameter(Mandatory)][string]$Path)

    return ([System.IO.Path]::GetFullPath($Path)).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar)
}

function Get-JunctionTarget {
    param([Parameter(Mandatory)][System.IO.FileSystemInfo]$Item)

    if (-not ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $null }
    if ($Item.LinkType -ne 'Junction') { return $null }
    $target = @($Item.Target)[0]
    if ([string]::IsNullOrWhiteSpace($target)) { return $null }
    if (-not [System.IO.Path]::IsPathRooted($target)) {
        $target = Join-Path $Item.Parent.FullName $target
    }
    return Get-NormalizedPath -Path $target
}

function Invoke-RepoGit {
    param([string[]]$Arguments)

    $ErrorActionPreference = 'Continue'
    $lines = @(& git '-C' $checkout @Arguments 2>&1 | ForEach-Object { [string]$_ })
    return [pscustomobject]@{ code = $LASTEXITCODE; lines = $lines }
}

function Test-EquivalentRemote {
    param([string]$Actual, [string]$Expected)

    $normalize = {
        param([string]$Value)
        $text = $Value.Trim().TrimEnd('/')
        if ($text.EndsWith('.git', [System.StringComparison]::OrdinalIgnoreCase)) {
            $text = $text.Substring(0, $text.Length - 4)
        }
        return $text
    }
    return [string]::Equals((& $normalize $Actual), (& $normalize $Expected),
        [System.StringComparison]::OrdinalIgnoreCase)
}

# Names come from the pinned commit, not the worktree: this is why the
# installed links keep working across checkouts and stay in sync with
# the pinned revision. Flat (skills/<name>) and one grouped level
# (skills/<group>/<name>) are supported; the link name is the leaf.
function Get-PinnedSkills {
    param([string]$Commit)

    $listing = Invoke-RepoGit @('ls-tree', '-r', '--name-only', $Commit, 'skills/')
    if ($listing.code -ne 0) {
        Stop-Install 'checkout-invalid' 'ls-tree-failed' "ls-tree failed: $($listing.lines -join ' ')"
    }
    $found = [System.Collections.Generic.List[object]]::new()
    foreach ($line in $listing.lines) {
        if ($line -notmatch '^skills/([^/]+)(?:/([^/]+))?/SKILL\.md$') { continue }
        $relative = if ($Matches[2]) { "skills/$($Matches[1])/$($Matches[2])" } else { "skills/$($Matches[1])" }
        $name = if ($Matches[2]) { $Matches[2] } else { $Matches[1] }
        if (@($found | Where-Object { $_.name -eq $name }).Count -eq 0) {
            $found.Add([pscustomobject]@{ name = $name; path = $relative })
        } else {
            Stop-Install 'ambiguous-skill' 'duplicate-leaf-name' "Two skills share the leaf name '$name'."
        }
    }
    return @($found.ToArray() | Sort-Object name)
}

function Get-ExpectedSkillPath {
    param([Parameter(Mandatory)][string]$Relative)

    return Get-NormalizedPath -Path (Join-Path $checkout ($Relative -replace '/', [System.IO.Path]::DirectorySeparatorChar))
}

function Test-OldPointerContent {
    param([Parameter(Mandatory)][string]$TargetDir, [Parameter(Mandatory)][object]$Proof)

    $candidate = Join-Path $TargetDir 'SKILL.md'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $false }
    $text = (Get-Content -LiteralPath $candidate -Raw -Encoding UTF8) -replace "`r`n", "`n"
    $lines = $text -split "`n"
    if ($lines.Count -lt 2 -or $lines[0] -ne '---') { return $false }
    $end = -1
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -eq '---') { $end = $i; break }
    }
    if ($end -lt 0) { return $false }
    $frontmatter = ($lines[1..($end - 1)] -join "`n")
    if ($frontmatter -notmatch "(?m)^name:\s*$([regex]::Escape([string]$Proof.frontmatterName))\s*$") { return $false }
    return $text.Contains([string]$Proof.bodyMustContain)
}

function Remove-ManagedJunctionSafely {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ExpectedTarget)

    $current = Get-Item -Force -LiteralPath $Path
    $currentTarget = Get-JunctionTarget -Item $current
    if (-not $currentTarget -or -not [string]::Equals($currentTarget, $ExpectedTarget,
        [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Install 'migration-unsafe' 'target-changed' "Managed junction '$Path' changed; left unchanged."
    }
    [System.IO.Directory]::Delete($current.FullName, $false)
}

function Read-Readback {
    $head = Invoke-RepoGit @('rev-parse', 'HEAD')
    $headValue = if ($head.code -eq 0 -and @($head.lines).Count -gt 0) { $head.lines[0].Trim() } else { '' }
    $status = Invoke-RepoGit @('status', '--porcelain=v1', '--untracked-files=all')
    $clean = ($status.code -eq 0 -and @($status.lines).Count -eq 0)

    $matched = 0
    $mismatched = [System.Collections.Generic.List[string]]::new()
    foreach ($skill in $skills) {
        $existing = Get-ChildItem -Force -LiteralPath $SkillsDirectory -ErrorAction SilentlyContinue |
            Where-Object { [string]::Equals($_.Name, $skill.name, [System.StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1
        $target = if ($existing) { Get-JunctionTarget -Item $existing } else { $null }
        $expected = Get-ExpectedSkillPath -Relative $skill.path
        if ($target -and [string]::Equals($target, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            $matched++
        } else {
            $mismatched.Add($skill.name)
        }
    }

    # The pinned skill files must exist in the checkout; ls-tree names are
    # re-checked against the worktree so a partial checkout fails readback.
    $missing = [System.Collections.Generic.List[string]]::new()
    foreach ($skill in $skills) {
        if (-not (Test-Path -LiteralPath (Join-Path (Get-ExpectedSkillPath -Relative $skill.path) 'SKILL.md') -PathType Leaf)) {
            $missing.Add($skill.path)
        }
    }

    $verified = ($headValue -eq $pinned) -and $clean -and
        ($mismatched.Count -eq 0) -and ($missing.Count -eq 0)
    return [pscustomobject]@{
        installedCheckoutHead = $headValue
        headMatchesPinnedRevision = ($headValue -eq $pinned)
        checkoutClean = $clean
        junctions = [pscustomobject]@{
            destination = $SkillsDirectory
            expected = @($skills).Count
            matched = $matched
            mismatched = @($mismatched.ToArray())
        }
        missingSkillFiles = @($missing.ToArray())
        verified = $verified
    }
}

$exitCode = 0
$ok = $false
$failure = $null
$failureAction = 'none'
$currentStage = 'preflight'
$connected = [System.Collections.Generic.List[string]]::new()
$conflicts = [System.Collections.Generic.List[string]]::new()
$migrated = [System.Collections.Generic.List[string]]::new()
$leftUnchanged = [System.Collections.Generic.List[string]]::new()

try {
    if ([string]::IsNullOrWhiteSpace($Revision)) {
        Stop-Install 'revision-missing' 'revision-required' 'An explicit -Revision is required.'
    }

    $checkout = Get-NormalizedPath -Path (Join-Path $PSScriptRoot '..\..\..')
    $inside = Invoke-RepoGit @('rev-parse', '--is-inside-work-tree')
    if ($inside.code -ne 0 -or @($inside.lines).Count -eq 0 -or $inside.lines[0].Trim() -ne 'true') {
        Stop-Install 'checkout-invalid' 'not-a-git-checkout' "Installer checkout is not a Git working tree: $checkout"
    }

    $head = Invoke-RepoGit @('rev-parse', 'HEAD')
    $installedRevision = if ($head.code -eq 0 -and @($head.lines).Count -gt 0) { $head.lines[0].Trim() } else { '' }
    $headBefore = $installedRevision

    $remote = Invoke-RepoGit @('remote', 'get-url', 'origin')
    $remoteUrl = if ($remote.code -eq 0 -and @($remote.lines).Count -gt 0) { $remote.lines[0].Trim() } else { '' }
    if ([string]::IsNullOrWhiteSpace($remoteUrl)) {
        Stop-Install 'unexpected-remote' 'remote-missing' "Checkout has no 'origin' remote: $checkout"
    }
    if (-not (Test-EquivalentRemote -Actual $remoteUrl -Expected $ExpectedRemoteUrl)) {
        Stop-Install 'unexpected-remote' 'remote-url-mismatch' "Remote '$remoteUrl' does not match '$ExpectedRemoteUrl'."
    }

    $status = Invoke-RepoGit @('status', '--porcelain=v1', '--untracked-files=all')
    if (-not ($status.code -eq 0 -and @($status.lines).Count -eq 0)) {
        Stop-Install 'dirty-checkout' 'uncommitted-changes' "Checkout has uncommitted changes: $($status.lines -join '; ')"
    }

    # Local resolution only: no fetch, no network. An unknown revision
    # refuses instead of reaching out.
    $resolved = Invoke-RepoGit @('rev-parse', '--verify', '--quiet', '--end-of-options', "$($Revision.Trim())^{commit}")
    if ($resolved.code -ne 0 -or @($resolved.lines).Count -eq 0 -or $resolved.lines[0] -notmatch '^[0-9a-f]{40}$') {
        Stop-Install 'unresolved-revision' 'commit-not-found' "Revision '$Revision' does not resolve to a local commit."
    }
    $pinned = $resolved.lines[0].Trim()
    $skills = Get-PinnedSkills -Commit $pinned
    $stages.Add([pscustomobject]@{ name = 'preflight'; status = 'passed' })

    $currentStage = 'advance'
    if ($installedRevision -ne $pinned) {
        $advanced = Invoke-RepoGit @('checkout', '--quiet', '--detach', $pinned)
        if ($advanced.code -ne 0) {
            Stop-Install 'advance-failed' 'git-checkout-failed' "git checkout ${pinned} failed: $($advanced.lines -join ' ')"
        }
        $installedRevision = $pinned
        $mutation = 'installed-checkout-advanced'
    }
    $stages.Add([pscustomobject]@{ name = 'advance'; status = 'passed'; detail = $pinned })

    $currentStage = 'install'
    New-Item -ItemType Directory -Path $SkillsDirectory -Force | Out-Null
    $SkillsDirectory = Get-NormalizedPath -Path $SkillsDirectory
    foreach ($skill in $skills) {
        $sourcePath = Get-ExpectedSkillPath -Relative $skill.path
        $existing = Get-ChildItem -Force -LiteralPath $SkillsDirectory -ErrorAction SilentlyContinue |
            Where-Object { [string]::Equals($_.Name, $skill.name, [System.StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1
        if ($existing) {
            $existingTarget = Get-JunctionTarget -Item $existing
            if ($existingTarget -and [string]::Equals($existingTarget, $sourcePath,
                [System.StringComparison]::OrdinalIgnoreCase)) {
                $connected.Add($skill.name)
                continue
            }
            $conflicts.Add($skill.name)
            Write-Warning "Conflict for '$($skill.name)': already exists and is not a junction to '$sourcePath'. Left unchanged."
            continue
        }
        New-Item -ItemType Junction -Path (Join-Path $SkillsDirectory $skill.name) -Target $sourcePath | Out-Null
        $connected.Add($skill.name)
    }

    # Old-name migration runs only on explicit opt-in and proven
    # ownership; anything else stays untouched without failing.
    $manifestPath = Join-Path $checkout 'skills\release\migrations.json'
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $migrations = @($manifest.migrations)
    } catch {
        $migrations = @()
        Write-Warning "Migrations manifest unreadable ($manifestPath): old links left unchanged."
    }
    foreach ($migration in $migrations) {
        $newSource = Join-Path $checkout (Join-Path 'skills' (([string]$migration.skill) -replace '/', '\'))
        if (-not (Test-Path -LiteralPath (Join-Path $newSource 'SKILL.md') -PathType Leaf)) { continue }
        $oldName = [string]$migration.fromSkill
        if ([string]::IsNullOrWhiteSpace($oldName)) { continue }
        $oldEntry = Get-ChildItem -Force -LiteralPath $SkillsDirectory -ErrorAction SilentlyContinue |
            Where-Object { [string]::Equals($_.Name, $oldName, [System.StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1
        if (-not $oldEntry) { continue }
        $oldTarget = Get-JunctionTarget -Item $oldEntry
        if (-not $oldTarget) { $leftUnchanged.Add($oldName); continue }
        if (-not (Test-OldPointerContent -TargetDir $oldTarget -Proof $migration.ownershipProof)) {
            $leftUnchanged.Add($oldName)
            continue
        }
        if (-not $AllowOldLinkMigration) { $leftUnchanged.Add($oldName); continue }
        Remove-ManagedJunctionSafely -Path (Join-Path $SkillsDirectory $oldEntry.Name) -ExpectedTarget $oldTarget
        $migrated.Add($oldEntry.Name)
    }
    if ($conflicts.Count -gt 0) {
        Stop-Install 'destination-conflict' 'foreign-entry-present' "Foreign entries left unchanged: $($conflicts -join ', ')"
    }
    $stages.Add([pscustomobject]@{ name = 'install'; status = 'passed' })

    $currentStage = 'readback'
    $verification = Read-Readback
    if (-not $verification.verified) {
        $problems = @()
        if (-not $verification.headMatchesPinnedRevision) {
            $problems += "HEAD $($verification.installedCheckoutHead) != pinned $pinned"
        }
        if (-not $verification.checkoutClean) { $problems += 'checkout is dirty' }
        if ($verification.junctions.mismatched.Count -gt 0) {
            $problems += "junctions: $($verification.junctions.mismatched -join ', ')"
        }
        if ($verification.missingSkillFiles.Count -gt 0) {
            $problems += "missing: $($verification.missingSkillFiles -join ', ')"
        }
        Stop-Install 'readback-mismatch' 'readback-values-differ' "Readback mismatch: $($problems -join '; ')" $verification
    }
    $stages.Add([pscustomobject]@{ name = 'readback'; status = 'passed' })
    $ok = $true
} catch {
    $exitCode = 1
    $reason = if ($_.Exception.Data.Contains('rolloutReason')) { [string]$_.Exception.Data['rolloutReason'] } else { 'unexpected-error' }
    $code = if ($_.Exception.Data.Contains('rolloutCode')) { [string]$_.Exception.Data['rolloutCode'] } else { 'unclassified' }
    if ($_.Exception.Data.Contains('rolloutVerification') -and $null -eq $verification) {
        $verification = $_.Exception.Data['rolloutVerification']
    }
    if ($currentStage -and @($stages | Where-Object { $_.name -eq $currentStage }).Count -eq 0) {
        $stages.Add([pscustomobject]@{ name = $currentStage; status = 'failed'; detail = $reason })
    }
    if ($mutation -eq 'none' -and -not [string]::IsNullOrWhiteSpace($checkout)) {
        try {
            $current = Invoke-RepoGit @('rev-parse', 'HEAD')
            $now = if ($current.code -eq 0 -and @($current.lines).Count -gt 0) { $current.lines[0].Trim() } else { '' }
            if ($now -ne '') { $installedRevision = $now }
            if ($now -ne $headBefore -and $now -ne '') { $mutation = 'installed-checkout-unexpected-head' }
        } catch { }
    }
    $failureAction = if ($mutation -ne 'none') { 'manual-repair' } elseif ($reason -eq 'readback-mismatch') { 'rerun-install' } else { 'none' }
    $failure = [pscustomobject]@{ reason = $reason; code = $code; message = $_.Exception.Message }
}

$receipt = [ordered]@{
    schema = 'task-runner/skill-rollout@1'
    ok = $ok
    requestedRevision = $Revision.Trim()
    pinnedRevision = $pinned
    installedRevision = $installedRevision
    installedCheckout = $checkout
    remote = [pscustomobject]@{ name = 'origin'; expectedUrl = $ExpectedRemoteUrl; url = $remoteUrl }
    allowOldLinkMigration = [bool]$AllowOldLinkMigration
    stages = @($stages.ToArray())
    connected = @($connected.ToArray() | Sort-Object)
    conflicts = @($conflicts.ToArray() | Sort-Object)
    migratedOldLinks = @($migrated.ToArray() | Sort-Object)
    leftUnchanged = @($leftUnchanged.ToArray() | Sort-Object)
    verification = $verification
    mutation = $mutation
    rollback = 'none'
    failureAction = $failureAction
    failure = $failure
}

$json = ConvertTo-Json -InputObject $receipt -Depth 12 -Compress
Write-Output $json
if (-not [string]::IsNullOrWhiteSpace($ReceiptPath)) {
    [System.IO.File]::WriteAllText($ReceiptPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))
}

exit $exitCode
