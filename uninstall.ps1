param(
    [switch]$NoPause,
    [switch]$Yes,
    [switch]$DeleteLibrary,
    [switch]$NoShortcut,
    [string]$InstallDir
)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Good($text) { Write-Host "  $text" -ForegroundColor Green }
function Note($text) { Write-Host "  $text" -ForegroundColor Yellow }
function Pause { if (-not $NoPause) { Read-Host "  Press Enter to close" } }

Write-Host ""
Write-Host "  Remove ClipForge" -ForegroundColor White
Say "  -------------------------------"

# Work out which copy to remove: the one this file sits in, or the installed one.
$installed = if ($InstallDir) { $InstallDir } else { Join-Path $env:LOCALAPPDATA "ClipForge" }
$target = $here
if ((Test-Path (Join-Path $installed "clipforge.py")) -and
    ((Resolve-Path $here).Path -ne (Resolve-Path $installed).Path)) {
    $target = $installed
}
Say "  This removes the app from:"
Say "    $target" "White"

# Your clips are somewhere else entirely - find out where before touching anything.
$mediaRoot = $null
$configPath = Join-Path $target "config.json"
if (Test-Path $configPath) {
    try { $mediaRoot = (Get-Content $configPath -Raw | ConvertFrom-Json).media_root } catch { }
}
$clipCount = 0
if ($mediaRoot -and (Test-Path $mediaRoot)) {
    $clipCount = (Get-ChildItem -Path $mediaRoot -Recurse -Filter *.mp4 -ErrorAction SilentlyContinue |
                 Measure-Object).Count
    Say ""
    Say "  Your clips and transcripts are kept in:"
    Say "    $mediaRoot  ($clipCount video file(s))" "White"
}

# Deleting clips can never happen by accident: keeping them is the default, and
# -DeleteLibrary is the only way to ask for it without typing the phrase by hand.
$wipeLibrary = [bool]$DeleteLibrary
if (-not $Yes) {
    Say ""
    Say "  The app itself will be removed. Your clips are only deleted if you ask for it."
    $answer = Read-Host "  Type DELETE CLIPS if you also want them gone, or press Enter to keep them"
    if ($answer -eq "DELETE CLIPS") { $wipeLibrary = $true }
    Say ""
    $go = Read-Host "  Remove ClipForge now? Type y to go ahead"
    if ($go -notmatch '^\s*y') {
        Say "  Nothing was removed."
        Pause
        exit 0
    }
}

# Shortcuts: desktop and both start menus.
$shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "ClipForge.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ClipForge.lnk"),
    (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\ClipForge.lnk")
) | Where-Object { $_ } | Select-Object -Unique
if ($NoShortcut) { $shortcuts = @() }
foreach ($shortcut in $shortcuts) {
    if ($shortcut -and (Test-Path $shortcut)) {
        Remove-Item -LiteralPath $shortcut -Force -ErrorAction SilentlyContinue
        Good "Removed shortcut $([System.IO.Path]::GetFileName($shortcut))"
    }
}

if ($wipeLibrary -and $mediaRoot -and (Test-Path $mediaRoot)) {
    try {
        Remove-Item -LiteralPath $mediaRoot -Recurse -Force
        Good "Deleted $mediaRoot"
    } catch {
        Note "Couldn't delete $mediaRoot - something may still be open. Delete it by hand if you want it gone."
    }
} elseif ($mediaRoot) {
    Say "  Kept your clips in $mediaRoot"
}

# The folder holding this running script can only go once this process has exited, and
# Windows will refuse while any shell still has it as its working directory - so the
# removal is handed to a helper that retries from %TEMP%.
$self = (Resolve-Path $target).Path
Set-Location $env:TEMP
$helper = Join-Path $env:TEMP "clipforge-uninstall.cmd"
$lines = @(
    "@echo off",
    "for /l %%i in (1,1,30) do (",
    "  rd /s /q `"$self`" 2>nul",
    "  if not exist `"$self`" goto finish",
    "  ping -n 2 127.0.0.1 >nul",
    ")",
    ":finish",
    "del `"%~f0`" >nul 2>nul"
)
Set-Content -Path $helper -Value $lines -Encoding ASCII
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$helper`"" -WindowStyle Hidden
Good "Removing $self"

Write-Host ""
Write-Host "  ClipForge has been removed." -ForegroundColor Green
Say "  This window will close in a few seconds."
Start-Sleep -Seconds 4
exit 0
