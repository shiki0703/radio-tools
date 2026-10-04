# 「はじめる.bat」と「更新」で共通して使う道具。
# 画面に出す文字はすべて日本語。PC の設定(環境変数やレジストリ)は変えない。

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # これが既定のままだと、ダウンロードが何倍も遅くなる
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

# 入れるもの
$script:PY_VERSION = '3.12.10'
$script:PY_URL = "https://www.python.org/ftp/python/$script:PY_VERSION/python-$script:PY_VERSION-embed-amd64.zip"
$script:GETPIP_URL = 'https://bootstrap.pypa.io/get-pip.py'
$script:FFMPEG_URLS = @(
  'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip',
  'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
)
# av は faster-whisper が音声を読むのに使う。19 で引数が変わり文字起こしが止まったので、確かめた 18 までにしておく
$script:PACKAGES = @('numpy>=2.0,<3', 'scipy>=1.14,<2', 'faster-whisper>=1.0.0', 'av>=11,<19', 'anthropic>=1.0,<2')

function Invoke-Quiet($exe, [string[]]$arguments) {
  # 外のプログラムを静かに動かして、終了コードと出た文字を返す。
  # PowerShell 5.1 は、外のプログラムが出す注意書きまで「エラー」として扱って
  # 止まってしまうので、ここでいったん受け止める。
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $exe @arguments 2>&1 | ForEach-Object { $_.ToString() }
    return @{ code = $LASTEXITCODE; text = ($out -join [Environment]::NewLine) }
  } finally { $ErrorActionPreference = $old }
}

function Write-Head($text) {
  Write-Host ''
  Write-Host "  $text" -ForegroundColor White
  Write-Host ('  ' + ('-' * 56)) -ForegroundColor DarkGray
}

function Write-Step($text) { Write-Host "  $text" -ForegroundColor Cyan }
function Write-Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }
function Write-Done($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  !   $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "  x   $text" -ForegroundColor Red }

function Stop-WithMessage($title, $hints) {
  Write-Host ''
  Write-Bad $title
  foreach ($h in $hints) {
    foreach ($line in ($h -split "`r?`n")) { Write-Host "      $line" -ForegroundColor Yellow }
  }
  Write-Host ''
  Write-Host ''
  exit 1
}

function Get-Download($url, $to, $label) {
  Write-Note "$label を取ってきています…"
  $sw = [Diagnostics.Stopwatch]::StartNew()
  Invoke-WebRequest -Uri $url -OutFile $to -UseBasicParsing -TimeoutSec 600
  $mb = (Get-Item $to).Length / 1MB
  Write-Note ("  {0:N0} MB / {1:N0} 秒" -f $mb, $sw.Elapsed.TotalSeconds)
}

function Get-DownloadAny($urls, $to, $label) {
  $last = $null
  foreach ($u in $urls) {
    try { Get-Download $u $to $label; return } catch { $last = $_; Write-Note '  別の入手先を試します…' }
  }
  throw $last
}

# ---------- 一式フォルダとその中身 ----------

function Get-RootDir { Split-Path -Parent $PSScriptRoot }
function Get-RuntimeDir { Join-Path (Get-RootDir) 'runtime' }
function Get-PythonExe { Join-Path (Get-RuntimeDir) 'python\python.exe' }
function Get-PythonwExe { Join-Path (Get-RuntimeDir) 'python\pythonw.exe' }
function Get-FfmpegExe { Join-Path (Get-RuntimeDir) 'bin\ffmpeg.exe' }

function Get-LocalVersion {
  $f = Join-Path $PSScriptRoot 'version.json'
  if (Test-Path $f) { return (Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json) }
  return $null
}

# ---------- Python ----------

function Install-Python {
  $dir = Join-Path (Get-RuntimeDir) 'python'
  $tmp = Join-Path $env:TEMP ('radio-tools-py-' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $zip = Join-Path $tmp 'python.zip'
    Get-DownloadAny @($script:PY_URL) $zip "Python $script:PY_VERSION"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dir -Force

    # 埋め込み版は、そのままでは pip でものを入れられないので、その道を開けておく
    $pth = Get-ChildItem -Path $dir -Filter '*._pth' | Select-Object -First 1
    $lines = Get-Content $pth.FullName | ForEach-Object { $_ -replace '^#\s*import site', 'import site' }
    if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
    Set-Content -Path $pth.FullName -Value $lines -Encoding ascii

    $getpip = Join-Path $tmp 'get-pip.py'
    Get-DownloadAny @($script:GETPIP_URL) $getpip 'pip'
    $r = Invoke-Quiet (Get-PythonExe) @($getpip, '--no-warn-script-location', '--quiet')
    if ($r.code -ne 0) { throw "pip を用意できませんでした。`n$($r.text)" }
  } finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Test-Packages {
  if (-not (Test-Path (Get-PythonExe))) { return $false }
  return ((Invoke-Quiet (Get-PythonExe) @('-c', 'import numpy, scipy, faster_whisper, anthropic')).code -eq 0)
}

function Install-Packages {
  $a = @('-m', 'pip', 'install', '--no-warn-script-location', '--disable-pip-version-check', '-q') + $script:PACKAGES
  $r = Invoke-Quiet (Get-PythonExe) $a
  if ($r.code -ne 0) { throw "必要な部品を入れられませんでした。`n$($r.text)" }
}

# ---------- ffmpeg ----------

function Install-Ffmpeg {
  $bin = Join-Path (Get-RuntimeDir) 'bin'
  $tmp = Join-Path $env:TEMP ('radio-tools-ff-' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $zip = Join-Path $tmp 'ffmpeg.zip'
    Get-DownloadAny $script:FFMPEG_URLS $zip 'FFmpeg(動画を書き出す部品)'
    $out = Join-Path $tmp 'x'
    Expand-Archive -Path $zip -DestinationPath $out -Force
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $found = 0
    Get-ChildItem -Path $out -Recurse -File | Where-Object { $_.Name -in @('ffmpeg.exe', 'ffprobe.exe') } | ForEach-Object {
      Copy-Item $_.FullName (Join-Path $bin $_.Name) -Force
      $found++
    }
    if ($found -lt 2) { throw 'FFmpeg の中身を取り出せませんでした。' }
  } finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# ---------- 動いているツールを止める ----------

function Stop-RunningTools {
  $root = (Get-RootDir).ToLower()
  $stopped = 0
  foreach ($name in @('pythonw.exe', 'python.exe')) {
    try { $list = Get-CimInstance Win32_Process -Filter "Name = '$name'" -ErrorAction Stop } catch { continue }
    foreach ($p in $list) {
      $line = ($p.CommandLine + ' ' + $p.ExecutablePath).ToLower()
      if ($line -like "*$root*") {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped++ } catch {}
      }
    }
  }
  if ($stopped -gt 0) { Start-Sleep -Milliseconds 800 }
  return $stopped
}

# ---------- デスクトップのアイコン ----------

function New-DesktopShortcut {
  # デスクトップに「ラジオ制作ツール」を置く(初回だけ)。すでにあれば何もしない。
  try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if (-not $desktop) { return $false }
    $link = Join-Path $desktop 'ラジオ制作ツール.lnk'
    if (Test-Path $link) { return $false }
    $target = Join-Path (Get-RootDir) 'はじめる.bat'
    if (-not (Test-Path $target)) { return $false }
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($link)
    $sc.TargetPath = $target
    $sc.WorkingDirectory = (Get-RootDir)
    $sc.Description = 'ラジオ制作ツール(制作ハブ・Radio Sync・動画クリッパー)'
    $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,137"
    $sc.Save()
    return $true
  } catch { return $false }
}
