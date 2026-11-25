#Requires -Version 5.1
<#
.SYNOPSIS
Install AICN v3.1+ as a native Antigravity plugin (%USERPROFILE%\.gemini\config\plugins\aicn),
sync the 7 user-invocable aicn-* skills into Claude Code (%USERPROFILE%\.claude\skills\aicn-*),
and retire the legacy 6 loose skill folders from .gemini\config\skills and .claude\skills.
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

$retiredLegacyFiles = @(
    "apply_channel_branding.py",
    "render_vertical_9x16.py",
    "split_compiled_episodes.py",
    "assemble_master_audio.py",
    "synthesize_and_audit_pacing.py"
)

function Remove-RetiredLegacyFiles([string]$Root) {
    foreach ($rf in $retiredLegacyFiles) {
        Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $rf -ErrorAction SilentlyContinue | ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force
            Write-Host "prune  $($_.FullName)"
        }
    }
}

function Copy-CleanTree([string]$Src, [string]$Dst) {
    New-Item -ItemType Directory -Force -Path $Dst | Out-Null
    $excludeDirs = @('.git', '__pycache__', '.cache', '.pytest_cache')
    $excludeFiles = @('.aicn-managed', 'SPEC_V3.md')
    Get-ChildItem -LiteralPath $Src -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($excludeDirs -contains $_.Name) { return }
            Copy-CleanTree $_.FullName (Join-Path $Dst $_.Name)
        } else {
            if ($_.Extension -eq '.pyc' -or ($excludeFiles -contains $_.Name)) { return }
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Dst $_.Name) -Force
        }
    }
    Remove-RetiredLegacyFiles $Dst
}

function Sync-ManagedDir([string]$Src, [string]$Dst) {
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

function Retire-LegacySkillDir([string]$DirPath) {
    if (-not (Test-Path -LiteralPath $DirPath)) { return }
    $norm = [System.IO.Path]::GetFullPath($DirPath).TrimEnd('\')
    if ($norm -ieq $SK) {
        Write-Host "skip   $norm (active source repo; relocate repo to .gemini\config\plugins\aicn to retire)"
        return
    }
    $item = Get-Item -LiteralPath $norm -Force
    $isReparse = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if ($isReparse) {
        [System.IO.Directory]::Delete($norm, $false)
        Write-Host "retire $norm (unlinked legacy junction)"
        return
    }
    if ((Test-Path -LiteralPath (Join-Path $norm ".aicn-managed")) -or $Force) {
        Remove-Item -LiteralPath $norm -Recurse -Force
        Write-Host "retire $norm"
    }
}

# 1. Install full plugin into Antigravity plugins root
$agyPluginsRoot = Join-Path $env:USERPROFILE ".gemini\config\plugins"
New-Item -ItemType Directory -Force -Path $agyPluginsRoot | Out-Null
$agyPluginDir = Join-Path $agyPluginsRoot "aicn"
Sync-ManagedDir $SK $agyPluginDir

[Environment]::SetEnvironmentVariable("AICN_HOME", $agyPluginDir, "User")
$env:AICN_HOME = $agyPluginDir

# 2. Retire old loose 6-skill layout from .gemini\config\skills and .claude\skills
$retiredLooseSkills = @(
    "aicn",
    "video-localization-orchestrator",
    "video-asr-visual-audit",
    "zh-vi-script-adapter",
    "vieneu-voice-pacing-mixer",
    "video-localization-qa-review"
)
foreach ($legacyRoot in @(
    (Join-Path $env:USERPROFILE ".gemini\config\skills"),
    (Join-Path $env:USERPROFILE ".claude\skills")
)) {
    foreach ($oldName in $retiredLooseSkills) {
        Retire-LegacySkillDir (Join-Path $legacyRoot $oldName)
    }
}

# 3. Sync the 7 intent-driven aicn-* skills into Claude Code (~/.claude/skills/aicn-*)
$claudeSkillsRoot = Join-Path $env:USERPROFILE ".claude\skills"
New-Item -ItemType Directory -Force -Path $claudeSkillsRoot | Out-Null
Get-ChildItem (Join-Path $agyPluginDir "skills") -Directory | ForEach-Object {
    Sync-ManagedDir $_.FullName (Join-Path $claudeSkillsRoot $_.Name)
}

function Find-AicnTool([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @($localBin, $localNode, "C:\Work\Tools\nodejs", "C:\Program Files\nodejs")) {
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

Write-Host "plugin $agyPluginDir"
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

# Verify Antigravity plugin integrity
if (-not (Test-Path -LiteralPath (Join-Path $agyPluginDir "plugin.json"))) {
    throw "AGY plugin check failed: missing plugin.json in $agyPluginDir"
}
$expectedSkills = @("aicn-scout", "aicn-create", "aicn-script", "aicn-produce", "aicn-audit", "aicn-fix", "aicn-clean")
foreach ($s in $expectedSkills) {
    $sMd = Join-Path $agyPluginDir "skills\$s\SKILL.md"
    if (-not (Test-Path -LiteralPath $sMd)) {
        throw "AGY plugin check failed: missing skills\$s\SKILL.md in $agyPluginDir"
    }
}
foreach ($ret in $retiredLegacyFiles) {
    $hit = Get-ChildItem -LiteralPath $agyPluginDir -Recurse -File -Filter $ret -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {
        throw "AGY plugin check failed: retired legacy file still present at $($hit.FullName)"
    }
}
Write-Host "ok     AGY plugin ready ($($expectedSkills.Count)/$($expectedSkills.Count) aicn-* skills in $agyPluginDir)"

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
    $null = & $py (Join-Path $agyPluginDir "scripts\aicn.py") --help
    if ($LASTEXITCODE -eq 0) {
        Write-Host "ok     aicn.py runs"
    } else {
        throw "aicn.py --help failed with exit code $LASTEXITCODE"
    }
} else {
    throw "No Python interpreter found; install Python or set AICN_PY"
}
