<#
.SYNOPSIS
Install the prebuilt Sts2Advisor mod (mods\Sts2Advisor) into a Windows Steam copy of Slay the Spire 2.

.DESCRIPTION
Windows counterpart of tools/install_advisor_mod.sh, but it does NOT build anything: it copies the
prebuilt Sts2Advisor.dll + Sts2Advisor.json from <repo>\mods\Sts2Advisor (refreshed on the Mac with
tools/package_advisor_mod.sh) into <GameDir>\mods\Sts2Advisor\, and writes
%APPDATA%\SlayTheSpire2\Sts2Advisor\config.json once (an existing one is never overwritten).

Re-run it after a Steam update or "verify integrity of game files" (either can remove mods\), and
after pulling a new mods\Sts2Advisor. Works on Windows PowerShell 5.1 (and PowerShell 7).

.PARAMETER GameDir
The "Slay the Spire 2" folder (the one that holds the game .exe). Default: found through the Steam
registry key and steamapps\libraryfolders.vdf.

.PARAMETER UserDir
Where config.json and the logs live. Default: %APPDATA%\SlayTheSpire2\Sts2Advisor.

.PARAMETER SetupPython
Also create <repo>\.venv (py -3.11 -m venv, falling back to python -m venv) and pip install
requirements-advisor.txt into it.

.PARAMETER Force
Install even though a Slay the Spire 2 process is running. Overwriting a DLL the game has loaded
corrupts it, so only use this when you know the game does not hold the file.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\install_advisor_mod.ps1 -SetupPython

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\install_advisor_mod.ps1 -GameDir "D:\SteamLibrary\steamapps\common\Slay the Spire 2"
#>
[CmdletBinding()]
param(
    [string]$GameDir,
    [string]$UserDir,
    [switch]$SetupPython,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$ModSource = Join-Path $Root 'mods\Sts2Advisor'
$ModFiles = @('Sts2Advisor.dll', 'Sts2Advisor.json')
$GameFolderName = 'Slay the Spire 2'

function Get-SteamLibraries {
    # Steam install dir (registry) plus every library listed in steamapps\libraryfolders.vdf.
    $libraries = New-Object System.Collections.Generic.List[string]
    $steam = $null
    try {
        $steam = (Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath
    } catch {
        $steam = $null
    }
    if (-not $steam) {
        foreach ($candidate in @("${env:ProgramFiles(x86)}\Steam", "$env:ProgramFiles\Steam")) {
            if ($candidate -and (Test-Path -LiteralPath $candidate)) { $steam = $candidate; break }
        }
    }
    if ($steam) {
        $steam = $steam -replace '/', '\'
        $libraries.Add($steam)
        $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $vdf) {
            foreach ($line in (Get-Content -LiteralPath $vdf)) {
                # new format:  "path"  "D:\\SteamLibrary"      old format:  "1"  "D:\\SteamLibrary"
                if ($line -match '^\s*"(?:path|\d+)"\s+"([^"]+)"') {
                    $libraries.Add(($Matches[1] -replace '\\\\', '\'))
                }
            }
        }
    }
    return @($libraries | Select-Object -Unique)
}

function Find-GameDir {
    foreach ($library in (Get-SteamLibraries)) {
        $candidate = Join-Path $library "steamapps\common\$GameFolderName"
        if (Test-Path -LiteralPath $candidate -PathType Container) { return $candidate }
    }
    return $null
}

# --- the prebuilt plugin must be here ---------------------------------------------------------
foreach ($name in $ModFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $ModSource $name))) {
        throw "Missing $ModSource\$name. Run 'git pull' in $Root (the prebuilt plugin is committed under mods\Sts2Advisor)."
    }
}

# --- the game folder --------------------------------------------------------------------------
if (-not $GameDir) {
    $GameDir = Find-GameDir
    if (-not $GameDir) {
        throw "Could not find '$GameFolderName' in any Steam library. Pass it explicitly: -GameDir ""D:\SteamLibrary\steamapps\common\$GameFolderName"""
    }
}
if (-not (Test-Path -LiteralPath $GameDir -PathType Container)) {
    throw "GameDir does not exist: $GameDir"
}
$GameDir = (Resolve-Path -LiteralPath $GameDir).Path
Write-Host "game folder: $GameDir"

# --- refuse while the game runs ---------------------------------------------------------------
# Overwriting the DLL the game has loaded corrupts it (BadImageFormatException 'Bad IL range' on every
# method that was not yet JIT-compiled) -- the same reason install_advisor_mod.sh refuses.
$running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^Slay\s?the\s?Spire\s?2' })
if ($running.Count -gt 0) {
    $names = ($running | ForEach-Object { "$($_.ProcessName) (pid $($_.Id))" }) -join ', '
    if ($Force) {
        Write-Warning "Slay the Spire 2 is running ($names); installing anyway because of -Force."
    } else {
        throw "Slay the Spire 2 is running ($names): overwriting the loaded Sts2Advisor.dll corrupts it. Quit the game first, then re-run (or pass -Force)."
    }
}

# --- copy the plugin --------------------------------------------------------------------------
$ModDest = Join-Path $GameDir 'mods\Sts2Advisor'
New-Item -ItemType Directory -Force -Path $ModDest | Out-Null
foreach ($name in $ModFiles) {
    Copy-Item -LiteralPath (Join-Path $ModSource $name) -Destination (Join-Path $ModDest $name) -Force
}
Write-Host "installed to $ModDest"
Get-ChildItem -LiteralPath $ModDest | Format-Table Name, Length, LastWriteTime -AutoSize | Out-String | Write-Host

$buildInfo = Join-Path $ModSource 'BUILD_INFO.txt'
if (Test-Path -LiteralPath $buildInfo) {
    Write-Host '--- BUILD_INFO.txt (the game version below must match yours) ---'
    Get-Content -LiteralPath $buildInfo | Select-Object -First 5 | ForEach-Object { Write-Host $_ }
}

# --- optional: the Python environment ---------------------------------------------------------
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if ($SetupPython) {
    $venvDir = Join-Path $Root '.venv'
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        $created = $false
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Write-Host 'creating .venv with: py -3.11 -m venv .venv'
            & py -3.11 -m venv $venvDir
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $VenvPython)) {
                $created = $true
            } else {
                Write-Warning 'py -3.11 did not work (is Python 3.11 installed?); falling back to python -m venv'
            }
        }
        if (-not $created) {
            if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
                throw 'No Python found. Install Python 3.11 from python.org (tick "Add python.exe to PATH" or keep the py launcher), then re-run with -SetupPython.'
            }
            & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -ne 0) {
                throw 'The python on PATH is older than 3.11 (requirements-advisor.txt pins numpy 2.4, which needs 3.11+). Install Python 3.11 and re-run.'
            }
            Write-Host 'creating .venv with: python -m venv .venv'
            & python -m venv $venvDir
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
                throw 'python -m venv failed.'
            }
        }
    } else {
        Write-Host ".venv already exists: $VenvPython"
    }
    Write-Host 'installing requirements-advisor.txt (numpy, gymnasium, scikit-learn) ...'
    & $VenvPython -m pip install -r (Join-Path $Root 'requirements-advisor.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed (see the output above).' }
}

# --- config.json (written once, never overwritten) --------------------------------------------
if (-not $UserDir) {
    if (-not $env:APPDATA) { throw 'APPDATA is not set; pass -UserDir.' }
    $UserDir = Join-Path $env:APPDATA 'SlayTheSpire2\Sts2Advisor'
}
New-Item -ItemType Directory -Force -Path $UserDir | Out-Null
$ConfigPath = Join-Path $UserDir 'config.json'
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    # Forward slashes: valid JSON without escaping, and .NET / Python on Windows accept them.
    $repoJson = $Root.Replace('\', '/')
    $config = @"
{
  "repo_path": "$repoJson",
  "python": "$repoJson/.venv/Scripts/python.exe",
  "port": 8767,
  "autostart": true,
  "hotkey": "F8",
  "autopilot_hotkey": "F9",
  "step_hotkey": "F10",
  "step_pause_seconds": 2.5,
  "panel_language": "auto",
  "map_policy": "hpaware"
}
"@
    # UTF-8 without BOM.
    [System.IO.File]::WriteAllText($ConfigPath, $config + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "wrote $ConfigPath"
} else {
    Write-Host "kept existing $ConfigPath (check that repo_path and python in it point at this checkout: $Root)"
}

# --- sanity checks on what the advice service needs --------------------------------------------
$problems = New-Object System.Collections.Generic.List[string]
if (-not (Test-Path -LiteralPath (Join-Path $Root 'agent\advisor_server.py'))) {
    $problems.Add("agent\advisor_server.py not found under $Root")
}
if (-not (Test-Path -LiteralPath (Join-Path $Root 'data\deck_predictor_v2.pkl'))) {
    $problems.Add('data\deck_predictor_v2.pkl is missing (git pull; the advice would silently differ without it)')
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $problems.Add("$VenvPython does not exist: re-run with -SetupPython, or set ""python"" in $ConfigPath")
} else {
    & $VenvPython -c "import numpy, gymnasium, sklearn"
    if ($LASTEXITCODE -ne 0) {
        $problems.Add('the .venv lacks numpy / gymnasium / scikit-learn: re-run with -SetupPython')
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host 'note: git is not on PATH; the advice service still works (it only shows the commit as "unknown").'
}

Write-Host ''
if ($problems.Count -gt 0) {
    foreach ($problem in $problems) { Write-Warning $problem }
    Write-Host 'The mod is installed, but the advice service cannot start until the warnings above are fixed.'
} else {
    Write-Host 'Done. Start the game; the panel appears at the bottom right (F8 hides it).'
}
Write-Host "Log: $UserDir\Sts2Advisor.log   Hot reload is off on Windows: restart the game to pick up a git pull."
