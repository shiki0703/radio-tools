# 「はじめる.bat」から呼ばれる本体。
#   1. 足りないものを入れる(初回だけ時間がかかる)
#   2. 新しい版があれば知らせて、その場で更新する
#   3. 制作ハブを起動して、ブラウザで開く
param([switch]$SkipUpdate)

. (Join-Path $PSScriptRoot 'common.ps1')

$root = Get-RootDir
$host.UI.RawUI.WindowTitle = 'ラジオ制作ツール'

Write-Host ''
Write-Host '  ラジオ制作ツール' -ForegroundColor White
$local = Get-LocalVersion
if ($local) { Write-Host "  いまの版 $($local.version)" -ForegroundColor DarkGray }

# ---------- 1. 足りないものを入れる ----------

$first = -not (Test-Path (Get-PythonExe))
Write-Head '準備を確かめています'

try {
  if ($first) {
    Write-Step 'はじめての起動です。必要なものを取ってきます(3分ほど)。'
    Write-Note 'PC の設定は変えません。このフォルダの中だけに入ります。'
    Write-Step 'Python を用意しています'
    Install-Python
    Write-Done 'Python'
  }

  if (-not (Test-Packages)) {
    Write-Step '音と文字を扱う部品を入れています(1分ほど)'
    Install-Packages
    if (-not (Test-Packages)) { throw '部品を入れましたが、うまく読み込めませんでした。' }
    Write-Done '音と文字を扱う部品'
  }

  if (-not (Test-Path (Get-FfmpegExe))) {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
      Write-Done 'FFmpeg(この PC に入っているものを使います)'
    } else {
      Write-Step 'FFmpeg を用意しています'
      Install-Ffmpeg
      Write-Done 'FFmpeg'
    }
  }
  if (-not $first) { Write-Done '準備はできています' }
} catch {
  Stop-WithMessage '必要なものを用意できませんでした。' @(
    "理由: $($_.Exception.Message)",
    'インターネットにつながっているか確かめて、もう一度「はじめる」を実行してください。',
    '会社のネットワークなど、通信が制限されている場所では入れられないことがあります。')
}

# ---------- 2. 新しい版があるか ----------

if (-not $SkipUpdate -and $local -and $local.check) {
  Write-Head '新しい版がないか見ています'
  try {
    $remote = Invoke-RestMethod -Uri $local.check -TimeoutSec 8 -UseBasicParsing
    if ($remote.version -and ($remote.version -ne $local.version)) {
      Write-Host ''
      Write-Host "  新しい版があります: $($remote.version)" -ForegroundColor Yellow
      if ($remote.notes) { foreach ($n in $remote.notes) { Write-Host "    ・$n" -ForegroundColor Gray } }
      Write-Host ''
      Write-Host '  いま更新しますか? [Y] 更新する  [N] あとで  ' -NoNewline -ForegroundColor White
      $answer = Read-Host
      if ($answer -notmatch '^[nN]') {
        & (Join-Path $PSScriptRoot 'update.ps1') -Root $root -Source $remote.download -Version $remote.version
        exit $LASTEXITCODE
      }
      Write-Note '次に起動したときに、また聞きます。'
    } else {
      Write-Done '最新の版です'
    }
  } catch {
    Write-Note 'いまは確かめられませんでした(ネットにつながっていないときは、そのまま使えます)。'
  }
}

# ---------- 3. 起動する ----------

Write-Head '起動しています'
$hub = Join-Path $root '制作ハブ\hub.py'
if (-not (Test-Path $hub)) {
  Stop-WithMessage '制作ハブが見つかりませんでした。' @(
    "探した場所: $hub",
    'フォルダの中身がそろっているか確かめてください。')
}

Start-Process -FilePath (Get-PythonwExe) -ArgumentList @($hub) -WorkingDirectory (Split-Path $hub)
Write-Done 'ブラウザで開きます'
if (New-DesktopShortcut) { Write-Done 'デスクトップに「ラジオ制作ツール」を置きました(次からはここから開けます)' }
Write-Note '次からは、この窓はすぐ閉じます。'
Start-Sleep -Seconds 2
