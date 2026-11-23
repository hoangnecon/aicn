#Requires -Version 5.1
<#
.SYNOPSIS
Link aicn (and vendored stage skills that are not installed yet) into the Windows agent skill folders.
Never overwrites an existing folder or junction. Optionally installs portable ffmpeg/ffprobe, rubberband, and node.
#>
[CmdletBinding()]
param(
    [switch]$InstallTools
)

$ErrorActionPreference = 'Stop'
$SK = Split-Path -Parent $MyInvocation.MyCommand.Path
$localBin = Join-Path $env:USERPROFILE ".local\bin"
$localNode = Join-Path $env:USERPROFILE ".local\node"

if ($InstallTools) {
    $ProgressPreference = 'SilentlyContinue'
    New-Item -ItemType Directory -Force -Path $localBin, $localNode | Out-Null
    $stage = Join-Path $env:TEMP "aicn_install_stage"
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    try {
        if (-not (Test-Path (Join-Path $localBin "ffmpeg.exe")) -or -not (Test-Path (Join-Path $localBin "ffprobe.exe"))) {
            Write-Host "install ffmpeg + ffprobe -> $localBin"
            $ffZip = Join-Path $stage "ffmpeg.zip"
            Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $ffZip
            Expand-Archive -Path $ffZip -DestinationPath (Join-Path $stage "ffmpeg") -Force
            Get-ChildItem (Join-Path $stage "ffmpeg") -Recurse -Include "ffmpeg.exe", "ffprobe.exe" | ForEach-Object {
                Copy-Item $_.FullName -Destination (Join-Path $localBin $_.Name) -Force
            }
        }
        if (-not (Test-Path (Join-Path $localBin "rubberband.exe"))) {
            Write-Host "install rubberband -> $localBin"
            $rbZip = Join-Path $stage "rubberband.zip"
            Invoke-WebRequest "https://breakfastquay.com/files/releases/rubberband-4.0.0-gpl-executable-windows.zip" -OutFile $rbZip
            Expand-Archive -Path $rbZip -DestinationPath (Join-Path $stage "rubberband") -Force
            Get-ChildItem (Join-Path $stage "rubberband") -Recurse -File | Where-Object { $_.Extension -in ".exe", ".dll" } | ForEach-Object {
                Copy-Item $_.FullName -Destination (Join-Path $localBin $_.Name) -Force
            }
        }
        if (-not (Test-Path (Join-Path $localNode "node.exe"))) {
            Write-Host "install node LTS -> $localNode"
            $nodeIdx = Invoke-RestMethod "https://nodejs.org/dist/index.json"
            $ltsVer = ($nodeIdx | Where-Object { $_.lts } | Select-Object -First 1).version
            $nodeZip = Join-Path $stage "node.zip"
            Invoke-WebRequest "https://nodejs.org/dist/$ltsVer/node-$ltsVer-win-x64.zip" -OutFile $nodeZip
            Expand-Archive -Path $nodeZip -DestinationPath (Join-Path $stage "node") -Force
            $nodeRoot = (Get-ChildItem (Join-Path $stage "node") -Directory | Select-Object -First 1).FullName
            if (Test-Path $localNode) { Remove-Item -Recurse -Force $localNode }
            Move-Item $nodeRoot $localNode
        }
        $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
        $parts = if ($userPath) { $userPath -split ';' | Where-Object { $_ -ne '' } } else { @() }
        foreach ($d in @($localBin, $localNode)) {
            if ($parts -notcontains $d) { $parts += $d }
        }
        [Environment]::SetEnvironmentVariable("PATH", ($parts -join ';'), "User")
    } finally {
        if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    }
}

$dests = @(
    (Join-Path $env:USERPROFILE ".claude\skills"),
    (Join-Path $env:USERPROFILE ".gemini\config\skills")
)

foreach ($dest in $dests) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $aicnLink = Join-Path $dest "aicn"
    if (Test-Path $aicnLink) {
        Write-Host "keep   $aicnLink"
    } else {
        New-Item -ItemType Junction -Path $aicnLink -Target $SK | Out-Null
        Write-Host "link   $aicnLink"
    }
    Get-ChildItem (Join-Path $SK "vendor") -Directory | ForEach-Object {
        $vLink = Join-Path $dest $_.Name
        if (Test-Path $vLink) {
            Write-Host "keep   $vLink"
        } else {
            New-Item -ItemType Junction -Path $vLink -Target $_.FullName | Out-Null
            Write-Host "link   $vLink"
        }
    }
}

function Find-AicnTool([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @($localBin, $localNode, "C:\Program Files\nodejs")) {
        $cand = Join-Path $dir "$Name.exe"
        if (Test-Path $cand) { return $cand }
    }
    return $null
}

$py = $env:AICN_PY
if (-not $py) { $py = [Environment]::GetEnvironmentVariable("AICN_PY", "User") }
if (-not $py -or -not (Test-Path $py)) {
    foreach ($c in @(
        "C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe",
        (Join-Path $env:USERPROFILE "Tools\VieNeu-TTS\.venv\Scripts\python.exe")
    )) {
        if (Test-Path $c) { $py = $c; break }
    }
}
if (-not $py) {
    $cmdPy = Get-Command python -ErrorAction SilentlyContinue
    if ($cmdPy) { $py = $cmdPy.Source }
}

Write-Host "python $(if ($py) { $py } else { 'MISSING (set AICN_PY)' })"
$missingTools = @()
foreach ($t in @("ffmpeg", "ffprobe", "rubberband", "node", "uvx")) {
    $p = Find-AicnTool $t
    if ($p) {
        Write-Host ("ok     {0,-10} {1}" -f $t, $p)
    } else {
        Write-Host ("miss   {0,-10} not found" -f $t)
        $missingTools += $t
    }
}
if ($missingTools.Count -gt 0) {
    Write-Host "hint   run '.\install.ps1 -InstallTools' to install portable ffmpeg/ffprobe, rubberband, and node into $localBin"
}

if ($py -and (Test-Path $py)) {
    $null = & $py (Join-Path $SK "scripts\aicn.py") --help
    if ($LASTEXITCODE -eq 0) {
        Write-Host "ok     aicn.py runs"
    } else {
        throw "aicn.py --help failed with exit code $LASTEXITCODE"
    }
} else {
    throw "No Python interpreter found; install Python or set AICN_PY"
}
