# Power Hour Maker - portable setup.
# Everything lands inside this folder: bin\ (uv, ffmpeg, deno), runtime\ (python), .venv\
# Re-run any time; finished steps are skipped. Use -Update to refresh yt-dlp (YouTube breaks often),
# -DesktopShortcut to also put a shortcut on the desktop.
param([switch]$Update, [switch]$Force, [switch]$DesktopShortcut)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bin     = Join-Path $Root 'bin'
$Runtime = Join-Path $Root 'runtime'
$Dl      = Join-Path $Runtime 'downloads'
$Venv    = Join-Path $Root '.venv'
$Py      = Join-Path $Venv 'Scripts\python.exe'
$Uv      = Join-Path $Bin 'uv.exe'
$Headers = @{ 'User-Agent' = 'power-hour-maker-setup' }

New-Item -ItemType Directory -Force $Bin, $Runtime, $Dl | Out-Null

$env:UV_PYTHON_INSTALL_DIR = Join-Path $Runtime 'python'
$env:UV_CACHE_DIR          = Join-Path $Runtime 'uv-cache'
$env:UV_PYTHON_PREFERENCE  = 'only-managed'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# Downloads a zip into its own empty folder, extracts it next to it, returns the extract dir.
function Get-Zip([string]$Url, [string]$Name) {
    $dir = Join-Path $Dl $Name
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    New-Item -ItemType Directory -Force $dir | Out-Null
    $zip = Join-Path $dir 'package.zip'
    Write-Host "    downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $zip -Headers $Headers -UseBasicParsing
    $out = Join-Path $dir 'x'
    Expand-Archive -Path $zip -DestinationPath $out -Force
    return $out
}

function Get-LatestAsset([string]$Repo, [string]$Pattern) {
    $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers $Headers
    $asset = $rel.assets | Where-Object { $_.name -match $Pattern } | Sort-Object name -Descending | Select-Object -First 1
    if (-not $asset) { throw "No release asset matching '$Pattern' in $Repo" }
    return $asset.browser_download_url
}

# --- uv (manages python + packages) -----------------------------------------
if ($Force -or -not (Test-Path $Uv)) {
    Step 'Fetching uv'
    $x = Get-Zip 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' 'uv'
    Copy-Item (Get-ChildItem $x -Recurse -Filter 'uv.exe' | Select-Object -First 1).FullName $Uv -Force
}

# --- ffmpeg (BtbN GPL shared build: libx264 + NVENC/QSV/AMF) ----------------
# Pinned to 8.1: FFmpeg 9.x NVENC needs NVIDIA driver 610+, 8.1 works with the 58x/59x drivers.
$FfmpegSeries = '8\.1'
if ($Force -or -not (Test-Path (Join-Path $Bin 'ffmpeg.exe'))) {
    Step 'Fetching FFmpeg'
    $url = Get-LatestAsset 'BtbN/FFmpeg-Builds' "^ffmpeg-n$FfmpegSeries-latest-win64-gpl-shared-$FfmpegSeries\.zip$"
    $x = Get-Zip $url 'ffmpeg'
    $ffbin = (Get-ChildItem $x -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1).Directory.FullName
    Copy-Item (Join-Path $ffbin '*') $Bin -Force
}

# --- deno (JS runtime yt-dlp needs for YouTube) ------------------------------
if ($Force -or -not (Test-Path (Join-Path $Bin 'deno.exe'))) {
    Step 'Fetching Deno'
    $x = Get-Zip 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' 'deno'
    Copy-Item (Join-Path $x 'deno.exe') $Bin -Force
}

# --- python venv + packages ------------------------------------------------------
if ($Force -or -not (Test-Path $Py)) {
    Step 'Creating Python 3.12 environment'
    & $Uv venv $Venv --python 3.12 --allow-existing
    if ($LASTEXITCODE) { throw 'uv venv failed' }
}
Step 'Installing Python packages'
$upgrade = @()
if ($Update) { $upgrade = @('--upgrade-package', 'yt-dlp', '--upgrade-package', 'yt-dlp-ejs') }
& $Uv pip install --python $Py -r (Join-Path $Root 'requirements.txt') @upgrade
if ($LASTEXITCODE) { throw 'package install failed' }

Remove-Item -Recurse -Force $Dl -ErrorAction SilentlyContinue

# --- shortcut with the app icon (copy it to the desktop / pin it if you like) -----
Step 'Creating shortcut'
$shell = New-Object -ComObject WScript.Shell
$targets = @(Join-Path $Root 'Power Hour Maker.lnk')
if ($DesktopShortcut) { $targets += Join-Path ([Environment]::GetFolderPath('Desktop')) 'Power Hour Maker.lnk' }
foreach ($t in $targets) {
    $lnk = $shell.CreateShortcut($t)
    $lnk.TargetPath = Join-Path $Venv 'Scripts\pythonw.exe'
    $lnk.Arguments = '-m app'
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = Join-Path $Root 'web\icon.ico'
    $lnk.Description = 'Power Hour Maker'
    $lnk.Save()
}
Step 'Done. Launch with the "Power Hour Maker" shortcut (or Power Hour Maker.bat)'
