# Ownership proofs used by install.ps1. This file defines functions only.
function Test-OldPointerContent {
    param([Parameter(Mandatory)][string]$TargetDir, [Parameter(Mandatory)][object]$Proof)
    $candidate = Join-Path $TargetDir 'SKILL.md'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $false }
    $text = (Get-Content -LiteralPath $candidate -Raw -Encoding UTF8) -replace "`r`n", "`n"
    if ($text -notmatch '(?s)\A---\n(.*?)\n---(\n|\Z)') { return $false }
    if ($Matches[1] -notmatch "(?m)^name:\s*$([regex]::Escape([string]$Proof.frontmatterName))\s*$") { return $false }
    return $text.Contains([string]$Proof.bodyMustContain)
}

function Get-CheckoutProof {
    param([Parameter(Mandatory)][string]$TargetDir)
    $ErrorActionPreference = 'Continue'
    $top = @(git '-C' $TargetDir 'rev-parse' '--show-toplevel' 2>&1)
    if ($LASTEXITCODE -ne 0 -or @($top).Count -eq 0) { return $null }
    $root = "$($top[0])".Trim()
    if ([string]::IsNullOrWhiteSpace($root)) { return $null }
    $url = @(git '-C' $root 'remote' 'get-url' 'origin' 2>&1)
    if ($LASTEXITCODE -ne 0 -or @($url).Count -eq 0) { return $null }
    $remote = "$($url[0])".Trim()
    if ([string]::IsNullOrWhiteSpace($remote)) { return $null }
    return [pscustomobject]@{ root = $root; remote = $remote }
}

function Test-PatOwnership {
    param([Parameter(Mandatory)][string]$TargetDir, [Parameter(Mandatory)][object]$Proof)
    $checkoutProof = Get-CheckoutProof -TargetDir $TargetDir
    if ($null -eq $checkoutProof -or
        -not (Test-EquivalentRemote -Actual $checkoutProof.remote -Expected $ExpectedPatRemoteUrl)) { return $false }
    $relative = [string]$Proof.targetRelativePath
    if ([string]::IsNullOrWhiteSpace($relative)) { return $false }
    $expected = Get-NormalizedPath -Path (Join-Path $checkoutProof.root ($relative -replace '/', '\'))
    if (-not [string]::Equals((Get-NormalizedPath -Path $TargetDir), $expected,
        [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    return (Test-OldPointerContent -TargetDir $TargetDir -Proof $Proof)
}

function Get-SameNamePatMigration {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][object[]]$Migrations)
    return @($Migrations | Where-Object {
        [string]::Equals([string]$_.skill, $Name, [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$_.fromSkill, $Name, [System.StringComparison]::OrdinalIgnoreCase)
    } | Select-Object -First 1)[0]
}

function Get-PinnedMigrations {
    param([Parameter(Mandatory)][string]$Commit)
    $shown = Invoke-RepoGit @('show', "${Commit}:skills/release/migrations.json")
    if ($shown.code -ne 0) { return @() }
    try { return @((($shown.lines -join "`n") | ConvertFrom-Json).migrations) }
    catch { return @() }
}
