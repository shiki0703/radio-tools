# 新しい版に入れ替える。start.ps1 から呼ばれる。
#   ・プログラムのファイルだけを入れ替える
#   ・作業中のもの(data / results / 設定 / runtime)には触らない
#   ・入れ替えの前に、いまの中身を控えておく(失敗したら元に戻す)
param(
  [Parameter(Mandatory = $true)][string]$Root,
  [Parameter(Mandatory = $true)][string]$Source,
  [string]$Version = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

# 入れ替える対象(プログラムだけ)。ここに無いものは、そのまま残る。
$TARGETS = @('radio_sync', 'video-clipper-v7', '制作ハブ', 'launcher', 'はじめる.bat', 'README.md')
# それぞれのツールの中で、持ち越すもの(作業中のデータ・設定)
$KEEP = @('data', 'results', '.venv', 'hub_config.json', 'settings.json')

Write-Head "新しい版 $Version に入れ替えています"

$touched = $false   # ファイルに手を付けたかどうか(まだなら、戻す作業も要らない)
$work = Join-Path $env:TEMP ('radio-tools-up-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $work 'backup'
New-Item -ItemType Directory -Force -Path $work, $backup | Out-Null

try {
  # 1) 取ってくる
  $zip = Join-Path $work 'new.zip'
  Get-Download $Source $zip '新しい版'
  $out = Join-Path $work 'new'
  Expand-Archive -Path $zip -DestinationPath $out -Force

  # GitHub から落とすと、中に1つフォルダが挟まっている
  $inner = Get-ChildItem -Path $out -Directory
  if ($inner.Count -eq 1 -and -not (Test-Path (Join-Path $out '制作ハブ'))) { $out = $inner[0].FullName }
  if (-not (Test-Path (Join-Path $out '制作ハブ'))) { throw '取ってきた中身が想定と違います。' }

  # 2) 動いているツールを止める
  $stopped = Stop-RunningTools
  if ($stopped -gt 0) { Write-Note "動いていたツールを $stopped 個止めました" }

  # 3) いまの中身を控える
  Write-Step 'いまの中身を控えています'
  foreach ($name in $TARGETS) {
    $src = Join-Path $Root $name
    if (Test-Path $src) { Copy-Item $src (Join-Path $backup $name) -Recurse -Force }
  }

  # 4) 入れ替える(作業中のものは持ち越す)
  Write-Step 'ファイルを入れ替えています'
  $touched = $true
  foreach ($name in $TARGETS) {
    $new = Join-Path $out $name
    if (-not (Test-Path $new)) { continue }
    $dst = Join-Path $Root $name
    if (Test-Path $new -PathType Leaf) {
      Copy-Item $new $dst -Force
      continue
    }
    # 持ち越すものを、いったん脇に寄せる
    $park = Join-Path $work ('keep-' + $name)
    New-Item -ItemType Directory -Force -Path $park | Out-Null
    foreach ($k in $KEEP) {
      $from = Join-Path $dst $k
      if (Test-Path $from) { Move-Item $from (Join-Path $park $k) -Force }
    }
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item $new $dst -Recurse -Force
    foreach ($k in $KEEP) {
      $from = Join-Path $park $k
      if (Test-Path $from) {
        $to = Join-Path $dst $k
        if (Test-Path $to) { Remove-Item $to -Recurse -Force }
        Move-Item $from $to -Force
      }
    }
  }
  Write-Done "新しい版 $Version になりました"
} catch {
  Write-Bad "入れ替えられませんでした: $($_.Exception.Message)"
  $detail = 'ファイルにはまだ手を付けていないので、そのまま使えます。'
  if ($touched) {
    # 控えから元に戻す
    $restored = 0
    foreach ($name in $TARGETS) {
      $src = Join-Path $backup $name
      if (-not (Test-Path $src)) { continue }
      $dst = Join-Path $Root $name
      try {
        if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
        Copy-Item $src $dst -Recurse -Force
        $restored++
      } catch {}
    }
    $detail = "元の中身に戻しました($restored 個)。"
  }
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
  Stop-WithMessage '更新をやめました。' @(
    $detail,
    'もう一度「はじめる」を実行すれば、今までどおり使えます。',
    'ネットワークの調子が悪いときは、しばらく経ってからお試しください。')
}

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

# 5) 新しい start.ps1 で起動しなおす(更新の確認はもうしない)
Write-Note '起動しなおします…'
& (Join-Path $Root 'launcher\start.ps1') -SkipUpdate
