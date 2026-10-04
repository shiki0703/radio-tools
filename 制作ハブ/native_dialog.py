"""Windows の標準のファイル選択画面を、PowerShell を通して出す。

「はじめる」で入れる Python(埋め込み版)には tkinter が入っていないので、
そのときはこちらを使う。tkinter.filedialog と同じ呼び方で使えるようにしてある
(askopenfilename / askopenfilenames / asksaveasfilename / askdirectory)。
Windows に最初から入っている仕組みだけを使うので、追加で入れるものはない。
"""
import base64
import json
import os
import subprocess

NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
LAST = {'front': None}      # 試すとき用

# 画面の文字(題名など)は JSON にして渡す。スクリプト本体は英数字だけにしておく
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$o = ConvertFrom-Json '__OPTIONS__'

if ($o.mode -eq 'folder') {
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description = $o.title
  $d.ShowNewFolderButton = $true
  if ($o.initialdir) { $d.SelectedPath = $o.initialdir }
} else {
  if ($o.mode -eq 'save') {
    $d = New-Object System.Windows.Forms.SaveFileDialog
    $d.OverwritePrompt = $true
    $d.AddExtension = $true
    if ($o.defaultext) { $d.DefaultExt = $o.defaultext.TrimStart('.') }
  } else {
    $d = New-Object System.Windows.Forms.OpenFileDialog
    $d.Multiselect = [bool]$o.multiple
    $d.CheckFileExists = $true
  }
  $d.Title = $o.title
  if ($o.filter) { $d.Filter = $o.filter }
  if ($o.initialfile) { $d.FileName = $o.initialfile }
  if ($o.initialdir) { $d.InitialDirectory = $o.initialdir }
}

if ($o.dry) {
  # 試すとき用:画面は出さず、組み立てた中身だけを返す
  $info = @{ type = $d.GetType().Name; title = $(if ($o.mode -eq 'folder') { $d.Description } else { $d.Title }) }
  if ($o.mode -ne 'folder') { $info.filter = $d.Filter; $info.filename = $d.FileName }
  if ($o.mode -eq 'open') { $info.multiple = $d.Multiselect }
  if ($o.mode -eq 'save') { $info.defaultext = $d.DefaultExt }
  [Console]::Out.Write((ConvertTo-Json -InputObject $info -Compress))
  exit 0
}

Add-Type -Namespace ND -Name W -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint pid);
[DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool BringWindowToTop(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr h);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern System.IntPtr FindWindowEx(System.IntPtr parent, System.IntPtr after, string cls, string title);
[DllImport("user32.dll")] public static extern bool PostMessage(System.IntPtr h, uint msg, System.IntPtr w, System.IntPtr l);
[DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
'@

# いま手前にある窓(ブラウザ)と入力をつないでから、こちらを手前に出す。
# Windows は、裏で動くプログラムが勝手に窓を手前に出すことを止めるため
function Bring-Front($h) {
  $fg = [ND.W]::GetForegroundWindow()
  $other = [uint32]0
  $fgThread = [ND.W]::GetWindowThreadProcessId($fg, [ref]$other)
  $me = [ND.W]::GetCurrentThreadId()
  if ($fgThread -ne 0 -and $fgThread -ne $me) { [void][ND.W]::AttachThreadInput($me, $fgThread, $true) }
  [void][ND.W]::BringWindowToTop($h)
  [void][ND.W]::SetForegroundWindow($h)
  if ($fgThread -ne 0 -and $fgThread -ne $me) { [void][ND.W]::AttachThreadInput($me, $fgThread, $false) }
}

# この処理が出した、見えている選択画面(なければ 0)
function Find-Dialog {
  $h = [System.IntPtr]::Zero
  while ($true) {
    $h = [ND.W]::FindWindowEx([System.IntPtr]::Zero, $h, '#32770', [NullString]::Value)   # $null だと空の文字列として渡ってしまう
    if ($h -eq [System.IntPtr]::Zero) { return $h }
    $procId = [uint32]0
    [void][ND.W]::GetWindowThreadProcessId($h, [ref]$procId)
    if ($procId -eq $PID -and [ND.W]::IsWindowVisible($h)) { return $h }
  }
}

# 見えない「いちばん手前」の窓を親にして、ブラウザの後ろに隠れないようにする
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.StartPosition = 'Manual'
$owner.Location = New-Object System.Drawing.Point(-32000, -32000)
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show()
Bring-Front $owner.Handle

# 選択画面が出たら、それ自体も手前に出して、すぐ入力できるようにする
$script:tries = 0
$front = New-Object System.Windows.Forms.Timer
$front.Interval = 150
$front.Add_Tick({
  $script:tries++
  $h = Find-Dialog
  if ($h -ne [System.IntPtr]::Zero) {
    if ([ND.W]::GetForegroundWindow() -ne $h) { Bring-Front $h }
    $front.Stop()
  } elseif ($script:tries -gt 40) { $front.Stop() }
})
$front.Start()

if ($o.autoclose -gt 0) {
  # 試すとき用:少し待ってから、この処理の選択画面だけを閉じる(手前かどうかも書き残す)
  $timer = New-Object System.Windows.Forms.Timer
  $timer.Interval = [int]$o.autoclose
  $timer.Add_Tick({
    $h = Find-Dialog
    if ($h -eq [System.IntPtr]::Zero) { return }
    [Console]::Error.WriteLine('FRONT=' + ([ND.W]::GetForegroundWindow() -eq $h))
    if ($o.accept) { Bring-Front $h; [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
    else { [void][ND.W]::PostMessage($h, 0x0010, [System.IntPtr]::Zero, [System.IntPtr]::Zero) }
    $timer.Stop()
  })
  $timer.Start()
}

$paths = @()
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
  if ($o.mode -eq 'folder') { $paths = @($d.SelectedPath) }
  elseif ($o.mode -eq 'open' -and $o.multiple) { $paths = @($d.FileNames) }
  else { $paths = @($d.FileName) }
}
$owner.Close()
[Console]::Out.Write((ConvertTo-Json -InputObject @($paths) -Compress))
"""


def _filter(filetypes):
    """tkinter の filetypes([(名前, '*.mp3 *.wav'), ...])を、Windows の画面の形にする"""
    parts = []
    for label, patterns in filetypes or []:
        pats = patterns if isinstance(patterns, (list, tuple)) else str(patterns).split()
        pats = [p if p.startswith('*') else '*' + p for p in pats]
        parts.append(f"{label}|{';'.join(pats)}")
    return '|'.join(parts)


def _run(options):
    """PowerShell で画面を出して、選ばれたパス(試すときは組み立てた中身)を返す"""
    text = json.dumps(options, ensure_ascii=True).replace("'", "''")
    script = SCRIPT.replace('__OPTIONS__', text)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
                           '-EncodedCommand', encoded],
                          capture_output=True, creationflags=NO_WINDOW)
    out = done.stdout.decode('utf-8', errors='replace').strip()
    if done.returncode != 0 or not out:
        detail = done.stderr.decode('utf-8', errors='replace').strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f'PowerShell が終了コード {done.returncode} で終わりました')
    err = done.stderr.decode('utf-8', errors='replace')
    if 'FRONT=' in err:                  # 試すとき用:選択画面がいちばん手前に出ていたか
        LAST['front'] = 'FRONT=True' in err
    value = json.loads(out)
    if options.get('dry'):
        return value
    if isinstance(value, str):          # 1つだけのときに配列にならない版がある
        value = [value]
    return [os.path.normpath(p) for p in value or [] if p]


def _options(mode, title='', filetypes=None, **kw):
    return {'mode': mode, 'title': title or '', 'filter': _filter(filetypes),
            'initialfile': kw.get('initialfile') or '', 'initialdir': kw.get('initialdir') or '',
            'defaultext': kw.get('defaultextension') or '', 'multiple': bool(kw.get('multiple')),
            'dry': os.environ.get('NATIVE_DIALOG_DRY') == '1',
            'autoclose': int(os.environ.get('NATIVE_DIALOG_AUTOCLOSE') or 0),     # 試すとき用
            'accept': os.environ.get('NATIVE_DIALOG_ACCEPT') == '1'}


# ---- tkinter.filedialog と同じ呼び方(parent は使わない) ----

def askopenfilename(parent=None, title='', filetypes=None, **kw):
    paths = _run(_options('open', title, filetypes, **kw))
    return paths if isinstance(paths, dict) else (paths[0] if paths else '')


def askopenfilenames(parent=None, title='', filetypes=None, **kw):
    paths = _run(_options('open', title, filetypes, multiple=True, **kw))
    return paths if isinstance(paths, dict) else tuple(paths)


def asksaveasfilename(parent=None, title='', filetypes=None, **kw):
    paths = _run(_options('save', title, filetypes, **kw))
    return paths if isinstance(paths, dict) else (paths[0] if paths else '')


def askdirectory(parent=None, title='', **kw):
    paths = _run(_options('folder', title, None, **kw))
    return paths if isinstance(paths, dict) else (paths[0] if paths else '')
