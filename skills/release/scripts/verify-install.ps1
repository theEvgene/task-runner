<#
Read-only verification for installed task-runner skills. Changes nothing:
resolves the revision locally, re-derives the expected links from
`git ls-tree <pinned> skills/`, and compares them with the junctions on
disk. Exits 0 when everything matches, 1 otherwise.
#>
[CmdletBinding()]
param(
    [string]$Revision = 'origin/main',
    [string]$SkillsDirectory = (Join-Path $HOME '.agents\skills')
)

$ErrorActionPreference = 'Stop'

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

$checkout = Get-NormalizedPath -Path (Join-Path $PSScriptRoot '..\..\..')
$errors = [System.Collections.Generic.List[string]]::new()

$pinned = @(git '-C' $checkout 'rev-parse' '--verify' '--quiet' '--end-of-options' "$($Revision.Trim())^{commit}" 2>&1)
if ($LASTEXITCODE -ne 0 -or @($pinned).Count -eq 0 -or $pinned[0] -notmatch '^[0-9a-f]{40}$') {
    $errors.Add("Revision '$Revision' does not resolve to a local commit.")
} else {
    $pinned = $pinned[0].Trim()
    $head = @(git '-C' $checkout 'rev-parse' 'HEAD' 2>&1)
    if ($LASTEXITCODE -ne 0 -or $head[0].Trim() -ne $pinned) {
        $errors.Add("Checkout HEAD $($head -join ' ') != pinned $pinned.")
    }
    $listing = @(git '-C' $checkout 'ls-tree' '-r' '--name-only' $pinned 'skills/' 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $errors.Add("ls-tree failed for ${pinned}: $($listing -join ' ').")
    } else {
        $expected = @{}
        foreach ($line in $listing) {
            if ($line -notmatch '^skills/([^/]+)(?:/([^/]+))?/SKILL\.md$') { continue }
            $name = if ($Matches[2]) { $Matches[2] } else { $Matches[1] }
            $relative = if ($Matches[2]) { "skills/$($Matches[1])/$($Matches[2])" } else { "skills/$($Matches[1])" }
            $expected[$name] = Get-NormalizedPath -Path (Join-Path $checkout ($relative -replace '/', '\'))
        }
        foreach ($name in ($expected.Keys | Sort-Object)) {
            $link = Join-Path $SkillsDirectory $name
            $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
            $actual = if ($item) { Get-JunctionTarget -Item $item } else { '' }
            if (-not [string]::Equals($actual, $expected[$name], [System.StringComparison]::OrdinalIgnoreCase)) {
                $errors.Add("Skill ${name}: expected junction to $($expected[$name]); actual $actual.")
            }
        }
        if ($expected.Count -eq 0) { $errors.Add("No skills discovered at $pinned.") }
    }
}

$status = @(git '-C' $checkout 'status' '--porcelain=v1' '--untracked-files=all' 2>&1)
if ($LASTEXITCODE -ne 0 -or @($status).Count -ne 0) {
    $errors.Add('Checkout is not clean.')
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host "Verified: pinned $pinned, $($expected.Count) skill link(s), checkout clean."
