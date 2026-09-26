import copy
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import core


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Radio Sync — 試作版 0.1')
        self.geometry('1120x680')
        self.project = {'version': 1, 'audios': [], 'videos': [], 'still': '', 'episodes': []}
        self.events = queue.Queue()
        self.busy = False
        self.preview_dir = tempfile.TemporaryDirectory(prefix='radio-preview-')
        self.buttons = []
        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=12, pady=10)
        for title, command in [('音声を追加', lambda: self.add('audios')), ('動画を追加', lambda: self.add('videos')),
            ('自動照合', self.analyze), ('開く', self.open), ('保存', self.save), ('静止画', self.still),
            ('プレビュー', self.preview), ('全話出力', self.export)]:
            b = ttk.Button(bar, text=title, command=command)
            b.pack(side='left', padx=2)
            self.buttons.append(b)
        self.summary = tk.StringVar()
        ttk.Label(self, textvariable=self.summary).pack(anchor='w', padx=12)
        ttk.Label(self, text='要確認は初期状態で静止画。選択区間を編集・承認してください。スコアは正解確率ではありません。').pack(anchor='w', padx=12, pady=6)
        self.tree = ttk.Treeview(self, columns=('start', 'end', 'video', 'source', 'score', 'state'), show='tree headings')
        for key, text in [('#0', '話 / 区間'), ('start', '話内開始'), ('end', '話内終了'), ('video', '動画'),
                          ('source', '動画内開始'), ('score', '一致スコア'), ('state', '状態')]:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=135 if key != 'video' else 220)
        self.tree.pack(fill='both', expand=True, padx=12)
        self.tree.bind('<Double-1>', lambda e: self.edit())
        row = ttk.Frame(self)
        row.pack(fill='x', padx=12, pady=8)
        for label, action in [('区間を編集 / 承認', self.edit), ('−1フレーム', lambda: self.nudge(-1/30)), ('＋1フレーム', lambda: self.nudge(1/30))]:
            b = ttk.Button(row, text=label, command=action)
            b.pack(side='left', padx=3)
            self.buttons.append(b)
        self.status = tk.StringVar(value='元データは変更しません。まず音声と動画を追加してください。')
        ttk.Label(self, textvariable=self.status, wraplength=1080).pack(fill='x', padx=12, pady=8)
        self.after(100, self.poll)
        self.refresh()

    def refresh(self):
        p = self.project
        self.summary.set(f"音声 {len(p['audios'])} 本 / 動画 {len(p['videos'])} 本 / {len(p['episodes'])} 話")
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(p['episodes']):
            root = self.tree.insert('', 'end', iid=str(i), text=Path(e['audio']).name, open=True)
            for j, s in enumerate(e['segments']):
                self.tree.insert(root, 'end', iid=f'{i}:{j}', text=f'区間 {j+1}', values=(
                    f"{s['start']:.3f}", f"{s['end']:.3f}", Path(s['video']).name if s['video'] else 'なし',
                    f"{s['source']:.3f}", f"{s['score']:.3f}", ('映像' if s['enabled'] else '静止画')+' / '+s['status']))

    def add(self, key):
        files = filedialog.askopenfilenames(title='ラジオ音声' if key == 'audios' else '収録動画')
        self.project[key] = list(dict.fromkeys(self.project[key] + list(files)))
        self.refresh()

    def job(self, function):
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.state(['disabled'])
        def work():
            try:
                result = function(lambda s: self.events.put(('status', s)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                    continue
                self.busy = False
                for b in self.buttons:
                    b.state(['!disabled'])
                if kind == 'error':
                    messagebox.showerror('処理できませんでした', value)
                    self.status.set('エラー。詳細を確認してください。')
                else:
                    if isinstance(value, dict):
                        self.project = value
                        self.refresh()
                    elif isinstance(value, Path):
                        if os.name == 'nt':
                            os.startfile(str(value))
                        else:
                            subprocess.Popen(['xdg-open', str(value)])
                    self.status.set('処理完了。必要な微調整を行い、プロジェクトを保存してください。')
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def analyze(self):
        p = self.project
        if not p['audios'] or not p['videos']:
            return messagebox.showinfo('素材不足', '音声と動画を追加してください。')
        if p['episodes'] and not messagebox.askyesno('再解析', '既存の微調整を置き換えます。保存済みですか？'):
            return
        audios, videos, still = list(p['audios']), list(p['videos']), p['still']
        def task(log):
            result = core.analyze(audios, videos, log)
            result['still'] = still
            return result
        self.job(task)

    def open(self):
        path = filedialog.askopenfilename(filetypes=[('Project', '*.json')])
        if path:
            try:
                self.project = core.load(path)
                self.refresh()
            except Exception as exc:
                messagebox.showerror('読み込みエラー', str(exc))

    def save(self):
        path = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('Project', '*.json')])
        if path:
            try:
                core.save(self.project, path)
            except Exception as exc:
                messagebox.showerror('保存エラー', str(exc))

    def still(self):
        path = filedialog.askopenfilename(filetypes=[('Image', '*.png *.jpg *.jpeg')])
        if path:
            self.project['still'] = path

    def selected(self):
        items = self.tree.selection()
        if not items:
            return None
        return [int(x) for x in items[0].split(':')]

    def edit(self):
        if self.busy:
            return
        index = self.selected()
        if not index or len(index) != 2:
            return
        s = self.project['episodes'][index[0]]['segments'][index[1]]
        win = tk.Toplevel(self)
        win.title('区間の微調整')
        win.transient(self)
        win.grab_set()
        video = tk.StringVar(value=s['video'])
        source = tk.StringVar(value=str(s['source']))
        enabled = tk.BooleanVar(value=s['enabled'])
        ttk.Label(win, text='使用動画').pack()
        ttk.Combobox(win, textvariable=video, values=self.project['videos'], width=90, state='readonly').pack(padx=15)
        ttk.Label(win, text='この区間に対応する動画内の開始秒').pack()
        ttk.Entry(win, textvariable=source).pack()
        ttk.Checkbutton(win, text='映像を使用する（OFFなら静止画）', variable=enabled).pack()
        ttk.Label(win, text='候補\n'+'\n'.join(f"{Path(c['video']).name}: {c['source']:.3f}秒 / {c['score']:.3f}" for c in s['candidates']), wraplength=650).pack(pady=8)
        def apply():
            try:
                value = float(source.get())
                if not core.math.isfinite(value) or value < 0:
                    raise ValueError()
                if enabled.get() and not video.get():
                    raise ValueError()
            except ValueError:
                return messagebox.showerror('入力エラー', '動画と0以上の有限の開始秒を指定してください。', parent=win)
            s.update(video=video.get(), source=value, enabled=enabled.get(), status='manual', locked=True)
            win.destroy()
            self.refresh()
        ttk.Button(win, text='適用・承認', command=apply).pack(pady=12)

    def nudge(self, delta):
        index = self.selected()
        if not index or len(index) != 2:
            return
        s = self.project['episodes'][index[0]]['segments'][index[1]]
        s['source'] = max(0, s['source'] + delta)
        s.update(status='manual', locked=True)
        self.refresh()
        self.tree.selection_set(f'{index[0]}:{index[1]}')

    def preview(self):
        index = self.selected()
        if not index:
            return
        e = copy.deepcopy(self.project['episodes'][index[0]])
        still = self.project['still']
        path = Path(self.preview_dir.name) / f'preview-{core.os.urandom(4).hex()}.mp4'
        def task(log):
            core.export_episode(e, path, still, log)
            return path
        self.job(task)

    def export(self):
        if not self.project['episodes']:
            return
        if any(s['status'] == 'review' for e in self.project['episodes'] for s in e['segments']):
            if not messagebox.askyesno('要確認区間あり', '未承認区間は静止画になります。この状態で出力しますか？'):
                return
        folder = filedialog.askdirectory()
        if not folder:
            return
        p = copy.deepcopy(self.project)
        def task(log):
            for i, e in enumerate(p['episodes']):
                core.export_episode(e, Path(folder) / f"{i+1:02d}_{Path(e['audio']).stem}.mp4", p['still'], log)
        self.job(task)


if __name__ == '__main__':
    App().mainloop()
