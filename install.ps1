#Requires -Version 5.1
<#
.SYNOPSIS
Install aicn and all 5 vendored stage skills into Windows Antigravity and Claude Code skill folders.
Uses real directory copies (not NTFS Junctions) so Antigravity on Windows discovers all 6/6 skills for
both the main agent and sub-agents. Safely upgrades old aicn junctions and refreshes managed copies on
re-run without overwriting unrelated user folders unless -Force is passed.
Optionally installs portable ffmpeg/ffprobe, rubberband, and node with -InstallTools.
#>
[CmdletBinding()]
param(
    [switch]$InstallTools,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$SK = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path)).TrimEnd('\')
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
        $env:PATH = "$localBin;$localNode;" + $env:PATH
    } finally {
        if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    }
}

function Copy-CleanTree([string]$Src, [string]$Dst) {
    New-Item -ItemType Directory -Force -Path $Dst | Out-Null
    $excludeDirs = @('.git', '__pycache__', '.cache')
    Get-ChildItem -LiteralPath $Src -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($excludeDirs -contains $_.Name) { return }
            Copy-CleanTree $_.FullName (Join-Path $Dst $_.Name)
        } else {
            if ($_.Extension -eq '.pyc' -or $_.Name -eq '.aicn-managed') { return }
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Dst $_.Name) -Force
        }
    }
}

function Sync-SkillDir([string]$Src, [string]$Dst) {
    $srcNorm = [System.IO.Path]::GetFullPath($Src).TrimEnd('\')
    $dstNorm = [System.IO.Path]::GetFullPath($Dst).TrimEnd('\')
    if ($srcNorm -ieq $dstNorm) {
        Write-Host "self   $dstNorm"
        return
    }

    $marker = Join-Path $dstNorm ".aicn-managed"
    if (Test-Path -LiteralPath $dstNorm) {
        $item = Get-Item -LiteralPath $dstNorm -Force
        $isReparse = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        if ($isReparse) {
            # Safely unlink junction/symlink without recursing into its target.
            [System.IO.Directory]::Delete($dstNorm, $false)
            Copy-CleanTree $srcNorm $dstNorm
            Set-Content -LiteralPath $marker -Value $srcNorm -Encoding UTF8
            Write-Host "upgrade $dstNorm (junction -> real dir)"
            return
        }
        if ((Test-Path -LiteralPath $marker) -or $Force) {
            Remove-Item -LiteralPath $dstNorm -Recurse -Force
            Copy-CleanTree $srcNorm $dstNorm
            Set-Content -LiteralPath $marker -Value $srcNorm -Encoding UTF8
            Write-Host "sync   $dstNorm"
            return
        }
        Write-Host "keep   $dstNorm (existing unmanaged folder; pass -Force to overwrite)"
        return
    }

    Copy-CleanTree $srcNorm $dstNorm
    Set-Content -LiteralPath $marker -Value $srcNorm -Encoding UTF8
    Write-Host "copy   $dstNorm"
}

$dests = @(
    (Join-Path $env:USERPROFILE ".claude\skills"),
    (Join-Path $env:USERPROFILE ".gemini\config\skills")
)

foreach ($dest in $dests) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Sync-SkillDir $SK (Join-Path $dest "aicn")
    Get-ChildItem (Join-Path $SK "vendor") -Directory | ForEach-Object {
        Sync-SkillDir $_.FullName (Join-Path $dest $_.Name)
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
$autoDetectedVenv = $false
if (-not $py -or -not (Test-Path $py)) {
    foreach ($c in @(
        "C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe",
        (Join-Path $env:USERPROFILE "Tools\VieNeu-TTS\.venv\Scripts\python.exe")
    )) {
        if (Test-Path $c) {
            $py = $c
            $autoDetectedVenv = $true
            break
        }
    }
}
if ($autoDetectedVenv) {
    [Environment]::SetEnvironmentVariable("AICN_PY", $py, "User")
    $env:AICN_PY = $py
    Write-Host "env    AICN_PY -> $py"
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

# Verify Antigravity skill discovery: all 6 skills must be real directories with SKILL.md
$agyRoot = Join-Path $env:USERPROFILE ".gemini\config\skills"
$expectedSkills = @("aicn") + @(Get-ChildItem (Join-Path $SK "vendor") -Directory | ForEach-Object { $_.Name })
$okSkills = 0
foreach ($s in $expectedSkills) {
    $sDir = Join-Path $agyRoot $s
    $sMd = Join-Path $sDir "SKILL.md"
    $isReparse = (Test-Path -LiteralPath $sDir) -and (((Get-Item -LiteralPath $sDir -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    if ((Test-Path -LiteralPath $sMd) -and (-not $isReparse)) {
        $okSkills++
    } else {
        throw "AGY skill check failed for '$s' at $sDir (missing SKILL.md or still a reparse point)"
    }
}
Write-Host "ok     AGY skills ready ($okSkills/$($expectedSkills.Count) real dirs in $agyRoot)"

if ($py -and (Test-Path $py)) {
    & $py -c "import Crypto, unicorn, requests" 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($InstallTools) {
            $uvCmd = Find-AicnTool "uv"
            if ($uvCmd) {
                Write-Host "install pycryptodome unicorn requests -> $py"
                & $uvCmd pip install --python $py pycryptodome unicorn requests
            } else {
                & $py -m pip install pycryptodome unicorn requests
            }
        } else {
            Write-Host "hint   missing SDK playback deps (pycryptodome/unicorn); run '.\install.ps1 -InstallTools'"
        }
    } else {
        Write-Host "ok     hg_sdk     pycryptodome + unicorn + requests"
    }
    $null = & $py (Join-Path $agyRoot "aicn\scripts\aicn.py") --help
    if ($LASTEXITCODE -eq 0) {
        Write-Host "ok     aicn.py runs"
    } else {
        throw "aicn.py --help failed with exit code $LASTEXITCODE"
    }
} else {
    throw "No Python interpreter found; install Python or set AICN_PY"
}
