<#
.SYNOPSIS
    Build a shareable zip: code + docs only, no credentials, no runtime data.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\pack.ps1
    powershell -ExecutionPolicy Bypass -File scripts\pack.ps1 -Out D:\share\copy-trading.zip
#>
param(
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
if (-not $Out) { $Out = Join-Path $root "dist\copy-trading-$stamp.zip" }

# directories to skip (relative to project root)
$excludeDirs = @(
    ".git", ".venv", "__pycache__", "dist", "node_modules",
    "Binance-Join-CopyTrader-Automation",
    "bot\data",
    "probe\out"
)
# individual files to skip
$excludeFiles = @(
    ".env", ".env.local",
    "session.json", "probe\session.json", "probe\listenkey.txt",
    "gui\state.json", "gui\gui.log"
)

$rootFull = (Resolve-Path $root).Path
$files = Get-ChildItem -Path $rootFull -Recurse -File -Force | Where-Object {
    $rel = $_.FullName.Substring($rootFull.Length + 1)
    $skip = $false
    foreach ($seg in ($rel -split '\\')) {
        if ($excludeDirs -contains $seg) { $skip = $true; break }
    }
    if (-not $skip) {
        foreach ($f in $excludeFiles) {
            if ($rel -ieq $f) { $skip = $true; break }
        }
    }
    if (-not $skip -and $rel -match '^probe\\_') { $skip = $true }   # scratch probes
    if (-not $skip -and $rel -match '^gui\\_') { $skip = $true }     # scratch tests
    if (-not $skip -and $rel -match '^bot\\data\\') { $skip = $true } # runtime data
    -not $skip
}

$stage = Join-Path ([IO.Path]::GetTempPath()) ("ct-pack-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
foreach ($f in $files) {
    $rel = $f.FullName.Substring($rootFull.Length + 1)
    $dest = Join-Path $stage $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
}

New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
if (Test-Path $Out) { Remove-Item -LiteralPath $Out -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $Out -Force
Remove-Item -LiteralPath $stage -Recurse -Force

# verify nothing sensitive slipped in
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($Out)
try {
    $names = @($zip.Entries | ForEach-Object { $_.FullName })
} finally {
    $zip.Dispose()
}
$leak = @($names | Where-Object {
    $_ -match '(^|/)\.env$' -or $_ -match 'session\.json$' -or
    $_ -match 'listenkey\.txt$' -or $_ -match 'Binance-Join-CopyTrader-Automation'
})

Write-Host ("saved : {0}" -f $Out)
Write-Host ("files : {0}   size: {1:N1} KB" -f $names.Count, ((Get-Item $Out).Length / 1KB))
if ($leak.Count -gt 0) {
    Write-Host "LEAK DETECTED - do not share:" -ForegroundColor Red
    $leak | ForEach-Object { Write-Host ("   " + $_) -ForegroundColor Red }
    exit 1
}
Write-Host "OK: no .env / session.json / legacy repo inside" -ForegroundColor Green
