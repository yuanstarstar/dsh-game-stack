param(
  [string]$Root = $PSScriptRoot,
  [switch]$Uninstall,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$profileDir = Join-Path $env:USERPROFILE '.dsh\profiles\desktop'
$profilePkg = Join-Path $profileDir 'package.json'

if (-not (Test-Path $profileDir)) { throw "DSH profile not found: $profileDir" }
if (-not (Test-Path $profilePkg)) { throw "profile package.json not found: $profilePkg" }
if (-not (Test-Path $Root)) { throw "Root not found: $Root" }

$ordered = @(
  '01-coach-perceive',
  '02-game-learn',
  '03-cv-recall',
  '04-live-bridge',
  '05-launcher'
)

function Get-PackageName([string]$bundleDir) {
  $p = Join-Path $bundleDir 'package.json'
  if (-not (Test-Path $p)) { return $null }
  $raw = Get-Content $p -Raw -Encoding UTF8
  $m = [regex]::Match($raw, '"name"\s*:\s*"([^"]+)"')
  if ($m.Success) { return $m.Groups[1].Value }
  return $null
}

$pairs = @()
foreach ($d in $ordered) {
  $bundle = Join-Path (Join-Path $Root $d) 'bundle'
  if (-not (Test-Path $bundle)) { Write-Warning "skip $d (no bundle folder)"; continue }
  $name = Get-PackageName $bundle
  if (-not $name) { Write-Warning "skip $d (package.json has no name)"; continue }
  $pairs += [pscustomobject]@{
    Dir    = $d
    Name   = $name
    Bundle = (Resolve-Path $bundle).Path
    Link   = Join-Path (Join-Path $profileDir 'node_modules') $name
  }
}
if ($pairs.Count -eq 0) { throw "no installable bundles found under $Root" }

Write-Host "profile : $profileDir"
Write-Host "bundles : $($pairs.Count)"
foreach ($p in $pairs) { Write-Host ("  {0,-28} {1}" -f $p.Name, $p.Bundle) }
Write-Host ""

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "$profilePkg.bak-$stamp"
if (-not $DryRun) { Copy-Item $profilePkg $backup -Force; Write-Host "backup  : $backup" }

if ($Uninstall) {
  foreach ($p in $pairs) {
    if (Test-Path $p.Link) {
      if ($DryRun) { Write-Host "would unlink $($p.Name)" }
      else { cmd /c rmdir "$($p.Link)" 2>$null | Out-Null; Write-Host "unlinked  $($p.Name)" }
    } else {
      Write-Host "absent    $($p.Name)"
    }
  }
} else {
  foreach ($p in $pairs) {
    $parent = Split-Path $p.Link -Parent
    if (-not (Test-Path $parent)) {
      if (-not $DryRun) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    }
    if (Test-Path $p.Link) {
      Write-Host "exists    $($p.Name)  (left untouched)"
    } elseif ($DryRun) {
      Write-Host "would link $($p.Link) -> $($p.Bundle)"
    } else {
      cmd /c mklink /J "$($p.Link)" "$($p.Bundle)" | Out-Null
      Write-Host "linked    $($p.Name)"
    }
  }
}

# --- update dsh.profile.bundles ---
$json = Get-Content $profilePkg -Raw | ConvertFrom-Json
if (-not $json.PSObject.Properties['dsh']) { $json | Add-Member -NotePropertyName dsh -NotePropertyValue ([pscustomobject]@{}) }
if (-not $json.dsh.PSObject.Properties['profile']) { $json.dsh | Add-Member -NotePropertyName profile -NotePropertyValue ([pscustomobject]@{}) }

$existing = @()
if ($json.dsh.profile.PSObject.Properties['bundles']) { $existing = @($json.dsh.profile.bundles | Where-Object { $_ }) }

$names = @($pairs | ForEach-Object { $_.Name })
if ($Uninstall) {
  $new = @($existing | Where-Object { $names -notcontains $_ })
} else {
  $new = @($existing)
  foreach ($n in $names) { if ($new -notcontains $n) { $new += $n } }
}

$json.dsh.profile | Add-Member -NotePropertyName bundles -NotePropertyValue @($new) -Force

if ($DryRun) {
  Write-Host ""
  Write-Host "[dry-run] bundles would become:"
  $new | ForEach-Object { Write-Host "  $_" }
} else {
  $out = $json | ConvertTo-Json -Depth 20
  [System.IO.File]::WriteAllText($profilePkg, $out, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host ""
  Write-Host "updated : $profilePkg"
  Write-Host "bundles : $($new.Count) entries"
}

Write-Host ""
Write-Host "Next: restart DSH (or Ctrl+R in the page), then check the sidebar entries."
Write-Host "Uninstall later with:  .\install-all.ps1 -Uninstall"
