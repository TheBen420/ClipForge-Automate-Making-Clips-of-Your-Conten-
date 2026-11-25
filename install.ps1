param(
    [switch]$NoShortcut,
    [switch]$DryRun,
    [switch]$NoPause,
    [string]$ShortcutDir
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Head($text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan }
function Good($text) { Write-Host "  $text" -ForegroundColor Green }
function Bad($text) { Write-Host "  $text" -ForegroundColor Red }
function Note($text) { Write-Host "  $text" -ForegroundColor Yellow }

function Pause {
    if (-not $NoPause) { Pause }
}

function Have-Winget {
    return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Find-Python {
    $candidates = @()
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    foreach ($version in @("313", "312", "311", "310")) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
    }
    $candidates += "C:\Python313\python.exe"
    $candidates += "C:\Python312\python.exe"
    foreach ($path in $candidates) {
        if (-not $path -or -not (Test-Path $path)) { continue }
        $probe = & $path -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)" 2>$null
        if ($probe -eq "1") { return $path }
    }
    return $null
}

function Find-Ffmpeg {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $pattern = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\*\**\bin\ffmpeg.exe"
    $hit = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}

Write-Host ""
Write-Host "  ClipForge setup" -ForegroundColor White
Say "  -------------------------------"
Say "  This puts everything ClipForge needs on this PC."
Say "  It takes about three minutes and needs internet."
if ($DryRun) { Note "Dry run: nothing will be installed." }

# ---------------------------------------------------------------- python
Head "1 of 4   Checking for Python"
$python = Find-Python
if ($python) {
    Good "Found Python at $python"
} else {
    Note "Python isn't on this PC yet - it's the language ClipForge is written in."
    if (-not (Have-Winget)) {
        Bad "I can't install it automatically on this version of Windows."
        Say ""
        Say "  Please install Python yourself, then run this file again:"
        Say "    https://www.python.org/downloads/"
        Say "  On the first screen, tick the box that says 'Add python.exe to PATH'."
        Pause
        exit 1
    }
    if ($DryRun) {
        Note "Would install Python 3.12 with winget."
        $python = "(would be installed)"
    } else {
        Say "  Installing Python now. This can take a minute..."
        winget install --id Python.Python.3.12 -e --silent `
            --accept-source-agreements --accept-package-agreements | Out-Null
        $python = Find-Python
        if (-not $python) {
            Bad "Python installed but I still can't find it."
            Say "  Close this window, then run this file again - Windows needs a fresh window to see it."
            Pause
            exit 1
        }
        Good "Python is installed."
    }
}

# ---------------------------------------------------------------- ffmpeg
Head "2 of 4   Checking for ffmpeg"
$ffmpeg = Find-Ffmpeg
if ($ffmpeg) {
    Good "Found ffmpeg at $ffmpeg"
} else {
    Note "ffmpeg isn't here yet - ClipForge uses it to cut and render the clips."
    if (-not (Have-Winget)) {
        Bad "I can't install it automatically on this version of Windows."
        Say ""
        Say "  Please install ffmpeg yourself, then run this file again:"
        Say "    https://www.gyan.dev/ffmpeg/builds/  (the 'release full' build)"
        Pause
        exit 1
    }
    if ($DryRun) {
        Note "Would install ffmpeg with winget."
    } else {
        Say "  Installing ffmpeg now. This can take a minute..."
        winget install --id Gyan.FFmpeg -e --silent `
            --accept-source-agreements --accept-package-agreements | Out-Null
        $ffmpeg = Find-Ffmpeg
        if (-not $ffmpeg) {
            Bad "ffmpeg installed but I still can't find it."
            Pause
            exit 1
        }
        Good "ffmpeg is installed."
    }
}

if ($DryRun) {
    Head "3 of 4   Would create its own Python folder here and install the libraries"
    Head "4 of 4   Would check the machine and add a ClipForge shortcut to the Desktop"
    Say ""
    Good "Dry run finished - the real run would continue from here."
    exit 0
}

# ---------------------------------------------------------------- libraries
Head "3 of 4   Installing the libraries ClipForge needs"
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (Test-Path $venvPython) {
    Good "Already set up from a previous run."
} else {
    Say "  Making a private copy of Python just for ClipForge (about 400 MB)..."
    & $python -m venv $venv
    if (-not (Test-Path $venvPython)) {
        Bad "Couldn't create it. Try running this file again, or as administrator."
        Pause
        exit 1
    }
}
Say "  Downloading what it needs - this is the slow part, and it shows its progress below."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Bad "Something went wrong downloading the libraries."
    Note "Check the internet connection, then run this file again."
    Pause
    exit 1
}
Good "Libraries installed."

# ---------------------------------------------------------------- doctor
Head "4 of 4   Checking this PC can run ClipForge"
& $venvPython (Join-Path $root "clipforge.py") doctor
if ($LASTEXITCODE -ne 0) {
    Note "The check above found something it doesn't like."
    Note "The most common cause is no working video encoder - send that text to whoever gave you this."
} else {
    Good "Everything checks out."
}

# ---------------------------------------------------------------- shortcut
if (-not $NoShortcut) {
    try {
        $desktop = if ($ShortcutDir) { $ShortcutDir } else {
            [Environment]::GetFolderPath("Desktop")
        }
        $pythonw = Join-Path $venv "Scripts\pythonw.exe"
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $desktop "ClipForge.lnk"))
        $link.TargetPath = $pythonw
        $link.Arguments = '"' + (Join-Path $root "clipforge_gui.py") + '"'
        $link.WorkingDirectory = $root
        $link.IconLocation = (Join-Path $root "icon.ico")
        $link.Description = "Turn streams into vertical clips"
        $link.Save()
        Say ""
        Good "Added a ClipForge shortcut to the desktop."
    } catch {
        Note "Couldn't add the desktop shortcut, but ClipForge itself is ready."
    }
}

Write-Host ""
Write-Host "  Done." -ForegroundColor Green
Say "  Double-click ClipForge on the desktop to open it."
Say "  Your clips will be saved in a ClipForge folder on your D: or C: drive."
Say ""
Pause
