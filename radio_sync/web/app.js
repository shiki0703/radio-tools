'use strict';

const FPS = 30;
const $ = (id) => document.getElementById(id);

/* ---------- small helpers ---------- */

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') el.style.cssText = v;
    else if (k === 'value') value = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'disabled' || k === 'open' || k === 'checked' || k === 'selected') el[k] = true;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === '') continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  if (value !== undefined) el.value = value;
  return el;
}

const basename = (p) => String(p || '').split(/[\\/]/).pop();

function fmt(t, ms = true) {
  if (!Number.isFinite(t)) return '--:--';
  const sign = t < 0 ? '-' : '';
  let x = Math.round(Math.abs(t) * 1000);
  const hh = Math.floor(x / 3600000); x -= hh * 3600000;
  const mm = Math.floor(x / 60000); x -= mm * 60000;
  const ss = Math.floor(x / 1000); x -= ss * 1000;
  const sec = String(ss).padStart(2, '0') + (ms ? '.' + String(x).padStart(3, '0') : '');
  return sign + (hh ? `${hh}:${String(mm).padStart(2, '0')}:${sec}` : `${mm}:${sec}`);
}

function fmtDuration(t) {
  if (!Number.isFinite(t) || t <= 0) return '長さ不明';
  const s = Math.round(t);
  const hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60;
  if (hh) return `${hh}時間${mm}分`;
  if (mm) return `${mm}分${ss ? ss + '秒' : ''}`;
  return `${ss}秒`;
}

function fmtMinutes(seconds) {
  if (seconds < 60) return '1分未満';
  if (seconds < 3600) return `約${Math.round(seconds / 60)}分`;
  return `約${Math.floor(seconds / 3600)}時間${Math.round(seconds % 3600 / 60)}分`;
}

const fmtGB = (b) => (b / 2 ** 30).toFixed(1) + ' GB';

function parseTime(text) {
  const s = String(text).trim()
    .replace(/[０-９．：]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
  const parts = s.split(':');
  if (!s || parts.length > 3 || parts.some((p) => p === '' || isNaN(Number(p)))) return NaN;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
}

/* ---------- server ---------- */

let TOKEN = (() => {
  const m = location.hash.match(/t=([\w-]+)/);
  try {
    if (m) {
      sessionStorage.setItem('radio-sync-token', m[1]);
      history.replaceState(null, '', location.pathname);
      return m[1];
    }
    return sessionStorage.getItem('radio-sync-token');
  } catch {
    return m ? m[1] : null;
  }
})();

/** この画面を見分けるための目印(PC とスマホの同期に使う) */
/** 画面が狭いか(スマホ) */
const isNarrow = () => window.innerWidth <= 820;

/** 上の帯と下のタブの高さを測って、編集画面が 1 画面に収まるようにする */
function measureChrome() {
  if (!isNarrow()) return;
  const top = document.querySelector('.top')?.offsetHeight || 0;
  const main = document.querySelector('main');
  const box = main ? getComputedStyle(main) : null;      // 下のタブの分は main の余白に入っている
  const pad = box ? parseFloat(box.paddingTop) + parseFloat(box.paddingBottom) : 96;
  document.documentElement.style.setProperty('--chrome', `${Math.round(top + pad + 4)}px`);
}

const CLIENT_ID = (() => {
  try {
    let id = sessionStorage.getItem('radio-sync-client');
    if (!id) {
      id = Math.random().toString(36).slice(2) + Date.now().toString(36);
      sessionStorage.setItem('radio-sync-client', id);
    }
    return id;
  } catch {
    return Math.random().toString(36).slice(2);
  }
})();

let failures = 0;

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Token': TOKEN, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    if (++failures >= 2) showBanner('ツールが終了しています。start_windows.bat から起動し直してください(作業内容は自動でバックアップされています)。');
    throw new Error('ツールと接続できませんでした。');
  }
  failures = 0;
  showBanner('');
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `エラーが発生しました (${res.status})`);
  return data;
}

const mediaUrl = (path) => `/media?t=${encodeURIComponent(TOKEN)}&p=${encodeURIComponent(path)}`;

function showBanner(text, action) {
  const el = $('banner');
  if (!el) return;
  el.replaceChildren();
  if (text) {
    el.append(h('span', {}, text));
    if (action) el.append(action);
  }
  el.hidden = !text;
}

/* ---------- state ---------- */

const emptyProject = () => ({ version: 1, audios: [], videos: [], still: '', stills: [], episodes: [] });

const S = {
  project: emptyProject(),
  path: null,
  dirty: false,
  backupAt: null,
  step: 'files',
  media: {},
  ep: 0,
  seg: 0,
  undo: [],
  redo: [],
  coalesce: null,
  jobs: {},
  outdir: '',
  plan: null,
  exported: null,
  cutEp: 0,          // カット画面で見ている話
  cutAt: 0,          // カット画面で選んでいる区切りの位置(秒)
  trimAt: -1,        // カット画面で選んでいる削る範囲(その始まりの秒)
  searching: false,
  tab: 'cuts',
  cutFilter: 'all',
  detailSpan: 120,
  sample: null,
  narrow: false,     // 画面が狭いか(スマホ)
  light: (() => {    // プレビューを軽くするか(既定は軽く。つまみ送りが速い)
    try { return localStorage.getItem('radio-sync-light') !== 'off'; } catch { return true; }
  })(),
  upload: null,      // ファイルを送っている最中の様子
  syncedAt: 0,       // 最後に取り込んだ(または預けた)時刻
  unsaved: false,    // まだサーバに預けていない変更があるか
  pending: 0,        // 相手の画面で保存された時刻(取り込み待ち)
};

const curEp = () => S.project.episodes[S.ep];
const curSeg = () => curEp()?.segments[S.seg];

function segKind(s) {
  if (s.status === 'review') return 'review';
  if (!s.enabled || !s.video) return 'still';
  return s.status === 'manual' ? 'manual' : 'auto';
}

const REVIEW_REASON = {
  weak: 'この区間はカメラの音とほとんど一致しません(オープニングのジングルなど、ラジオにしか入っていない音の可能性)。前後のカメラ映像を続けて当てていますが、再生して合っていなければ「静止画にする」か位置を直してください。',
  switch: 'カメラが切り替わる位置がはっきりしませんでした。再生して、切り替わりの前後が合っているか確かめてください。',
  nocamera: 'この話に合うカメラ映像が見つかりませんでした。カメラを使う場合は「カメラ映像を使う」を押して位置を合わせてください。',
};

const KIND_LABEL = { auto: '自動で一致', manual: '確認済み', review: '要確認', still: '静止画' };

function reviewCount(ep) {
  const eps = ep ? [ep] : S.project.episodes;
  return eps.reduce((n, e) => n + e.segments.filter((s) => s.status === 'review').length, 0);
}

function segAt(ep, t) {
  const segs = ep.segments;
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (segs[mid].start <= t + 1e-6) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function clampSelection() {
  const eps = S.project.episodes;
  S.ep = Math.max(0, Math.min(S.ep, eps.length - 1));
  const ep = curEp();
  S.seg = ep ? Math.max(0, Math.min(S.seg, ep.segments.length - 1)) : 0;
}

/* ---------- edits, undo, autosave ---------- */

const snapshot = () => JSON.stringify({ project: S.project, ep: S.ep, seg: S.seg });

function commit(change, coalesceKey) {
  const now = Date.now();
  if (!(coalesceKey && S.coalesce && S.coalesce.key === coalesceKey && now - S.coalesce.at < 1200)) {
    S.undo.push(snapshot());
    if (S.undo.length > 100) S.undo.shift();
  }
  S.coalesce = coalesceKey ? { key: coalesceKey, at: now } : null;
  S.redo = [];
  change();
  markDirty();
  renderAll();
}

/** もう変えたあとのものを「元に戻す」に積む(ドラッグ操作用) */
function pushUndo(before) {
  if (!before || before === snapshot()) return;
  S.undo.push(before);
  if (S.undo.length > 100) S.undo.shift();
  S.redo = [];
  S.coalesce = null;
  markDirty();
  renderHeader();
}

function restore(json) {
  const o = JSON.parse(json);
  S.project = o.project;
  S.ep = o.ep;
  S.seg = o.seg;
  clampSelection();
}

function undo() {
  if (!S.undo.length) return;
  S.redo.push(snapshot());
  restore(S.undo.pop());
  S.coalesce = null;
  markDirty();
  renderAll();
  syncPlayer(false);
  toast('元に戻しました');
}

function redo() {
  if (!S.redo.length) return;
  S.undo.push(snapshot());
  restore(S.redo.pop());
  markDirty();
  renderAll();
  syncPlayer(false);
}

let autosaveTimer = 0;
function markDirty() {
  S.dirty = true;
  S.unsaved = true;            // まだサーバに預けていない変更がある
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(async () => {
    try {
      const r = await api('/api/autosave', { project: S.project, path: S.path, client: CLIENT_ID });
      S.backupAt = new Date();
      S.syncedAt = r.saved_at || 0;
      S.unsaved = false;
      renderHeader();
    } catch { /* banner already shown */ }
  }, 1200);
  renderHeader();
}

// A bulk change (a whole cut) keeps flagged segments flagged: moving a cut is not checking each part of it.
function lock(s) {
  s.locked = true;
  if (s.status !== 'review') s.status = 'manual';
}

function touch(s) {
  s.status = 'manual';
  s.locked = true;
}

/* ---------- toasts & dialogs ---------- */

function toast(text, kind = '') {
  const el = h('div', { class: 'toast ' + kind }, text);
  if (!$('toasts')) return;
  $('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 3800);
}

function ask(title, body, buttons) {
  return new Promise((resolve) => {
    const modal = $('modal');
    const close = (value) => {
      modal.hidden = true;
      modal.replaceChildren();
      document.removeEventListener('keydown', onKey, true);
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(buttons[buttons.length - 1].value); }
    };
    modal.replaceChildren(h('div', { class: 'mbox', role: 'dialog', 'aria-modal': 'true' },
      h('h3', {}, title),
      typeof body === 'string' ? h('p', {}, body) : body,
      h('div', { class: 'mbtns' }, buttons.map((b) =>
        h('button', { class: b.primary ? 'primary' : b.danger ? 'danger' : 'outline', onclick: () => close(b.value) }, b.label)))));
    modal.hidden = false;
    document.addEventListener('keydown', onKey, true);
    (modal.querySelector('.primary') || modal.querySelector('button')).focus();
  });
}

const showError = (message) => ask('うまくいきませんでした', message, [{ label: '閉じる', value: null, primary: true }]);

function guard(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (e) {
      showError(e.message);
    }
  };
}

/* ---------- jobs ---------- */

const JOB_DONE = {
  analyze(job) {
    const still = S.project.still;
    commit(() => {
      S.project = { ...job.result, still };
      S.ep = 0;
      S.seg = 0;
    });
    const n = reviewCount();
    toast(n ? `照合が終わりました。要確認は ${n} か所です。` : '照合が終わりました。要確認の区間はありません。');
    setStep('review');
    if (n) gotoReview(1, true);
  },
  export(job) {
    S.exported = job.result;
    toast('書き出しが終わりました');
    refreshPlan();
  },
};

function watchJob(id, kind) {
  S.jobs[kind] = { id, kind, state: 'running', progress: 0, message: '準備中…' };
  renderAll();
  const tick = async () => {
    let job;
    try {
      job = await api('/api/jobs/' + id);
    } catch {
      setTimeout(tick, 3000);
      return;
    }
    S.jobs[kind] = job;
    if (job.state === 'running') {
      updateProgress(job);
      setTimeout(tick, 700);
      return;
    }
    renderAll();
    if (job.state === 'done') JOB_DONE[kind]?.(job);
    else if (job.state === 'cancelled') toast('中止しました');
    else if (job.state === 'error') showError(job.error);
  };
  tick();
}

function updateProgress(job) {
  for (const el of document.querySelectorAll(`[data-job="${job.kind}"]`)) {
    el.querySelector('.progress > div').style.width = (job.progress * 100).toFixed(1) + '%';
    el.querySelector('.pct').textContent = Math.floor(job.progress * 100) + '%';
    el.querySelector('.eta').textContent = job.eta != null ? `残り ${fmtMinutes(job.eta)}` : '残り時間を計算中…';
    el.querySelector('.msg').textContent = job.message || '';
  }
}

const running = (kind) => S.jobs[kind]?.state === 'running';

function progressCard(kind, title) {
  const job = S.jobs[kind];
  const el = h('div', { class: 'card', 'data-job': kind, style: 'display:grid;gap:10px' },
    h('div', { class: 'spread' }, h('b', {}, title), h('span', { class: 'pct mono' }, '0%')),
    h('div', { class: 'progress' }, h('div', { style: 'width:0%' })),
    h('div', { class: 'spread' },
      h('span', { class: 'msg muted small' }),
      h('span', { class: 'row' }, h('span', { class: 'eta muted small' }),
        h('button', { class: 'danger', onclick: guard(() => api('/api/cancel', { job: job.id })) }, '中止'))),
    h('p', { class: 'muted small' }, 'この画面を閉じても処理は続きます。もう一度開くと進み具合を確認できます。'));
  queueMicrotask(() => updateProgress(job));
  return el;
}

/* ---------- media info & proxies ---------- */

async function inspect(paths) {
  const need = [...new Set(paths.filter((p) => p && !S.media[p]))];
  if (!need.length) return;
  const { items } = await api('/api/inspect', { paths: need });
  for (const info of items) S.media[info.path] = info;
}

const proxyWatch = {};

// Edge on Windows can usually decode HEVC (iPhone video) itself, which avoids a long conversion.
const CAN_PLAY_HEVC = (() => {
  try {
    const v = document.createElement('video');
    return ['hvc1.2.4.L153.B0', 'hvc1.1.6.L153.B0', 'hev1.1.6.L153.B0']
      .some((codec) => v.canPlayType(`video/mp4; codecs="${codec}"`) !== '');
  } catch {
    return false;
  }
})();

function playableUrl(path) {
  const info = S.media[path];
  if (!info) {
    inspect([path]).then(() => { renderAll(); syncPlayer(isPlaying()); }).catch(() => {});
    return null;
  }
  // プレビューは軽量版を使う。4K60 の元動画をブラウザでそのまま流すと、1秒に2コマほどしか
  // 出せずに音とずれて見える(PC によっては再生できない)ので、できあがるまで待つ
  if ((FROM_PHONE || S.light) && info.kind === 'video') {
    if (info.proxy_ready && info.proxy_path) return mediaUrl(info.proxy_path);
    startProxy(path, true);
    return null;
  }
  if (info.playable) return mediaUrl(path);
  if (info.proxy_ready) return mediaUrl(info.proxy_path);
  if (info.hevc && CAN_PLAY_HEVC && !info.directFailed) return mediaUrl(path);
  if (info.kind === 'video' || info.kind === 'audio') startProxy(path);
  return null;
}

async function startProxy(path, force = false) {
  if (proxyWatch[path]) return;
  proxyWatch[path] = { progress: 0, state: 'running' };
  try {
    const r = await api('/api/proxy', { path, force });
    if (r.ready && r.proxy_path) {
      Object.assign(S.media[path], { proxy_ready: true, proxy_path: r.proxy_path });
      delete proxyWatch[path];
      syncPlayer(false);
      return;
    }
    if (r.ready) {                       // 軽量版はいらない(そのまま再生できる)
      delete proxyWatch[path];
      syncPlayer(false);
      return;
    }
    const tick = async () => {
      const job = await api('/api/jobs/' + r.job).catch(() => null);
      if (!job) return setTimeout(tick, 2000);
      proxyWatch[path] = job;
      if (job.state === 'running') {
        syncPlayer(isPlaying());
        return setTimeout(tick, 1000);
      }
      if (job.state === 'done') {
        Object.assign(S.media[path], { proxy_ready: true, proxy_path: r.proxy_path });
        delete proxyWatch[path];
      }
      syncPlayer(isPlaying());
    };
    tick();
  } catch (e) {
    proxyWatch[path] = { state: 'error', error: e.message };
    syncPlayer(false);
  }
}

function mediaWaitMessage(path) {
  const info = S.media[path];
  if (!info) return '読み込み中…';
  if (info.problem) return info.problem;
  const job = proxyWatch[path];
  if (job?.state === 'error') return 'プレビュー用の変換に失敗しました: ' + (job.error || '');
  const eta = job?.eta != null ? `・残り${fmtMinutes(job.eta)}` : '';
  const why = FROM_PHONE && info.playable
    ? 'スマホで見られるように、'
    : S.light && info.kind === 'video'
      ? 'なめらかに再生できるように、'
      : info.kind === 'audio' && info.codec === 'mp3'
        ? '音と映像の位置を正確に合わせるため、'
        : `${basename(path)} はそのまま再生できないため、`;
  const hint = !FROM_PHONE && S.light && info.kind === 'video'
    ? '\n(元の画質ですぐ見るときは「なめらか優先」を外してください。重くてカクつくことがあります)' : '';
  return `${why}プレビュー用の軽量版を作成中… ${Math.floor((job?.progress || 0) * 100)}%${eta}(初回のみ)${hint}`;
}

/* ---------- navigation ---------- */

function canAnalyze() {
  return S.project.audios.length > 0 && S.project.videos.length > 0;
}

function setStep(step) {
  if ((step === 'review' || step === 'cut' || step === 'export') && !S.project.episodes.length) return;
  if (step === 'analyze' && !canAnalyze()) return;
  if (step !== 'review') pause();
  if (step !== 'cut') leaveCut();
  S.step = step;
  renderAll();
  window.scrollTo(0, 0);
  if (step === 'review') {
    loadEpisodeAudio();
    // Start preparing previews of every camera now instead of when each one first plays.
    for (const v of S.project.videos) playableUrl(v);
  }
  if (step === 'cut') enterCut();
  if (step === 'export' && S.outdir) refreshPlan();
}

function renderAll() {
  clampSelection();
  renderHeader();
  renderSteps();
  for (const v of ['files', 'analyze', 'review', 'cut', 'export']) $('view-' + v).hidden = S.step !== v;
  if (S.step === 'files') renderFiles();
  if (S.step === 'analyze') renderAnalyze();
  if (S.step === 'review') renderReview();
  if (S.step === 'cut') renderCut();
  if (S.step === 'export') renderExport();
}

function renderHeader() {
  $('docName').textContent = S.path ? basename(S.path) : '新しいプロジェクト';
  let state = '';
  const at = S.backupAt ? S.backupAt.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' }) : '';
  if (S.dirty) {
    state = isNarrow()
      ? (at ? `${at} 控え済み` : '未保存')
      : (at ? `未保存の変更あり(${at} 自動バックアップ済み)` : '未保存の変更あり');
  } else if (S.path) state = '保存済み';
  $('saveState').textContent = state;
  $('btnUndo').disabled = !S.undo.length;
  $('btnRedo').disabled = !S.redo.length;
}

function renderSteps() {
  const has = S.project.episodes.length > 0;
  const enabled = { files: true, analyze: canAnalyze(), review: has, cut: has, export: has };
  const complete = { files: canAnalyze(), analyze: has, review: has && !reviewCount(),
                     cut: has && S.project.episodes.some((e) => (e.cuts || []).length), export: !!S.exported };
  for (const b of $('steps').querySelectorAll('button')) {
    const step = b.dataset.step;
    b.disabled = !enabled[step];
    b.classList.toggle('current', S.step === step);
    b.classList.toggle('complete', S.step !== step && complete[step]);
    b.querySelector('.count')?.remove();
    if (step === 'review' && has && reviewCount()) b.append(h('span', { class: 'count' }, reviewCount()));
    const label = b.querySelector('.lbl');
    if (label) label.textContent = isNarrow() ? b.dataset.short : b.dataset.full;
    if (S.step === step && !isNarrow()) b.scrollIntoView({ block: 'nearest', inline: 'center' });
  }
}

/* ---------- step 1: files ---------- */

/** この端末(スマホなど)の中にあるファイルを送って取り込む */
function uploadCard() {
  const up = S.upload;
  const input = h('input', {
    type: 'file', multiple: true, accept: 'video/*,audio/*,.mov,.mp4,.wav,.mp3,.m4a',
    style: 'display:none', id: 'uploadInput',
    // 先に控えを取る(input を空にすると、選ばれたファイルの一覧も消えるため)
    onchange: (e) => { const files = [...e.target.files]; e.target.value = ''; guard(() => uploadFiles(files))(); },
  });
  if (up) {
    return h('div', { class: 'card soft', style: 'display:grid;gap:8px' },
      h('b', {}, '送っています…'),
      h('div', { class: 'progress' }, h('div', { id: 'uploadBar', style: `width:${Math.round(up.ratio * 100)}%` })),
      h('div', { class: 'muted small', id: 'uploadLabel' },
        `${up.index}/${up.count} ${up.name} … ${Math.round(up.ratio * 100)}%`),
      h('p', { class: 'muted small' }, '送り終わるまで、この画面を閉じないでください。'));
  }
  return h('div', { class: 'card soft spread' },
    h('div', {},
      h('b', {}, 'この端末から送って追加'),
      h('p', { class: 'muted small' },
        'スマホの中の動画・音声を、この PC に送って素材にします。大きい動画は Wi-Fi でも数分かかります。')),
    h('div', {}, input,
      h('button', { onclick: () => $('uploadInput').click() }, 'ファイルを送る')));
}

function renderFiles() {
  const p = S.project;
  const problems = [...p.audios, ...p.videos].filter((x) => S.media[x]?.problem).length;
  $('view-files').replaceChildren(h('div', { class: 'page' },
    h('div', {},
      h('h1', {}, '素材を選ぶ'),
      h('p', { class: 'muted' }, 'その日の収録素材を追加してください。元のファイルは変更・移動しません。')),
    h('div', { class: 'card soft spread' },
      h('div', {},
        h('b', {}, 'フォルダからまとめて追加'),
        h('p', { class: 'muted small' }, 'フォルダ(とその1つ下のフォルダ)にある、映像のないファイルを「ラジオ音声」、映像のあるファイルを「カメラ動画」に自動で振り分けます。')),
      h('button', { onclick: guard(addFolder) }, 'フォルダを選ぶ')),
    FROM_PHONE && uploadCard(),
    h('div', { class: 'grid2' },
      fileCard('audios', 'ラジオ音声', '1ファイルが1話になります。上から順に「第1話、第2話…」になります。', '＋ 音声を追加'),
      fileCard('videos', 'カメラ動画', 'カメラに録音された会話の音で位置を合わせます。台数・本数の制限はありません。', '＋ 動画を追加')),
    stillCard(),
    h('div', { class: 'footer-nav' },
      h('span', { class: 'muted' },
        !p.audios.length ? 'ラジオ音声を追加してください' :
        !p.videos.length ? 'カメラ動画を追加してください' :
        problems ? `確認が必要なファイルが ${problems} 本あります` : '準備ができました'),
      h('button', { class: 'primary big', disabled: !canAnalyze(), onclick: () => setStep('analyze') }, '次へ:自動照合 →'))));
}

function fileCard(key, title, description, addLabel) {
  const list = S.project[key];
  const total = list.reduce((t, x) => t + (S.media[x]?.duration || 0), 0);
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', {},
        h('h3', {}, title, ' ', h('span', { class: 'muted small' }, list.length ? `${list.length}本・合計 ${fmtDuration(total)}` : '')),
        h('p', { class: 'muted small' }, description)),
      h('button', { onclick: guard(() => addFiles(key)) }, addLabel)),
    list.length
      ? h('ul', { class: 'files' }, list.map((path, i) => fileRow(key, path, i)))
      : h('div', { class: 'empty' }, 'まだありません'));
}

function fileRow(key, path, i) {
  const info = S.media[path];
  const list = S.project[key];
  return h('li', { class: 'file' + (info?.problem ? ' bad' : '') },
    key === 'audios' ? h('span', { class: 'num' }, `第${i + 1}話`) : h('span', { class: 'num cam' }, `動画${i + 1}`),
    h('div', { class: 'fmeta' },
      h('div', { class: 'fname', title: path }, basename(path)),
      h('div', { class: 'fsub muted' },
        !info ? '確認中…' : [
          info.duration ? h('span', {}, fmtDuration(info.duration)) : null,
          info.problem ? h('span', { class: 'bad-text' }, '' + info.problem) :
            info.hevc && CAN_PLAY_HEVC ? h('span', {}, 'iPhone形式(HEVC)・そのまま再生できます') :
            !info.playable && !info.proxy_ready ? h('span', {}, '確認画面で再生するとき、軽量版を自動で作ります') : null,
        ])),
    key === 'audios' && list.length > 1 && [
      h('button', { class: 'ghost icon', title: '上へ', disabled: i === 0, onclick: () => moveFile(i, -1) }, '↑'),
      h('button', { class: 'ghost icon', title: '下へ', disabled: i === list.length - 1, onclick: () => moveFile(i, 1) }, '↓'),
    ],
    h('button', { class: 'ghost icon', title: 'リストから外す(ファイル自体は消えません)', onclick: () => removeFile(key, i) }, '×'));
}

function stillCard() {
  const still = S.project.still;
  return h('div', { class: 'card still-preview' },
    h('div', { class: 'thumb', style: still ? `background-image:url("${mediaUrl(still)}")` : '' }),
    h('div', { style: 'flex:1' },
      h('b', {}, '映像がない所に表示する画像(既定)'),
      h('p', { class: 'muted small' }, (still ? basename(still) : '未設定のときは濃い青灰色の背景になります。番組ロゴなどを指定できます。') +
        ' 確認画面で、静止画のカットごとに別の画像も選べます。')),
    h('div', { class: 'row' },
      still && h('button', { class: 'ghost', onclick: () => commit(() => { S.project.still = ''; }) }, '外す'),
      h('button', { onclick: guard(pickStill) }, still ? '変更' : '画像を選ぶ')));
}

async function addFiles(key) {
  const { paths } = await api('/api/pick', { kind: key === 'audios' ? 'audio' : 'video' });
  if (!paths.length) return;
  await inspect(paths);
  const moved = [];
  commit(() => {
    for (const path of paths) {
      const kind = S.media[path]?.kind;
      let target = key;
      if (key === 'audios' && kind === 'video') target = 'videos';
      if (key === 'videos' && kind === 'audio') target = 'audios';
      if (target !== key) moved.push(basename(path));
      if (!S.project[target].includes(path)) S.project[target].push(path);
    }
  });
  if (moved.length) toast(`${moved.join('、')} は種類が違うため、もう一方のリストに入れました`, 'warn');
}

async function addFolder() {
  const { paths } = await api('/api/pick', { kind: 'folder' });
  if (!paths.length) return;
  const r = await api('/api/scan', { folder: paths[0] });
  for (const info of [...r.audios, ...r.videos]) S.media[info.path] = info;
  if (!r.audios.length && !r.videos.length) {
    toast('音声・動画ファイルが見つかりませんでした', 'warn');
    return;
  }
  commit(() => {
    for (const info of r.audios) if (!S.project.audios.includes(info.path)) S.project.audios.push(info.path);
    for (const info of r.videos) if (!S.project.videos.includes(info.path)) S.project.videos.push(info.path);
  });
  toast(`ラジオ音声 ${r.audios.length} 本・カメラ動画 ${r.videos.length} 本を追加しました` +
        (r.skipped.length ? `(読めないファイル ${r.skipped.length} 本は除外)` : ''));
}

async function pickStill() {
  const { paths } = await api('/api/pick', { kind: 'still' });
  if (!paths.length) return;
  await inspect(paths);
  commit(() => {
    S.project.still = paths[0];
    if (!S.project.stills.includes(paths[0])) S.project.stills.push(paths[0]);
  });
}

function moveFile(i, dir) {
  commit(() => {
    const list = S.project.audios;
    [list[i], list[i + dir]] = [list[i + dir], list[i]];
    const order = new Map(list.map((a, k) => [a, k]));
    S.project.episodes.sort((a, b) => (order.get(a.audio) ?? 1e9) - (order.get(b.audio) ?? 1e9));
  });
}

async function removeFile(key, i) {
  const path = S.project[key][i];
  const usedBy = key === 'audios'
    ? S.project.episodes.some((e) => e.audio === path)
    : S.project.episodes.some((e) => e.segments.some((s) => s.video === path));
  if (usedBy) {
    const answer = await ask('リストから外しますか?', `「${basename(path)}」は照合結果で使われています。外した後に照合し直すと、結果から消えます。`,
      [{ label: '外す', value: true, danger: true }, { label: 'やめる', value: false }]);
    if (!answer) return;
  }
  commit(() => {
    S.project[key].splice(i, 1);
    if (key === 'audios') S.project.episodes = S.project.episodes.filter((e) => e.audio !== path);
  });
}

/* ---------- step 2: analyze ---------- */

function renderAnalyze() {
  const p = S.project;
  const audioSec = p.audios.reduce((t, x) => t + (S.media[x]?.duration || 0), 0);
  const videoSec = p.videos.reduce((t, x) => t + (S.media[x]?.duration || 0), 0);
  // Measured on an i7-14700F: ~0.55 s per 8 s block per hour of camera audio.
  const estimate = (audioSec / 8) * (videoSec / 3600) * 0.55 + (videoSec + audioSec) / 3600 * 20;
  const locked = p.episodes.filter((e) => p.audios.includes(e.audio))
    .reduce((n, e) => n + e.segments.filter((s) => s.locked).length, 0);
  const problems = [...p.audios, ...p.videos].map((x) => S.media[x]).filter((i) => i?.problem);
  const job = S.jobs.analyze;

  $('view-analyze').replaceChildren(h('div', { class: 'page' },
    h('div', {},
      h('h1', {}, '自動照合'),
      h('p', { class: 'muted' }, 'ラジオ音声を8秒ずつに区切り、カメラの音と照らし合わせて、使う映像と位置を自動で決めます。')),
    h('div', { class: 'stats' },
      stat('話(ラジオ音声)', `${p.audios.length} 本`, `合計 ${fmtDuration(audioSec)}`),
      stat('カメラ動画', `${p.videos.length} 本`, `合計 ${fmtDuration(videoSec)}`),
      stat('かかる時間の目安', fmtMinutes(estimate), '素材が長いほど時間がかかります')),
    problems.length > 0 && h('div', { class: 'notice bad' },
      '次のファイルを確認してください(「素材を選ぶ」で外せます)\n' + problems.map((i) => `・${i.name}: ${i.problem}`).join('\n')),
    p.episodes.length > 0 && (locked
      ? h('div', { class: 'notice' }, `手動で調整した ${locked} 区間は、照合し直してもそのまま残します。それ以外の区間は新しい結果に置き換わります。`)
      : h('div', { class: 'notice warn' }, '照合し直すと、前回の自動照合の結果は新しい結果に置き換わります。')),
    running('analyze')
      ? progressCard('analyze', '照合しています…')
      : h('div', { class: 'card spread' },
          h('div', {},
            h('b', {}, job?.state === 'cancelled' ? '中止しました。もう一度始められます。' : '準備ができたら開始してください'),
            h('p', { class: 'muted small' }, '処理はこのPCの中だけで行い、素材を外部に送ることはありません。')),
          h('div', { class: 'row' },
            p.episodes.length > 0 && h('button', { onclick: () => setStep('review') }, '前回の結果を見る'),
            h('button', { class: 'primary big', disabled: !canAnalyze() || problems.length > 0, onclick: guard(startAnalyze) },
              p.episodes.length ? '照合し直す' : '▶ 自動照合を開始')))));
}

const stat = (label, value, sub) =>
  h('div', { class: 'stat' }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), h('div', { class: 'sub' }, sub));

async function startAnalyze() {
  const p = S.project;
  await inspect([...p.audios, ...p.videos]);
  const r = await api('/api/analyze', { audios: p.audios, videos: p.videos, previous: p.episodes });
  watchJob(r.job, 'analyze');
  for (const a of p.audios) if (S.media[a]?.kind === 'audio' && !S.media[a].playable && !S.media[a].proxy_ready) startProxy(a);
  if (S.light || FROM_PHONE) {
    for (const v of p.videos) if (S.media[v]?.kind === 'video' && !S.media[v].proxy_ready) startProxy(v, true);
  }
}

/* ---------- step 3: review ---------- */

function renderReview() {
  const ep = curEp();
  if (!ep) return;
  $('view-review').classList.toggle('one-episode', S.project.episodes.length <= 1);
  renderPhoneReviewBar();
  renderEpisodeList();
  renderReviewBar();
  renderTimelines();
  renderSide();
  renderSampleBar();
  renderWaveControls();
  syncPlayer(isPlaying());
  drawWave();
}

/** スマホの確認画面:プレビューのすぐ下に置く、いちばん使う操作 */
function renderPhoneReviewBar() {
  const host = $('phoneReview');
  if (!host) return;
  host.hidden = !isNarrow();
  if (!isNarrow()) return;
  const ep = curEp();
  const cuts = cutsOf(ep);
  const n = cutIndexAt(cuts, S.seg);
  const cut = cuts[n];
  const reviews = reviewCount(ep);
  const segs = cut ? ep.segments.slice(cut.from, cut.to + 1) : [];
  const still = cut && !cut.cam;
  host.replaceChildren(
    h('div', { class: 'prow' },
      h('button', { class: 'icon', disabled: n <= 0, onclick: () => pickTime(cuts[n - 1].start) }, '◀'),
      h('div', { class: 'pcut' },
        h('b', {}, cut ? `カット${n + 1}/${cuts.length}` : 'カットなし'),
        cut && h('span', { class: 'kind ' + (cut.cam ? 'cam' : 'still') }, cut.cam ? 'カメラ' : '静止画'),
        cut && cut.review && h('span', { class: 'flag' }, '要確認')),
      h('button', { class: 'icon', disabled: n >= cuts.length - 1, onclick: () => pickTime(cuts[n + 1].start) }, '▶'),
      h('span', { style: 'flex:1' }),
      reviews > 0 && h('button', { class: 'ghost small', onclick: () => gotoReview(1) }, `要確認 ${reviews}`)),
    h('div', { class: 'prow' },
      cut && h('button', { class: 'primary', disabled: !cut.review, onclick: () => approveCut(cut) }, 'OK'),
      cut && h('button', { onclick: () => setCutCamera(cut, still) }, still ? 'カメラに戻す' : '静止画にする'),
      cut && !cut.cam && h('button', { onclick: () => chooseStill({ label: `カット${n + 1}`, segments: segs }) }, '画像'),
      cut && h('button', { class: 'ghost', onclick: () => openSegment(cut.from) }, '詳しく'),
      h('span', { style: 'flex:1' }),
      h('button', { class: 'ghost', onclick: () => borrowInSheet('カット一覧', $('cutPanel')) }, '一覧')));
}

function renderSide() {
  for (const b of $('sideTabs').querySelectorAll('button')) b.classList.toggle('on', b.dataset.tab === S.tab);
  $('cutPanel').hidden = S.tab !== 'cuts';
  $('inspector').hidden = S.tab !== 'segment';
  if (S.tab === 'cuts') renderCuts();
  else renderInspector();
}

function setTab(tab) {
  S.tab = tab;
  renderSide();
  renderWaveControls();
  drawWave();
}

// The selected segment changed without other edits.
function refreshSelection() {
  renderReviewBar();
  renderTimelines();
  if (S.tab === 'cuts') markCurrentCut();
  else renderInspector();
  renderWaveControls();
}

/* ----- cuts: stretches where one camera continues (or the still image) ----- */

// Whether segment s simply continues p (same camera and continuous position, or both still).
function continues(p, s) {
  const cam = s.enabled && !!s.video;
  if ((p.enabled && !!p.video) !== cam) return false;
  if (!cam) return (p.still || '') === (s.still || '');
  return p.video === s.video && Math.abs(p.source + (p.end - p.start) - s.source) < 0.05;
}

function cutsOf(ep) {
  const cuts = [];
  ep.segments.forEach((s, j) => {
    const cam = s.enabled && !!s.video;
    const last = cuts[cuts.length - 1];
    const prev = ep.segments[j - 1];
    const joined = last && continues(prev, s);
    if (joined) {
      last.end = s.end;
      last.to = j;
    } else {
      cuts.push({ cam, video: cam ? s.video : '', start: s.start, end: s.end, from: j, to: j });
    }
  });
  for (const c of cuts) {
    const segs = ep.segments.slice(c.from, c.to + 1);
    c.review = segs.filter((s) => s.status === 'review').length;
    c.manual = segs.filter((s) => s.status === 'manual').length;
    if (c.cam) {
      c.source0 = segs[0].source;
      c.offset = segs[0].source - segs[0].start;
    }
  }
  return cuts;
}

const cutIndexAt = (cuts, j) => cuts.findIndex((c) => j >= c.from && j <= c.to);

const CUT_FILTERS = [['all', 'すべて'], ['review', '要確認あり'], ['still', '静止画'], ['camera', 'カメラ']];

function matchCut(c, filter) {
  return filter === 'all' || (filter === 'review' && c.review > 0) || (filter === 'still' && !c.cam) || (filter === 'camera' && c.cam);
}

const stillOf = (s) => s.still || S.project.still || '';

function stillName(path) {
  if (path) return basename(path);
  return S.project.still ? `既定の画像(${basename(S.project.still)})` : '既定(濃い青灰色の背景)';
}

function guideOpen() {
  try { return localStorage.getItem('radio-sync-cut-guide') === 'open'; } catch { return false; }
}

function renderCuts() {
  const ep = curEp();
  const cuts = cutsOf(ep);
  const reviews = reviewCount(ep);
  const filter = S.cutFilter;
  const keep = $('cutList')?.scrollTop || 0;
  const shown = cuts.map((c, n) => [c, n]).filter(([c]) => matchCut(c, filter));
  const list = h('ul', { class: 'cut-list', id: 'cutList' },
    shown.length
      ? shown.map(([c, n]) => cutCard(ep, cuts, c, n))
      : h('li', { class: 'empty' }, `この話に「${CUT_FILTERS.find(([k]) => k === filter)[1]}」のカットはありません。◀ ▶ で他の話を探せます。`));
  const guide = h('details', { class: 'guide', open: guideOpen() },
    h('summary', {}, 'カット一覧とは?(使い方)'),
    h('ul', {},
      h('li', {}, h('b', {}, 'カット'), '=同じ映像が続くひとまとまりです(同じカメラの続き、または同じ静止画)。自動照合の結果を8秒ごとではなく、まとまり単位で並べています。'),
      h('li', {}, '各カットでできること:', h('b', {}, '▶再生・抜き取り'), '(数か所だけ再生)、', h('b', {}, 'ずれ調整'), '(±コマ、下の波形のドラッグ)、',
        h('b', {}, '静止画にする/画像を選ぶ'), '、要確認の', h('b', {}, 'OK'), '。'),
      h('li', {}, 'カメラと静止画の切り替わり位置は、上の拡大タイムラインの黒いつまみをドラッグして動かせます。8秒単位で細かく直すときは「詳しく」。')));
  guide.addEventListener('toggle', () => {
    try { localStorage.setItem('radio-sync-cut-guide', guide.open ? 'open' : 'closed'); } catch { /* not important */ }
  });
  $('cutPanel').replaceChildren(
    h('div', { class: 'cut-summary' },
      h('div', { class: 'spread' },
        h('b', {}, `第${S.ep + 1}話のカット ${cuts.length} 本`),
        reviews ? h('span', { class: 'chip review' }, `要確認 ${reviews}`) : h('span', { class: 'chip done' }, '要確認なし')),
      guide,
      h('div', { class: 'filters' },
        h('span', { class: 'muted small' }, '絞り込み'),
        CUT_FILTERS.map(([k, label]) => h('button', {
          class: 'filter' + (k === filter ? ' on' : ''),
          onclick: () => { S.cutFilter = k; renderCuts(); },
        }, label, h('span', { class: 'n' }, cuts.filter((c) => matchCut(c, k)).length))),
        h('span', { class: 'grow' }),
        h('button', { class: 'icon', title: '前の該当カットへ(他の話も含む)', onclick: () => jumpCut(-1) }, '◀'),
        h('button', { class: 'icon', title: '次の該当カットへ(他の話も含む)', onclick: () => jumpCut(1) }, '▶')),
      h('div', { class: 'row' },
        h('button', { class: 'primary', onclick: () => startSampling() }, '▶ この話を抜き取り確認'),
        reviews > 0 && h('button', { onclick: guard(approveEpisodeReviews) }, `要確認 ${reviews} か所をまとめてOK`))),
    list);
  list.scrollTop = keep;
  markCurrentCut(S.markedSeg !== S.seg);
}

// Move to the previous/next cut that matches the filter, continuing into other episodes.
function jumpCut(dir) {
  const found = [];
  S.project.episodes.forEach((e, i) => cutsOf(e).forEach((c) => { if (matchCut(c, S.cutFilter)) found.push([i, c]); }));
  if (!found.length) return toast('該当するカットはありません', 'warn');
  const order = (i, j) => i * 1e6 + j;
  const here = currentCut();
  const pick = dir > 0
    ? found.find(([i, c]) => order(i, c.from) > order(S.ep, here ? here.to : S.seg)) || found[0]
    : [...found].reverse().find(([i, c]) => order(i, c.from) < order(S.ep, here ? here.from : S.seg)) || found[found.length - 1];
  selectSegment(pick[0], pick[1].from);
}

function cutCard(ep, cuts, c, n) {
  const prev = cuts[n - 1];
  const next = cuts[n + 1];
  const segs = ep.segments.slice(c.from, c.to + 1);
  const image = c.cam ? '' : stillOf(segs[0]);
  return h('li', {
    class: 'cut' + (c.cam ? '' : ' still') + (c.review ? ' has-review' : ''),
    'data-from': c.from, 'data-to': c.to,
    onclick: (e) => { if (!e.target.closest('button')) pickTime(c.start); },
  },
  h('div', { class: 'cut-head' },
    h('div', { class: 'cut-title' }, `カット${n + 1}`,
      h('span', { class: 'kind ' + (c.cam ? 'cam' : 'still') }, c.cam ? 'カメラ' : '静止画'),
      h('span', { class: 'cutwhere', title: `話全体(${fmtDuration(ep.duration)})の中での位置` },
        h('i', { style: `left:${(c.start / ep.duration) * 100}%;width:${Math.max(1.5, ((c.end - c.start) / ep.duration) * 100)}%` }))),
    c.review ? h('span', { class: 'chip review' }, `要確認 ${c.review}`) : c.manual ? h('span', { class: 'chip manual' }, '調整済み') : null),
  h('div', { class: 'cut-body' },
    !c.cam && h('div', { class: 'thumb', style: image ? `background-image:url("${mediaUrl(image)}")` : '' }),
    h('div', { class: 'cut-lines' },
      h('div', {}, h('span', { class: 'k' }, 'ラジオ'), h('span', { class: 'mono' }, `${fmt(c.start, false)} 〜 ${fmt(c.end, false)}`), h('span', { class: 'muted' }, `(${fmtDuration(c.end - c.start)})`)),
      c.cam
        ? h('div', { title: `ずれ ${c.offset >= 0 ? '+' : ''}${c.offset.toFixed(3)}秒(ラジオの時刻に足すと動画の時刻)` },
            h('span', { class: 'k' }, '映像'), h('span', { class: 'nm' }, basename(c.video)), h('span', { class: 'mono' }, `の ${fmt(c.source0)} から`))
        : h('div', {}, h('span', { class: 'k' }, '画像'), h('span', { class: 'nm' }, stillName(segs[0].still || '')), h('span', { class: 'muted' }, '(カメラ映像なし)')))),
  h('div', { class: 'cut-actions' },
    h('button', { onclick: () => playFrom(c.start), title: 'カットの先頭から再生' }, '▶ 再生'),
    c.cam && h('button', { onclick: () => startSampling(n), title: 'このカットの数か所を4秒ずつ再生' }, '抜き取り'),
    c.cam && h('span', { class: 'shift', title: '映像の位置をカット全体でずらします(←→キーでも可)' },
      h('span', { class: 'lbl' }, 'ずれ'),
      [[-10, '−10'], [-1, '−1'], [1, '+1'], [10, '+10']].map(([f, label]) =>
        h('button', { onclick: () => shiftCut(c, f / FPS) }, label)),
      h('span', { class: 'lbl' }, 'コマ')),
    h('span', { class: 'grow' }),
    c.cam
      ? h('button', { onclick: () => setCutCamera(c, false) }, '静止画にする')
      : [h('button', { onclick: () => chooseStill({ label: `カット${n + 1}`, segments: segs }) }, '画像を選ぶ'),
         prev?.cam && h('button', { onclick: () => extendCut(c, prev, 'prev'), title: '前のカメラ映像をこのカットまで続けます' }, '← 前の映像を延長'),
         next?.cam && h('button', { onclick: () => extendCut(c, next, 'next'), title: '次のカメラ映像をこのカットから始めます' }, '次の映像を延長 →')],
    c.review > 0 && h('button', { class: 'primary', onclick: () => approveCut(c) }, 'OK'),
    h('button', { class: 'ghost', onclick: () => openSegment(c.from), title: '8秒ごとの区間単位で細かく調整します' }, '詳しく')));
}

/* ----- still images ----- */

async function addStillFile() {
  const { paths } = await api('/api/pick', { kind: 'still' });
  if (!paths.length) return '';
  await inspect(paths);
  if (!S.project.stills.includes(paths[0])) commit(() => { S.project.stills.push(paths[0]); });
  return paths[0];
}

// Save the camera picture under the playhead as a still image candidate.
async function captureStill() {
  const ep = curEp();
  const t = playTime();
  const s = ep.segments[segAt(ep, t)];
  if (!s.video) return toast('カメラ映像が映っている所で押してください', 'warn');
  const r = await api('/api/frame', { path: s.video, time: s.source + (t - s.start) });
  await inspect([r.path]);
  if (!S.project.stills.includes(r.path)) commit(() => { S.project.stills.push(r.path); });
  toast('この場面を静止画の候補に追加しました。静止画のカットの「画像を選ぶ」から使えます。');
}

function chooseStill(target) {
  const modal = $('modal');
  const current = target.segments[0].still || '';
  const all = h('input', { type: 'checkbox' });
  const close = () => {
    modal.hidden = true;
    modal.replaceChildren();
    modal.onclick = null;
  };
  const apply = (path) => {
    const ep = curEp();
    const segs = all.checked ? ep.segments.filter((s) => !(s.enabled && s.video)) : target.segments;
    commit(() => {
      for (const s of segs) {
        if (path) s.still = path;
        else delete s.still;
        lock(s);
      }
    });
    syncPlayer(isPlaying());
    close();
    toast(`${all.checked ? 'この話の静止画すべて' : target.label}の画像を「${stillName(path)}」にしました`);
  };
  const tile = (path, label) => {
    const shown = path || S.project.still;
    return h('button', { class: 'still-tile' + (path === current ? ' on' : ''), onclick: () => apply(path) },
      h('div', { class: 'thumb', style: shown ? `background-image:url("${mediaUrl(shown)}")` : '' }),
      h('span', { class: 'nm' }, label));
  };
  modal.replaceChildren(h('div', { class: 'mbox wide', role: 'dialog', 'aria-modal': 'true' },
    h('h3', {}, `${target.label}に表示する画像を選ぶ`),
    h('div', { class: 'still-grid' },
      tile('', stillName('')),
      S.project.stills.map((p) => tile(p, basename(p)))),
    h('label', { class: 'check' }, all, 'この話の静止画すべてに同じ画像を使う'),
    h('p', { class: 'muted small' }, '候補は「画像ファイルを追加」のほか、再生中にプレイヤーの「この場面を静止画候補に」でカメラの場面からも増やせます。'),
    h('div', { class: 'mbtns' },
      h('button', {
        onclick: guard(async () => {
          const path = await addStillFile();
          if (path) apply(path);
        }),
      }, '＋ 画像ファイルを追加'),
      h('button', { class: 'outline', onclick: close }, '閉じる'))));
  modal.onclick = (e) => { if (e.target === modal) close(); };
  modal.hidden = false;
}

function markCurrentCut(scroll = true) {
  S.markedSeg = S.seg;
  for (const el of document.querySelectorAll('#cutList .cut')) {
    const on = S.seg >= Number(el.dataset.from) && S.seg <= Number(el.dataset.to);
    if (on && scroll && !isNarrow() && !el.classList.contains('current')) el.scrollIntoView({ block: 'nearest' });
    el.classList.toggle('current', on);
  }
}

function currentCut() {
  const cuts = cutsOf(curEp());
  return cuts[cutIndexAt(cuts, S.seg)];
}

function playFrom(t) {
  stopSampling();
  seek(t);
  if (!isPlaying()) togglePlay();
}

function shiftCut(c, delta) {
  const ep = curEp();
  commit(() => {
    for (let j = c.from; j <= c.to; j++) {
      const s = ep.segments[j];
      s.source = Math.max(0, Math.round((s.source + delta) * 10000) / 10000);
      lock(s);
    }
  }, `cut-${S.ep}-${c.from}`);
  syncPlayer(isPlaying());
}

function setCutCamera(c, on) {
  const ep = curEp();
  commit(() => {
    for (let j = c.from; j <= c.to; j++) {
      ep.segments[j].enabled = on;
      touch(ep.segments[j]);
    }
  });
  syncPlayer(isPlaying());
  toast('カットを静止画にしました。再生すると参考としてカメラ映像も表示されます。');
}

function extendCut(c, other, side) {
  const ep = curEp();
  const ref = ep.segments[side === 'prev' ? other.to : other.from];
  const length = S.media[ref.video]?.duration ?? Infinity;
  let used = 0;
  commit(() => {
    for (let j = c.from; j <= c.to; j++) {
      const s = ep.segments[j];
      const source = ref.source + (s.start - ref.start);
      if (source < 0 || source + (s.end - s.start) > length + 0.25) continue;
      Object.assign(s, { video: ref.video, source: Math.round(source * 10000) / 10000, enabled: true });
      touch(s);
      used++;
    }
  });
  syncPlayer(isPlaying());
  if (used) toast('カメラ映像を延長しました');
  else toast('カメラのファイルが録画していない時間のため、延長できませんでした', 'warn');
}

function approveCut(c) {
  const ep = curEp();
  commit(() => {
    for (let j = c.from; j <= c.to; j++) if (ep.segments[j].status === 'review') touch(ep.segments[j]);
  });
}

async function approveEpisodeReviews() {
  const ep = curEp();
  const n = reviewCount(ep);
  const ok = await ask('要確認をまとめてOKにしますか?',
    `第${S.ep + 1}話の要確認 ${n} か所を、いまの設定(カメラ映像か静止画か)のまま確認済みにします。「元に戻す」で取り消せます。`,
    [{ label: 'まとめてOK', value: true, primary: true }, { label: 'やめる', value: false }]);
  if (!ok) return;
  commit(() => { for (const s of ep.segments) if (s.status === 'review') touch(s); });
  toast(`${n} か所を確認済みにしました`);
}

function openSegment(j) {
  stopSampling();
  S.seg = j;
  S.tab = 'segment';
  seek(curEp().segments[j].start);
  renderReview();
}

/* ----- spot check: play a few seconds from every cut and every switch ----- */

const SAMPLE_SECONDS = 4;

function samplePlan(ep, only) {
  const cuts = cutsOf(ep);
  const items = [];
  cuts.forEach((c, n) => {
    if (only != null && n !== only) return;
    const len = c.end - c.start;
    if (c.cam) {
      const count = len > 900 ? 4 : len > 180 ? 3 : len > 30 ? 2 : 1;
      const names = [['全体'], ['冒頭', '終盤'], ['冒頭', '中盤', '終盤'], ['冒頭', '前半', '後半', '終盤']][count - 1];
      for (let k = 0; k < count; k++) {
        const t0 = c.start + (count === 1 ? 0 : (len - SAMPLE_SECONDS) * k / (count - 1));
        items.push({ t0, t1: Math.min(c.end, t0 + SAMPLE_SECONDS), label: `カット${n + 1}の${names[k]}` });
      }
    }
    if (only == null && n > 0) {
      items.push({ t0: Math.max(0, c.start - 2.5), t1: Math.min(ep.duration, c.start + 2.5), label: `カット${n}→${n + 1}の切り替わり` });
    }
    // every stretch that needs review gets its own look
    for (let j = c.from; j <= c.to; j++) {
      const s = ep.segments[j];
      if (s.status === 'review' && s.enabled && (j === c.from || ep.segments[j - 1].status !== 'review')) {
        items.push({ t0: s.start, t1: Math.min(c.end, s.start + SAMPLE_SECONDS), label: `カット${n + 1}の要確認(${fmt(s.start, false)})` });
      }
    }
  });
  items.sort((a, b) => a.t0 - b.t0);
  const merged = [];
  for (const it of items) {
    const last = merged[merged.length - 1];
    if (last && it.t0 <= last.t1 + 0.5) {
      last.t1 = Math.max(last.t1, it.t1);
      last.label += '・' + it.label;
    } else {
      merged.push({ ...it });
    }
  }
  return merged;
}

function startSampling(only) {
  const items = samplePlan(curEp(), only);
  if (!items.length) return toast('確認するカメラ映像がありません', 'warn');
  S.sample = { items, i: 0 };
  playSample();
}

function playSample() {
  seek(S.sample.items[S.sample.i].t0);
  renderSampleBar();
  if (!isPlaying()) togglePlay();
}

function nextSample(dir) {
  if (!S.sample) return;
  S.sample.i = Math.max(0, S.sample.i + dir);
  if (S.sample.i >= S.sample.items.length) {
    stopSampling();
    pause();
    toast('抜き取り確認が終わりました。ずれていたカットは「カット一覧」でまとめて直せます。');
    return;
  }
  playSample();
}

function stopSampling() {
  if (!S.sample) return;
  S.sample = null;
  renderSampleBar();
}

function renderSampleBar() {
  $('sampleBar').hidden = !S.sample;
  if (S.sample) {
    const it = S.sample.items[S.sample.i];
    $('sampleText').textContent = `抜き取り確認 ${S.sample.i + 1}/${S.sample.items.length}:${it.label}(${fmt(it.t0, false)})`;
  }
}

function renderEpisodeList() {
  $('episodeList').replaceChildren(h('h3', {}, '話'), ...S.project.episodes.map((e, i) => {
    const n = reviewCount(e);
    return h('button', { class: 'ep' + (i === S.ep ? ' current' : ''), onclick: () => selectEpisode(i) },
      h('span', { class: 't' }, `第${i + 1}話`, n ? h('span', { class: 'chip review' }, `要確認 ${n}`) : h('span', { class: 'chip done' }, '確認済み')),
      h('span', { class: 'n', title: e.audio }, basename(e.audio)),
      h('span', { class: 'n' }, fmtDuration(e.duration)));
  }));
}

function renderReviewBar() {
  const ep = curEp();
  const counts = { auto: 0, manual: 0, review: 0, still: 0 };
  for (const s of ep.segments) counts[segKind(s)]++;
  const total = reviewCount();
  $('reviewBar').replaceChildren(
    h('div', { class: 'title' },
      h('h2', {}, `第${S.ep + 1}話`),
      h('span', { class: 'muted' }, basename(ep.audio))),
    h('div', { class: 'legend' }, Object.entries(counts).map(([k, n]) =>
      h('span', {}, h('i', { style: `background:var(--${k})` }), `${KIND_LABEL[k]} ${n}`))),
    h('div', { class: 'row' },
      h('button', { disabled: !total, onclick: () => gotoReview(-1) }, '◀ 前の要確認'),
      h('button', { disabled: !total, onclick: () => gotoReview(1) }, '次の要確認 ▶')));
}

function renderTimelines() {
  const ep = curEp();
  const s = curSeg();
  $('tlTotal').textContent = fmt(ep.duration, false);
  const overview = $('overview');
  overview.replaceChildren(...ep.segments.map((x, j) => h('div', {
    class: `tseg ${segKind(x)}${j === S.seg ? ' sel' : ''}`,
    style: `left:${x.start / ep.duration * 100}%;width:${(x.end - x.start) / ep.duration * 100}%`,
  })), h('div', { class: 'playhead', id: 'phOverview' }));
  scrubbable(overview, (ratio) => ratio * ep.duration);

  const [a, b] = detailWindow(ep, s);
  $('detailRange').textContent = `${fmt(a, false)} 〜 ${fmt(b, false)}(カットの境目のつまみをドラッグで切り替わり位置を変更)`;
  for (const btn of $('detailZoom').querySelectorAll('button')) btn.classList.toggle('on', Number(btn.dataset.span) === S.detailSpan);
  const detail = $('detail');
  const kids = [];
  for (let j = 0; j < ep.segments.length; j++) {
    const x = ep.segments[j];
    if (x.end <= a || x.start >= b) continue;
    const left = (Math.max(x.start, a) - a) / (b - a) * 100;
    const width = (Math.min(x.end, b) - Math.max(x.start, a)) / (b - a) * 100;
    kids.push(h('div', {
      class: `tseg ${segKind(x)}${j === S.seg ? ' sel' : ''}`,
      style: `left:${left}%;width:${width}%`,
      title: `区間${j + 1}  ${fmt(x.start)}〜${fmt(x.end)}  ${KIND_LABEL[segKind(x)]}`,
    }, width > 3 ? h('span', {}, j + 1) : null));
    if (j > 0 && x.start > a && !continues(ep.segments[j - 1], x)) {
      kids.push(h('div', {
        class: 'handle', style: `left:${left}%`,
        title: 'ドラッグして、カメラ映像の始まり・終わり(カットの切り替わり)を動かします',
        onpointerdown: (e) => dragBoundary(e, j, a, b),
      }));
    }
  }
  detail.replaceChildren(...kids, h('div', { class: 'playhead', id: 'phDetail' }));
  scrubbable(detail, (ratio) => a + ratio * (b - a));
  const slot = $('reviewBarSlot');
  if (slot && ep) slot.replaceChildren(playBar('reviewBar', playTime(), ep.duration, (t) => seek(t)));
  movePlayhead(playTime());
}

// Keep receiving pointer moves while dragging outside the element (not available for every pointer).
function capture(el, e) {
  try {
    el.setPointerCapture(e.pointerId);
  } catch { /* the drag still works while the pointer stays over the element */ }
}

// Press and drag on a timeline to move the playhead.
function scrubbable(el, toTime) {
  const at = (e) => {
    const r = el.getBoundingClientRect();
    return Math.max(0, Math.min(curEp().duration - 0.001, toTime((e.clientX - r.left) / r.width)));
  };
  el.onpointerdown = (e) => {
    if (e.target.closest('.handle')) return;
    capture(el, e);
    stopSampling();
    seek(at(e));
    el.onpointermove = (ev) => seek(at(ev));
    el.onpointerup = (ev) => {
      el.onpointermove = el.onpointerup = null;
      pickTime(at(ev));
    };
  };
}

function detailWindow(ep, s) {
  const span = Math.min(S.detailSpan, ep.duration);
  let a = Math.max(0, (s.start + s.end) / 2 - span / 2);
  a = Math.min(a, ep.duration - span);
  return [Math.max(0, a), Math.max(0, a) + span];
}

// How far a cut boundary may move: not past the neighbouring cuts, and not beyond what the camera files recorded.
function boundaryLimits(ep, j) {
  const cuts = cutsOf(ep);
  const L = cuts[cutIndexAt(cuts, j - 1)];
  const R = cuts[cutIndexAt(cuts, j)];
  let lo = L.start + 1 / FPS;
  let hi = R.end - 1 / FPS;
  const left = ep.segments[j - 1];
  const right = ep.segments[j];
  const leftLength = S.media[left.video]?.duration;
  if (left.video && leftLength) hi = Math.min(hi, left.start + leftLength - left.source);
  if (right.video) lo = Math.max(lo, right.start - right.source);
  return [Math.ceil(lo * FPS) / FPS, Math.floor(Math.max(lo, hi) * FPS) / FPS];
}

function dragBoundary(e, j, a, b) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  const ep = curEp();
  const r = $('detail').getBoundingClientRect();
  const [lo, hi] = boundaryLimits(ep, j);
  let t = ep.segments[j].start;
  capture(handle, e);
  handle.classList.add('dragging');
  pause();
  handle.onpointermove = (ev) => {
    const raw = a + (ev.clientX - r.left) / r.width * (b - a);
    t = Math.round(Math.max(lo, Math.min(hi, raw)) * FPS) / FPS;
    handle.style.left = `${(t - a) / (b - a) * 100}%`;
    $('detailRange').textContent = `切り替わり位置 ${fmt(t)}` + (raw < lo - 0.02 || raw > hi + 0.02 ? '(これ以上は動かせません)' : '');
    seek(t);
  };
  handle.onpointerup = () => {
    handle.onpointermove = handle.onpointerup = null;
    moveBoundary(j, t);
  };
}

function splitAt(ep, t) {
  const j = segAt(ep, t);
  const s = ep.segments[j];
  if (t - s.start < 1e-6 || s.end - t < 1e-6) return;
  const off = t - s.start;
  ep.segments.splice(j, 1, { ...s, end: t, candidates: [...(s.candidates || [])] }, {
    ...s, start: t, source: Math.round((s.source + off) * 10000) / 10000,
    candidates: (s.candidates || []).map((c) => ({ ...c, source: c.source + off })),
  });
}

// Move the boundary in front of segment j to t: the cut on the growing side continues into the freed time.
function moveBoundary(j, t) {
  const ep = curEp();
  const old = ep.segments[j].start;
  if (Math.abs(t - old) < 0.5 / FPS) return renderTimelines();
  const ref = { ...(t > old ? ep.segments[j - 1] : ep.segments[j]) };
  const lo = Math.min(t, old);
  const hi = Math.max(t, old);
  commit(() => {
    splitAt(ep, t);
    for (const s of ep.segments) {
      if (s.start < lo - 1e-6 || s.end > hi + 1e-6) continue;
      s.enabled = ref.enabled;
      s.video = ref.video;
      s.source = ref.video ? Math.max(0, Math.round((ref.source + (s.start - ref.start)) * 10000) / 10000) : 0;
      touch(s);
    }
    S.seg = segAt(ep, t);
  });
  seek(t);
  toast(`切り替わり位置を ${fmt(t)} に動かしました(Ctrl+Zで元に戻せます)`);
}

function movePlayhead(t) {
  const ep = curEp();
  if (!ep) return;
  movePlayBar('reviewBar', t, ep.duration);
  const o = $('phOverview');
  if (o) o.style.left = `calc(${t / ep.duration * 100}% - 1px)`;
  const d = $('phDetail');
  if (d) {
    const [a, b] = detailWindow(ep, curSeg());
    d.hidden = t < a || t > b;
    d.style.left = `calc(${(t - a) / (b - a) * 100}% - 1px)`;
  }
  $('clock').textContent = `${fmt(t)} / ${fmt(ep.duration, false)}`;
}

function statusChip(s) {
  const k = segKind(s);
  return h('span', { class: 'chip ' + k }, KIND_LABEL[k]);
}

function renderInspector() {
  const ep = curEp();
  const s = curSeg();
  const box = $('inspector');
  if (S.tab !== 'segment') return;
  if (!s) return box.replaceChildren(h('p', { class: 'muted' }, '区間がありません'));
  const j = S.seg;
  const prev = ep.segments[j - 1];
  const next = ep.segments[j + 1];
  const useCam = s.enabled && !!s.video;
  const info = S.media[s.video];
  const length = s.end - s.start;
  const outOfRange = useCam && info?.duration && s.source + length > info.duration + 0.25;

  const kids = [
    h('div', { class: 'ihead' },
      h('div', {},
        h('div', { class: 'ititle' }, `区間 ${j + 1}`, statusChip(s)),
        h('div', { class: 'muted mono small' }, `${fmt(s.start)} 〜 ${fmt(s.end)}(${length.toFixed(1)}秒)`)),
      h('span', { class: 'muted small' }, `${j + 1} / ${ep.segments.length}`)),
  ];

  if (s.status === 'review') {
    kids.push(h('div', { class: 'notice warn' }, REVIEW_REASON[s.reason] || (useCam ? REVIEW_REASON.switch : REVIEW_REASON.nocamera)));
  }

  kids.push(h('div', { class: 'field' },
    h('label', {}, 'この区間の映像'),
    h('div', { class: 'switch' },
      h('button', { class: useCam ? 'on' : '', onclick: () => setUseCamera(true), disabled: !S.project.videos.length }, 'カメラ映像を使う'),
      h('button', { class: useCam ? '' : 'on', onclick: () => setUseCamera(false) }, '静止画にする'))));

  if (useCam) {
    kids.push(
      h('div', { class: 'field' },
        h('label', {}, '使う動画'),
        h('select', {
          value: s.video,
          onchange: (e) => commit(() => { s.video = e.target.value; touch(s); }),
        }, S.project.videos.map((v, i) => h('option', { value: v }, `動画${i + 1}:${basename(v)}`)))),
      h('div', { class: 'field' },
        h('label', {}, '動画内の位置', h('span', { class: 'hint' }, 'この区間の先頭が、動画の何分何秒か')),
        h('div', { class: 'posrow' },
          h('input', {
            class: 'time', value: fmt(s.source), title: '例: 1:23.456',
            onchange: (e) => {
              const v = parseTime(e.target.value);
              if (!(v >= 0)) {
                toast('「1:23.456」または「83.456」の形で入力してください', 'warn');
                e.target.value = fmt(s.source);
                return;
              }
              setSource(v);
            },
            onkeydown: (e) => { if (e.key === 'Enter') e.target.blur(); },
          }),
          info?.duration ? h('span', { class: 'muted small' }, `動画の長さ ${fmt(info.duration, false)}`) : null),
        h('div', { class: 'nudges' }, [[-1, '−1秒'], [-10 / FPS, '−10コマ'], [-1 / FPS, '−1コマ'], [1 / FPS, '+1コマ'], [10 / FPS, '+10コマ'], [1, '+1秒']]
          .map(([d, label]) => h('button', { onclick: () => nudge(d) }, label))),
        outOfRange && h('div', { class: 'notice bad' }, 'この位置だと動画の長さを超えます。位置を前にずらすか、「再生位置で分割」で区間を分けてください。')),
      h('div', { class: 'quick' },
        h('button', { disabled: !(prev?.enabled && prev.video), onclick: () => continueFrom(prev, 'prev'), title: '前の区間と同じ動画の、続きの位置にします' }, '← 前の区間の続きにする'),
        h('button', { disabled: !(next?.enabled && next.video), onclick: () => continueFrom(next, 'next'), title: '次の区間と同じ動画の、つながる位置にします' }, '次の区間とつなげる →'),
        h('button', { disabled: length < 1 || S.searching, onclick: guard(searchNearby) }, S.searching ? '探しています…' : 'この付近を自動で探す')));
  } else {
    kids.push(h('div', { class: 'field' },
      h('label', {}, '表示する画像'),
      h('div', { class: 'still-pick' },
        h('div', { class: 'thumb', style: stillOf(s) ? `background-image:url("${mediaUrl(stillOf(s))}")` : '' }),
        h('span', {}, stillName(s.still || '')),
        h('button', { onclick: () => chooseStill({ label: `区間${j + 1}`, segments: [s] }) }, '画像を選ぶ'))));
    if (s.status === 'review' && (prev?.enabled && prev.video)) {
      kids.push(h('div', { class: 'quick' },
        h('button', { onclick: () => continueFrom(prev, 'prev') }, '← 前の区間の続きの映像を使う')));
    }
  }

  if (s.candidates?.length) {
    kids.push(h('details', { class: 'cands', open: s.status === 'review' },
      h('summary', {}, `自動照合の候補(${s.candidates.length})`),
      h('ul', {}, s.candidates.map((c) => {
        const on = useCam && c.video === s.video && Math.abs(c.source - s.source) < 0.001;
        const level = c.score >= 0.5 ? 'hi' : c.score >= 0.3 ? 'mid' : 'lo';
        return h('li', {}, h('button', { class: 'cand' + (on ? ' on' : ''), onclick: () => applyCandidate(c) },
          h('span', { class: 'nm' }, basename(c.video)),
          h('span', { class: 'mono' }, fmt(c.source)),
          h('span', { class: 'score ' + level }, `一致 ${c.score.toFixed(2)}`)));
      })),
      h('p', { class: 'muted small' }, '一致スコアは正解の確率ではありません。再生して確かめてください。')));
  }

  kids.push(
    h('div', { class: 'tools' },
      h('button', { class: 'ghost', onclick: splitHere, title: '再生位置(▼)で区間を2つに分けます。カメラのファイルの切れ目などに使います' }, '再生位置で分割'),
      h('button', { class: 'ghost', disabled: !next, onclick: mergeNext, title: '次の区間をこの区間に取り込みます' }, '⇥ 次の区間と結合')),
    h('div', { class: 'ifoot' },
      h('button', { class: 'primary big', onclick: approveAndNext },
        s.status === 'review' ? 'これでOK・次の要確認へ' : '次の要確認へ', h('kbd', {}, 'Enter'))));

  box.replaceChildren(...kids);
}

/* ----- segment edits ----- */

function setUseCamera(on) {
  const ep = curEp();
  const s = curSeg();
  commit(() => {
    if (on) {
      // Continuing a neighbouring shot is the best guess; then the strongest match; then the first camera.
      if (!s.video) {
        const prev = ep.segments[S.seg - 1];
        const next = ep.segments[S.seg + 1];
        const best = s.candidates?.[0];
        if (prev?.enabled && prev.video) {
          s.video = prev.video;
          s.source = Math.round((prev.source + (s.start - prev.start)) * 10000) / 10000;
        } else if (next?.enabled && next.video && next.source - (next.start - s.start) >= 0) {
          s.video = next.video;
          s.source = Math.round((next.source - (next.start - s.start)) * 10000) / 10000;
        } else if (best) {
          s.video = best.video;
          s.source = best.source;
        } else {
          s.video = S.project.videos[0];
          s.source = 0;
        }
      }
      s.enabled = true;
    } else {
      s.enabled = false;
    }
    touch(s);
  });
  syncPlayer(false);
}

function setSource(value) {
  const s = curSeg();
  commit(() => {
    s.source = Math.max(0, Math.round(value * 10000) / 10000);
    touch(s);
  }, `source-${S.ep}-${S.seg}`);
  keepPlayheadInSegment();
  syncPlayer(isPlaying());
}

// Arrow keys move the whole cut on the cut list, or only the segment on the detail tab.
function nudgeKey(delta) {
  if (S.tab !== 'cuts') return nudge(delta);
  const c = currentCut();
  if (c?.cam) shiftCut(c, delta);
}

function nudge(delta) {
  const s = curSeg();
  if (!s?.enabled || !s.video) return;
  setSource(s.source + delta);
}

function continueFrom(other, side) {
  const s = curSeg();
  const source = other.source + (s.start - other.start);
  if (source < 0) return toast('つなげると動画の先頭より前になるため、合わせられません', 'warn');
  commit(() => {
    s.video = other.video;
    s.source = Math.round(source * 10000) / 10000;
    s.enabled = true;
    touch(s);
  });
  syncPlayer(false);
  toast(side === 'prev' ? '前の区間の続きの位置にしました' : '次の区間につながる位置にしました');
}

function applyCandidate(c) {
  const s = curSeg();
  commit(() => {
    s.video = c.video;
    s.source = c.source;
    s.enabled = true;
    touch(s);
  });
  keepPlayheadInSegment();
  syncPlayer(false);
}

async function searchNearby() {
  const ep = curEp();
  const s = curSeg();
  const where = { ep: S.ep, seg: S.seg };
  S.searching = true;
  renderInspector();
  try {
    const r = await api('/api/rematch', {
      audio: ep.audio, start: s.start, end: s.end, video: s.video, center: s.source, radius: 20,
    });
    const best = r.candidates[0];
    if (!best || best.score < 0.3) {
      toast('前後20秒の範囲では一致する位置が見つかりませんでした。動画を変えるか、候補から選んでください。', 'warn');
      return;
    }
    if (S.ep !== where.ep || S.seg !== where.seg) return;
    commit(() => {
      s.source = best.source;
      const others = (s.candidates || []).filter((c) => !(c.video === best.video && Math.abs(c.source - best.source) < 0.01));
      s.candidates = [...r.candidates, ...others].sort((x, y) => y.score - x.score).slice(0, 5);
      touch(s);
    });
    syncPlayer(false);
    toast(`一致スコア ${best.score.toFixed(2)} の位置に合わせました。再生して確かめてください。`);
  } finally {
    S.searching = false;
    if (S.step === 'review') renderInspector();
  }
}

function splitHere() {
  const ep = curEp();
  const s = curSeg();
  const t = Math.round(playTime() * FPS) / FPS;
  if (!(t > s.start + 0.2 && t < s.end - 0.2)) {
    toast('再生位置(▼)をこの区間の中に移動してから押してください', 'warn');
    return;
  }
  commit(() => {
    const offset = t - s.start;
    const reviewing = s.status === 'review';
    const first = { ...s, end: t, candidates: [...(s.candidates || [])] };
    const second = {
      ...s, start: t, source: Math.round((s.source + offset) * 10000) / 10000,
      candidates: (s.candidates || []).map((c) => ({ ...c, source: c.source + offset })),
    };
    for (const part of [first, second]) {
      part.status = reviewing ? 'review' : 'manual';
      part.locked = !reviewing;
    }
    ep.segments.splice(S.seg, 1, first, second);
    S.seg += 1;
  });
  toast('区間を2つに分けました。後半を表示しています。');
}

function mergeNext() {
  const ep = curEp();
  const s = curSeg();
  const next = ep.segments[S.seg + 1];
  if (!next) return;
  commit(() => {
    s.end = next.end;
    s.candidates = s.candidates || [];
    ep.segments.splice(S.seg + 1, 1);
    if (s.status !== 'review') touch(s);
  });
  toast('次の区間を取り込みました。映像の位置はこの区間の設定を使います。');
}

function approveAndNext() {
  if (S.tab === 'cuts') {
    const c = currentCut();
    if (c?.review) approveCut(c);
    return gotoReview(1, true);
  }
  const s = curSeg();
  if (s?.status === 'review') commit(() => touch(s));
  gotoReview(1, true);
}

function gotoReview(dir, fromApprove = false) {
  const flat = [];
  S.project.episodes.forEach((e, i) => e.segments.forEach((s, j) => flat.push([i, j, s])));
  if (!flat.length) return;
  const here = flat.findIndex(([i, j]) => i === S.ep && j === S.seg);
  for (let k = 1; k <= flat.length; k++) {
    const [i, j, s] = flat[((here + dir * k) % flat.length + flat.length) % flat.length];
    if (s.status === 'review') {
      selectSegment(i, j);
      return;
    }
  }
  if (fromApprove || !reviewCount()) {
    renderAll();
    toast('要確認の区間はもうありません。「4 書き出し」に進めます。');
  }
}

function selectEpisode(i) {
  if (i === S.ep) return;
  pause();
  S.sample = null;
  S.ep = i;
  S.seg = 0;
  renderReview();
  loadEpisodeAudio(0);
}

function selectSegment(i, j) {
  const changedEpisode = i !== S.ep;
  if (changedEpisode) pause();
  S.ep = i;
  S.seg = j;
  const s = curSeg();
  renderAll();
  if (changedEpisode) loadEpisodeAudio(s.start);
  else seek(s.start);
}

function pickTime(t) {
  const ep = curEp();
  t = Math.max(0, Math.min(ep.duration - 0.001, t));
  stopSampling();
  S.seg = segAt(ep, t);
  seek(t);
  refreshSelection();
}

function keepPlayheadInSegment() {
  const s = curSeg();
  const t = playTime();
  if (t < s.start || t >= s.end) seek(s.start);
}

/* ---------- waveform & drag adjustment ---------- */

// The window follows the playhead; the camera lane uses the mapping of the segment under the playhead,
// so its peaks sit under the radio peaks exactly when that position is right.
const W = { span: 6, radio: null, cams: {}, pending: {}, drag: null };

function waveWindow() {
  const ep = curEp();
  const span = Math.min(W.span, ep.duration);
  const a = Math.max(0, Math.min(playTime() - span / 2, ep.duration - span));
  return [a, a + span];
}

function ensureEnvelope(slot, path, t0, t1) {
  const have = slot === 'radio' ? W.radio : W.cams[path];
  if (have && have.path === path && have.t0 <= Math.max(0, t0) + 1e-6 && have.until >= t1) return;
  const key = `${slot}|${path}`;
  if (W.pending[key]) return;
  const start = Math.max(0, t0 - 15);
  const duration = Math.min(120, t1 - start + 15);
  W.pending[key] = true;
  api('/api/envelope', { path, start, duration })
    .then((d) => {
      const entry = { ...d, path, until: start + duration };
      if (slot === 'radio') W.radio = entry;
      else W.cams[path] = entry;
      drawWave();
    })
    .catch(() => {})
    .finally(() => { delete W.pending[key]; });
}

function drawWave() {
  const canvas = $('wave');
  const ep = curEp();
  if (!canvas || !ep || S.step !== 'review') return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const hgt = canvas.clientHeight;
  if (!w) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(hgt * dpr);
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const [a, b] = waveWindow();
  const span = b - a;
  const x = (t) => (t - a) / span * w;
  const mid = hgt / 2;
  const here = ep.segments[segAt(ep, playTime())];
  const mapping = here.video ? here : null;

  ensureEnvelope('radio', ep.audio, a, b);
  if (mapping) ensureEnvelope('cam', mapping.video, mapping.source + (a - mapping.start), mapping.source + (b - mapping.start));

  const target = adjustTarget();
  if (target) {
    g.fillStyle = css.getPropertyValue('--wave-seg');
    g.fillRect(x(target.start), 0, x(target.end) - x(target.start), hgt);
  }

  const lane = (data, toSource, color, up) => {
    if (!data) return false;
    g.fillStyle = color;
    const room = mid - 6;
    for (let px = 0; px < w; px++) {
      const t0 = a + px / w * span;
      const t1 = a + (px + 1) / w * span;
      const i0 = Math.floor((toSource(t0) - data.t0) * data.rate);
      const i1 = Math.ceil((toSource(t1) - data.t0) * data.rate);
      let m = -1;
      for (let i = Math.max(0, i0); i <= Math.min(i1, data.values.length - 1); i++) m = Math.max(m, data.values[i]);
      if (m < 0) continue;
      const bar = Math.max(1, m * room);
      g.fillRect(px, up ? mid - 2 - bar : mid + 2, 1, bar);
    }
    return true;
  };
  const radioData = W.radio?.path === ep.audio ? W.radio : null;
  const haveRadio = lane(radioData, (t) => t, css.getPropertyValue('--radio'), true);
  const camData = mapping ? W.cams[mapping.video] : null;
  const haveCam = !mapping || lane(camData, (t) => mapping.source + (t - mapping.start), css.getPropertyValue('--cam'), false);

  g.strokeStyle = 'rgba(17,24,39,.3)';
  g.setLineDash([4, 4]);
  for (let j = segAt(ep, a); j < ep.segments.length && ep.segments[j].start < b; j++) {
    const t = ep.segments[j].start;
    if (t <= a) continue;
    g.beginPath(); g.moveTo(x(t) + .5, 0); g.lineTo(x(t) + .5, hgt); g.stroke();
  }
  g.setLineDash([]);
  g.strokeStyle = 'rgba(17,24,39,.15)';
  g.beginPath(); g.moveTo(0, mid + .5); g.lineTo(w, mid + .5); g.stroke();
  g.fillStyle = '#111827';
  g.fillRect(x(playTime()) - 1, 0, 2, hgt);

  g.fillStyle = css.getPropertyValue('--muted');
  g.font = '12px sans-serif';
  if (!mapping) g.fillText('この位置はカメラ映像なし(静止画)', 8, hgt - 8);
  if (!haveRadio || !haveCam) g.fillText('波形を読み込み中…', 8, 16);
}

// What a drag or the slider moves: the whole cut on the cut list, one segment on the detail tab.
function adjustTarget() {
  const ep = curEp();
  if (!ep || LIVE.target) return LIVE.target;
  if (S.tab === 'cuts') {
    const cuts = cutsOf(ep);
    const n = cutIndexAt(cuts, S.seg);
    const c = cuts[n];
    if (!c?.cam) return null;
    return { label: `カット${n + 1}全体`, segments: ep.segments.slice(c.from, c.to + 1), start: c.start, end: c.end };
  }
  const s = curSeg();
  if (!s?.enabled || !s.video) return null;
  return { label: `区間${S.seg + 1}`, segments: [s], start: s.start, end: s.end };
}

const LIVE = { snap: null, applied: 0, target: null };

function beginLive() {
  if (LIVE.snap) return;
  const target = adjustTarget();
  if (!target) return;
  LIVE.snap = snapshot();
  LIVE.applied = 0;
  LIVE.target = target;
}

// delta: seconds added to the camera position since beginLive. Edits in place so the dragged control survives.
function applyLive(delta) {
  if (!LIVE.snap) return;
  const step = delta - LIVE.applied;
  if (Math.abs(step) < 1e-9) return;
  for (const s of LIVE.target.segments) s.source = Math.max(0, Math.round((s.source + step) * 10000) / 10000);
  LIVE.applied = delta;
  renderWaveControls();
  syncPlayer(isPlaying());
  drawWave();
}

function endLive() {
  if (!LIVE.snap) return;
  const { snap, applied, target } = LIVE;
  Object.assign(LIVE, { snap: null, applied: 0, target: null });
  if (Math.abs(applied) > 1e-9) {
    for (const s of target.segments) (target.segments.length > 1 ? lock : touch)(s);
    S.undo.push(snap);
    if (S.undo.length > 100) S.undo.shift();
    S.redo = [];
    S.coalesce = null;
    markDirty();
    renderAll();
    const frames = Math.round(-applied * FPS);
    toast(`${target.label}の映像を${Math.abs(frames)}コマ${frames > 0 ? '遅らせました' : '早めました'}(Ctrl+Zで元に戻せます)`);
  }
  renderWaveControls();
}

function renderWaveControls() {
  const target = adjustTarget();
  const slider = $('offsetSlider');
  slider.disabled = !target;
  if (!LIVE.snap) slider.value = 0;
  $('waveTarget').textContent = target ? `ドラッグで動かす範囲:${target.label}` : 'この位置はカメラ映像なし';
  for (const btn of $('waveZoom').querySelectorAll('button')) btn.classList.toggle('on', Number(btn.dataset.span) === W.span);
  if (!target) {
    $('offsetLabel').textContent = '';
    return;
  }
  const f = target.segments[0];
  const offset = f.source - f.start;
  const frames = Math.round(-LIVE.applied * FPS);
  $('offsetLabel').textContent = `ずれ ${offset >= 0 ? '+' : ''}${offset.toFixed(3)}秒` +
    (frames ? `(${Math.abs(frames)}コマ${frames > 0 ? '遅らせる' : '早める'})` : '');
}

function wireWave() {
  const canvas = $('wave');
  canvas.addEventListener('pointerdown', (e) => {
    const [a, b] = waveWindow();
    W.drag = { x: e.clientX, width: canvas.getBoundingClientRect().width, a, span: b - a, moved: false };
    capture(canvas, e);
  });
  canvas.addEventListener('pointermove', (e) => {
    const d = W.drag;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!d.moved) {
      if (Math.abs(dx) < 4 || !adjustTarget()) return;
      d.moved = true;
      beginLive();
      canvas.classList.add('dragging');
    }
    // dragging the camera waveform right shows each camera moment later: the video is delayed
    applyLive(-Math.round(dx / d.width * d.span * FPS) / FPS);
  });
  canvas.addEventListener('pointerup', (e) => {
    const d = W.drag;
    W.drag = null;
    canvas.classList.remove('dragging');
    if (!d) return;
    if (d.moved) endLive();
    else pickTime(d.a + (e.clientX - canvas.getBoundingClientRect().left) / d.width * d.span);
  });
  canvas.addEventListener('pointercancel', () => {
    W.drag = null;
    canvas.classList.remove('dragging');
    endLive();
  });
  const slider = $('offsetSlider');
  slider.addEventListener('input', () => {
    beginLive();
    applyLive(-Number(slider.value) / FPS);
  });
  slider.addEventListener('change', () => endLive());
  for (const btn of $('waveZoom').querySelectorAll('button')) {
    btn.onclick = () => { W.span = Number(btn.dataset.span); renderWaveControls(); drawWave(); };
  }
  for (const btn of $('detailZoom').querySelectorAll('button')) {
    btn.onclick = () => { S.detailSpan = Number(btn.dataset.span); renderTimelines(); };
  }
}

/* ---------- player ---------- */

const radio = $('radio');
const cam = $('cam');
const P = { radioUrl: '', camUrl: '', pendingSeek: null, raf: 0, lastWave: 0, lastCamSeek: 0 };

const isPlaying = () => !radio.paused && !radio.ended;
const playTime = () => (P.pendingSeek != null ? P.pendingSeek : radio.currentTime || 0);

function loadEpisodeAudio(at) {
  const ep = curEp();
  if (!ep) return;
  const url = playableUrl(ep.audio);
  if (!url) {
    $('overlay').textContent = mediaWaitMessage(ep.audio);
    setTimeout(() => { if (S.step === 'review' && curEp() === ep) loadEpisodeAudio(at); }, 1000);
    return;
  }
  if (P.radioUrl !== url) {
    P.radioUrl = url;
    P.pendingSeek = at ?? curSeg()?.start ?? 0;
    radio.src = url;
  } else if (at != null) {
    seek(at);
  }
  syncPlayer(false);
}

function seek(t) {
  if (radio.readyState >= 1 && P.radioUrl) {
    radio.currentTime = t;
    P.pendingSeek = null;
  } else {
    P.pendingSeek = t;
  }
  movePlayhead(t);
  syncPlayer(isPlaying());
  drawWave();
}

radio.addEventListener('loadedmetadata', () => {
  if (P.pendingSeek != null) {
    radio.currentTime = P.pendingSeek;
    P.pendingSeek = null;
  }
  syncPlayer(false);
});
radio.addEventListener('play', () => { updatePlayButton(); tick(); });
radio.addEventListener('pause', () => { cam.pause(); updatePlayButton(); syncPlayer(false); });
radio.addEventListener('ended', () => { cam.pause(); updatePlayButton(); });
radio.addEventListener('error', () => {
  if (P.radioUrl) $('overlay').textContent = 'ラジオ音声を再生できませんでした。ファイルを確認してください。';
});
cam.addEventListener('loadedmetadata', () => syncPlayer(isPlaying()));
cam.addEventListener('error', () => {
  if (!P.camUrl) return;
  const failed = Object.values(S.media).find((m) => m.hevc && !m.proxy_ready && mediaUrl(m.path) === P.camUrl);
  if (failed) {  // the browser could not decode HEVC after all: fall back to a converted preview
    failed.directFailed = true;
    P.camUrl = '';
    syncPlayer(isPlaying());
    return;
  }
  $('overlay').textContent = 'この動画をブラウザで再生できませんでした。';
});

function updatePlayButton() {
  $('btnPlay').textContent = isPlaying() ? '‖ 停止' : '▶ 再生';
}

function tick() {
  cancelAnimationFrame(P.raf);
  const loop = (now) => {
    if (!isPlaying()) return;
    if (S.sample && playTime() >= S.sample.items[S.sample.i].t1 - 0.02) {
      nextSample(1);
      if (!S.sample) return;
    }
    syncPlayer(true);
    if (now - P.lastWave > 90) {
      P.lastWave = now;
      drawWave();
    }
    P.raf = requestAnimationFrame(loop);
  };
  P.raf = requestAnimationFrame(loop);
}

async function togglePlay() {
  if (S.step !== 'review') return;
  if (isPlaying()) return pause();
  if (!P.radioUrl) {
    loadEpisodeAudio();
    return toast('音声を準備しています。少し待ってから再生してください', 'warn');
  }
  try {
    await radio.play();
  } catch {
    toast('再生を開始できませんでした', 'bad');
  }
}

function pause() {
  radio.pause();
  cam.pause();
}

function syncPlayer(playing) {
  if (S.step !== 'review') return;
  const ep = curEp();
  if (!ep?.segments.length) return;
  const t = playTime();
  const j = segAt(ep, t);
  if (playing && j !== S.seg) {
    S.seg = j;
    refreshSelection();
  }
  movePlayhead(t);
  const s = ep.segments[j];
  const overlay = $('overlay');
  const showVideo = (on) => {
    cam.hidden = !on;
    const still = stillOf(s);
    $('stillImg').hidden = on || !still;
    $('placeholder').hidden = on || !!still;
    if (!on && still && $('stillImg').dataset.src !== still) {
      $('stillImg').dataset.src = still;
      $('stillImg').src = mediaUrl(still);
    }
    $('placeholder').textContent = '静止画(画像は未設定)';
  };

  cam.muted = !$('chkCamAudio').checked;
  // A segment switched to the still image keeps its camera position: show that footage for reference.
  const reference = !s.enabled && !!s.video;
  $('screen').classList.toggle('reference', reference);
  $('btnCapture').disabled = !s.video;
  if (!s.video) {
    cam.pause();
    showVideo(false);
    $('srcBadge').textContent = '';
    overlay.textContent = s.status === 'review' ? '要確認:カメラ映像が見つからないため静止画です' : 'カメラ映像なし(静止画)';
    return;
  }
  $('srcBadge').textContent = `${basename(s.video)}  ${fmt(s.source + (t - s.start))}${reference ? '(参考)' : ''}`;
  const url = playableUrl(s.video);
  if (!url) {
    cam.pause();
    showVideo(false);
    overlay.textContent = mediaWaitMessage(s.video);
    return;
  }
  const want = s.source + (t - s.start);
  const info = S.media[s.video];
  if (info?.duration && want > info.duration + 0.05) {
    cam.pause();
    showVideo(false);
    overlay.textContent = 'この位置は動画の長さを超えています';
    return;
  }
  overlay.textContent = reference ? '出力ではここは静止画になります(参考としてカメラ映像を表示中)' : '';
  showVideo(true);
  if (P.camUrl !== url) {
    P.camUrl = url;
    cam.src = url;
    return; // loadedmetadata calls back
  }
  if (cam.readyState < 1) return;
  const diff = cam.currentTime - want;
  if (playing) {
    const now = performance.now();
    if (!cam.seeking && Math.abs(diff) > 0.08 && now - P.lastCamSeek > 600) {
      // Seeking large (4K) video takes a moment: land slightly ahead, then fine-tune with the playback rate.
      cam.currentTime = want + 0.1;
      P.lastCamSeek = now;
    } else if (!cam.seeking) {
      cam.playbackRate = Math.abs(diff) > 0.015 ? 1 - Math.max(-0.2, Math.min(0.2, diff * 2)) : 1;
    }
    if (cam.paused) cam.play().catch(() => {});
  } else {
    if (!cam.paused) cam.pause();
    cam.playbackRate = 1;
    if (Math.abs(diff) > 0.004) cam.currentTime = want;
  }
}


/* ---------- step 4: cut ---------- */

const CUT = {
  wave: null,          // 話全体の音の大きさ
  waveFor: '',
  detail: null,        // 拡大したときの細かい波形 {t0, rate, values}
  detailKey: '',
  view: [0, 0],        // タイムラインに映している範囲(秒)
  cursor: 0,           // いま見ている位置(秒)
  mode: 'seek',        // 'seek'(見る) / 'cut'(区切る) / 'trim'(削る)
  drag: null,          // 削る範囲をドラッグ中 {a, b}
  snap: null,          // 区切りを動かす前の状態(元に戻す用)
  skip: true,          // 再生のとき、削る範囲を飛ばすか
  touched: 'cut',      // 最後に触ったもの('cut' = 区切り / 'trim' = 削る範囲)
  pan: false,          // タイムラインを左右に動かしている最中か
  listsOpen: false,    // 「区切りと削る範囲の一覧」を開いているか
  hover: '',           // マウスが端に乗っているか(カーソルの形を変える)
  playing: false,
  audio: null,         // ラジオ音声(プレビューの再生に使う)
  audioFor: '',
  canvas: null,
  mini: null,
  bg: null,            // 背景を描いておく画(再生中に描き直さないため)
  bgKey: '',
  miniKey: '',
  preview: null,
  previewSrc: '',
  raf: 0,
  loop: 0,
  syncedAt: 0,
  wantAt: null,        // 行きたい位置(前の移動が終わるのを待つ)
  seekHooked: false,
};

const CUT_FPS = 30;

async function enterCut() {
  S.cutEp = Math.max(0, Math.min(S.cutEp, S.project.episodes.length - 1));
  if (!S.project.episodes.every((e) => Array.isArray(e.cuts))) {
    try {
      const { suggestions } = await api('/api/cut/suggest', { project: S.project });
      commit(() => S.project.episodes.forEach((e, i) => {
        if (!Array.isArray(e.cuts)) e.cuts = (suggestions[i] || []).slice();
      }));
    } catch (e) {
      S.project.episodes.forEach((ep) => { if (!Array.isArray(ep.cuts)) ep.cuts = []; });
      toast(e.message, 'bad');
    }
  }
  if (S.project.episodes.some((e) => !Array.isArray(e.trims) || e.drop)) {
    commit(() => S.project.episodes.forEach(adoptTrims));
  }
  const ep = cutEpisode();
  if (ep) CUT.view = [0, ep.duration];
  CUT.cursor = 0;
  CUT.drag = null;
  CUT.snap = null;
  renderCut();
  loadCutWave();
  loadCutAudio();
}

function leaveCut() {
  cutPause();
}

function cutEpisode() {
  return S.project.episodes[S.cutEp];
}

/** 前の作りの「この本は書き出さない」を、削る範囲に読み替える */
function adoptTrims(ep) {
  const trims = Array.isArray(ep.trims) ? ep.trims.slice() : [];
  if (Array.isArray(ep.drop) && ep.drop.length) {
    const points = [0, ...(ep.cuts || []).slice().sort((a, b) => a - b), ep.duration];
    for (const k of ep.drop) {
      if (k >= 0 && k < points.length - 1) trims.push({ start: points[k], end: points[k + 1] });
    }
  }
  delete ep.drop;
  ep.trims = normalizeTrims(trims, ep.duration);
}

/* --- 素材の読み込み --- */

async function loadCutWave() {
  const ep = cutEpisode();
  if (!ep || CUT.waveFor === ep.audio) return;
  CUT.wave = null;
  CUT.waveFor = ep.audio;
  drawCut(true);
  try {
    const r = await api('/api/overview', { path: ep.audio, points: 900 });
    if (CUT.waveFor === ep.audio) CUT.wave = r.values;
  } catch (e) {
    CUT.wave = [];
  }
  drawCut(true);
}

/** 拡大したときは、その範囲の細かい波形を取りに行く(2分以内のときだけ) */
async function loadCutDetail() {
  const ep = cutEpisode();
  const [a, b] = CUT.view;
  if (!ep || b - a > 120) {
    if (CUT.detail) { CUT.detail = null; CUT.detailKey = ''; drawCut(true); }
    return;
  }
  const start = Math.max(0, Math.floor(a / 10) * 10 - 5);
  const duration = Math.min(120, Math.ceil(b - start) + 10);
  const key = `${ep.audio}|${start}|${duration}`;
  if (CUT.detailKey === key) return;
  CUT.detailKey = key;
  try {
    const r = await api('/api/envelope', { path: ep.audio, start, duration });
    if (CUT.detailKey === key) CUT.detail = r;
  } catch (e) {
    CUT.detail = null;
  }
  drawCut(true);
}

function loadCutAudio() {
  const ep = cutEpisode();
  if (!ep) return;
  if (!CUT.audio) {
    CUT.audio = new Audio();
    CUT.audio.preload = 'auto';
    CUT.audio.addEventListener('ended', () => cutPause());
  }
  if (CUT.audioFor !== ep.audio) {
    const url = playableUrl(ep.audio);
    if (!url) { setTimeout(loadCutAudio, 1000); return; }
    CUT.audioFor = ep.audio;
    CUT.audio.src = url;
  }
}

/* --- 削る範囲(トリム) --- */

const round3 = (t) => Math.round(t * 1000) / 1000;

/** 重なりをまとめて時間順に整える */
function normalizeTrims(list, duration) {
  const items = (list || [])
    .map((t) => ({ start: Math.max(0, +t.start || 0), end: Math.min(+t.end || 0, duration) }))
    .filter((t) => t.end - t.start > 0.05)
    .sort((a, b) => a.start - b.start);
  const out = [];
  for (const t of items) {
    const last = out[out.length - 1];
    if (last && t.start <= last.end + 0.01) last.end = Math.max(last.end, round3(t.end));
    else out.push({ start: round3(t.start), end: round3(t.end) });
  }
  return out;
}

const trimsOf = (ep) => ep.trims || [];

/** 削ったあとに残る範囲 */
function keepRanges(ep) {
  const out = [];
  let cursor = 0;
  for (const t of trimsOf(ep)) {
    if (t.start - cursor > 0.05) out.push([cursor, t.start]);
    cursor = Math.max(cursor, t.end);
  }
  if (ep.duration - cursor > 0.05) out.push([cursor, ep.duration]);
  return out;
}

/** a〜b のうち、削らずに残る長さ */
function keptLength(ep, a = 0, b = ep.duration) {
  return keepRanges(ep).reduce((sum, [x, y]) => sum + Math.max(0, Math.min(b, y) - Math.max(a, x)), 0);
}

const inTrim = (ep, t) => trimsOf(ep).find((x) => t >= x.start - 1e-6 && t < x.end);

function setTrims(list, note = '') {
  const ep = cutEpisode();
  commit(() => { ep.trims = normalizeTrims(list, ep.duration); });
  if (note) toast(note + '(Ctrl+Z で元に戻せます)');
  renderCut();
}

function addTrim(a, b) {
  const ep = cutEpisode();
  const start = Math.max(0, Math.min(a, b)), end = Math.min(ep.duration, Math.max(a, b));
  if (end - start < 0.2) return;
  setTrims([...trimsOf(ep), { start, end }],
           `${fmt(start, false)} 〜 ${fmt(end, false)}(${fmtDuration(end - start)})を削ります`);
  const made = trimsOf(ep).find((t) => t.start <= start + 0.01 && start <= t.end);
  if (made) { S.trimAt = made.start; CUT.touched = 'trim'; renderCut(); }
}

function removeTrim(trim) {
  const ep = cutEpisode();
  setTrims(trimsOf(ep).filter((t) => t !== trim && !(Math.abs(t.start - trim.start) < 1e-6 && Math.abs(t.end - trim.end) < 1e-6)),
           '削るのをやめました');
}

/* --- 削る範囲を選ぶ・動かす --- */

/** その時刻の近くにある「削る範囲の端」 */
function trimEdgeAt(ep, t, grab) {
  let best = null;
  for (const trim of trimsOf(ep)) {
    for (const side of ['start', 'end']) {
      const d = Math.abs(trim[side] - t);
      if (d < grab && (!best || d < best.d)) best = { trim, side, d };
    }
  }
  return best;
}

const currentTrim = (ep) => trimsOf(ep).find((t) => Math.abs(t.start - S.trimAt) < 1e-6);

function selectTrim(trim) {
  S.trimAt = trim ? trim.start : -1;
  CUT.touched = 'trim';
  CUT.bgKey = '';
}

function selectTrimStep(dir) {
  const ep = cutEpisode();
  const list = trimsOf(ep);
  if (!list.length) return;
  const i = list.findIndex((t) => Math.abs(t.start - S.trimAt) < 1e-6);
  const next = list[Math.max(0, Math.min((i < 0 ? 0 : i + dir), list.length - 1))];
  selectTrim(next);
  seekCut(Math.max(0, next.start - 1));
  ensureVisible(next.start);
  renderCut();
}

/** 端をつかんで伸ばし縮みさせている間(終わってから「元に戻す」に積む) */
function dragTrimEdge(trim, side, to) {
  const ep = cutEpisode();
  if (!trim) return;
  if (!CUT.snap) CUT.snap = snapshot();
  const at = Math.round(Math.max(0, Math.min(to, ep.duration)) * CUT_FPS) / CUT_FPS;
  if (side === 'start') trim.start = Math.min(at, trim.end - 0.2);
  else trim.end = Math.max(at, trim.start + 0.2);
  S.trimAt = trim.start;
  CUT.touched = 'trim';
  CUT.cursor = side === 'start' ? trim.start : trim.end;
  CUT.bgKey = '';
  refreshCutLive();
}

function endTrimDrag() {
  const ep = cutEpisode();
  const before = CUT.snap;
  CUT.snap = null;
  const at = S.trimAt;
  ep.trims = normalizeTrims(trimsOf(ep), ep.duration);
  const still = trimsOf(ep).find((t) => t.start <= at + 0.01 && at <= t.end + 0.01);
  S.trimAt = still ? still.start : (trimsOf(ep)[0]?.start ?? -1);
  if (before) pushUndo(before);
  renderCut();
}

/** ボタンでの微調整 */
function nudgeTrim(side, delta) {
  const ep = cutEpisode();
  const trim = currentTrim(ep);
  if (!trim) return;
  dragTrimEdge(trim, side, (side === 'start' ? trim.start : trim.end) + delta);
  ensureVisible(side === 'start' ? trim.start : trim.end);
  endTrimDrag();
}

function removeSelectedTrim() {
  const ep = cutEpisode();
  const trim = currentTrim(ep);
  if (trim) removeTrim(trim);
}

/** 表の「このカットを削る / 元に戻す」 */
function togglePartTrim(part) {
  const ep = cutEpisode();
  if (part.out < 0.2) {
    setTrims(trimsOf(ep).filter((t) => t.end <= part.start + 0.01 || t.start >= part.end - 0.01),
             'このカットを元に戻しました');
    return;
  }
  addTrim(part.start, part.end);
}

/* --- カット(区切られた範囲) --- */

function cutParts(ep) {
  // 削る範囲の中にある区切りは、もう意味がないので境目に数えない
  const points = [0, ...(ep.cuts || []).filter((t) => !inTrim(ep, t)).sort((a, b) => a - b), ep.duration];
  const parts = [];
  let no = 0;                                     // 書き出されるものだけに通し番号を振る
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i], end = points[i + 1];
    const out = keptLength(ep, start, end);
    parts.push({ start, end, index: i, out, no: out >= 0.2 ? ++no : 0 });
  }
  return parts;
}

function partAt(ep, t) {
  const parts = cutParts(ep);
  return parts.find((p) => t >= p.start && t < p.end) || parts[parts.length - 1];
}

/** 映像がない(静止画になる)ひとまとまり */
function blankRanges(ep) {
  const out = [];
  for (const s of ep.segments) {
    const blank = !(s.enabled && s.video);
    if (!blank) continue;
    const last = out[out.length - 1];
    if (last && Math.abs(last.end - s.start) < 0.001) last.end = s.end;
    else out.push({ start: s.start, end: s.end });
  }
  return out;
}

/* --- 拡大・移動 --- */

function setView(a, b) {
  const ep = cutEpisode();
  const min = 2;                                  // 2秒まで拡大できる
  let span = Math.max(min, Math.min(b - a, ep.duration));
  let start = Math.max(0, Math.min(a, ep.duration - span));
  CUT.view = [start, start + span];
  loadCutDetail();
  refreshCutLive();
}

function zoomCut(factor, center) {
  const ep = cutEpisode();
  const [a, b] = CUT.view;
  const at = center ?? (a + b) / 2;
  const span = Math.max(2, Math.min((b - a) * factor, ep.duration));
  setView(at - (at - a) * (span / (b - a)), at - (at - a) * (span / (b - a)) + span);
}

function ensureVisible(t) {
  const [a, b] = CUT.view;
  const span = b - a;
  if (t < a + span * 0.05 || t > b - span * 0.05) setView(t - span / 2, t + span / 2);
}

/* --- 再生 --- */

function cutPlay() {
  const ep = cutEpisode();
  if (!ep || !CUT.audio || !CUT.audioFor) return;
  if (CUT.skip) {
    const here = inTrim(ep, CUT.cursor);
    if (here) CUT.cursor = here.end;
  }
  CUT.audio.currentTime = Math.min(CUT.cursor, ep.duration - 0.05);
  CUT.audio.play().catch(() => {});
  CUT.playing = true;
  CUT.syncedAt = 0;
  updateCutPreview(true);
  const tick = () => {
    if (!CUT.playing) return;
    CUT.cursor = CUT.audio.currentTime;
    if (CUT.skip) {
      const here = inTrim(ep, CUT.cursor);
      if (here) {                                   // 削る所は飛ばす
        if (here.end >= ep.duration - 0.05) { cutPause(); CUT.cursor = here.start; refreshCutLive(); return; }
        CUT.cursor = here.end;
        CUT.audio.currentTime = here.end;
      }
    }
    if (CUT.cursor >= ep.duration) { cutPause(); return; }
    ensureVisible(CUT.cursor);
    drawCut();
    movePlayBar('cutBar', CUT.cursor, ep.duration);
    const now = performance.now();
    if (now - CUT.syncedAt > 150) {                 // 映像の追い込みは 0.15 秒おきで十分
      CUT.syncedAt = now;
      updateCutPreview(true);
    }
    CUT.loop = requestAnimationFrame(tick);
  };
  CUT.loop = requestAnimationFrame(tick);
  updateCutButtons();
}

function cutPause() {
  CUT.playing = false;
  cancelAnimationFrame(CUT.loop);
  CUT.audio?.pause();
  CUT.preview?.querySelector('video')?.pause();
  updateCutButtons();
}

function cutTogglePlay() {
  CUT.playing ? cutPause() : cutPlay();
}

function seekCut(t) {
  const ep = cutEpisode();
  CUT.cursor = Math.max(0, Math.min(t, ep.duration - 0.02));
  if (CUT.audio && CUT.audioFor) CUT.audio.currentTime = CUT.cursor;
  refreshCutLive();
}

function updateCutButtons() {
  const b = $('cutPlay');
  if (b) b.textContent = CUT.playing ? '‖ 一時停止' : '▶ 再生';
}

/** いま選んでいるもの(区切り or 削る範囲)の「×」の位置。出さないときは null */
function closeBox(ep, kind, width) {
  if (!width) return null;
  const sel = currentTrim(ep);
  const showTrim = CUT.touched === 'trim' && sel;
  if (kind === 'trim' ? !showTrim : !!showTrim) return null;
  const [v0, v1] = CUT.view;
  const x = (t) => (t - v0) / (v1 - v0) * width;
  if (kind === 'cut') {
    if (!(ep.cuts || []).some((t) => Math.abs(t - S.cutAt) < 1e-6)) return null;
    const px = x(S.cutAt);
    return px < -10 || px > width + 10 ? null : { x: px, half: 8 };
  }
  const x0 = Math.max(0, x(sel.start)), x1 = Math.min(width, x(sel.end));
  return x1 - x0 < 26 ? null : { x: (x0 + x1) / 2, half: 10 };
}

const closeBoxOf = (ep, kind, canvas) => closeBox(ep, kind, canvas.clientWidth);

/** タイムラインの「×」を押したか */
function hitClose(e, ep, canvas) {
  if (e.offsetY > 20) return '';
  for (const kind of ['cut', 'trim']) {
    const box = closeBoxOf(ep, kind, canvas);
    if (box && Math.abs(e.offsetX - box.x) <= box.half) return kind;
  }
  return '';
}

/* --- 描画 --- */

function drawCut(reset) {
  if (reset) CUT.bgKey = '';
  drawCutMain();
  drawCutMini();
}

/** 斜線で塗る(削る範囲の印) */
function hatch(g, x0, x1, height, color) {
  if (x1 <= x0) return;
  g.save();
  g.beginPath();
  g.rect(x0, 0, x1 - x0, height);
  g.clip();
  g.strokeStyle = color;
  g.lineWidth = 1.5;
  for (let px = x0 - height; px < x1; px += 9) {
    g.beginPath();
    g.moveTo(px, height);
    g.lineTo(px + height, 0);
    g.stroke();
  }
  g.restore();
}

/** 背景(波形・帯・削る範囲・区切り)。中身が変わったときだけ描き直す */
function paintCutBase(g, ep, width, height) {
  g.clearRect(0, 0, width, height);
  const [v0, v1] = CUT.view;
  const span = v1 - v0;
  const x = (t) => (t - v0) / span * width;
  const clampX = (t) => Math.max(0, Math.min(width, x(t)));
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue('--accent').trim() || '#2563eb';
  const cam = css.getPropertyValue('--cam').trim() || '#ef7d32';
  const parts = cutParts(ep);

  // カットの下地
  for (const part of parts) {
    if (part.end < v0 || part.start > v1) continue;
    g.fillStyle = part.index % 2 ? 'rgba(37, 99, 235, .09)' : 'rgba(37, 99, 235, .04)';
    g.fillRect(x(part.start), 0, x(part.end) - x(part.start), height);
  }

  // 目盛り
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find((s) => span / s < 12) || 900;
  g.strokeStyle = 'rgba(16, 24, 40, .10)';
  g.fillStyle = '#7b8798';
  g.font = '11px system-ui';
  g.lineWidth = 1;
  for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) {
    g.beginPath();
    g.moveTo(x(t), 18);
    g.lineTo(x(t), height - 20);
    g.stroke();
    g.fillText(fmt(t, false), x(t) + 3, height - 24);
  }

  // 映像のある所・ない所
  const bandTop = height - 16, bandHeight = 12;
  for (const s of ep.segments) {
    if (s.end < v0 || s.start > v1) continue;
    g.fillStyle = (s.enabled && s.video) ? cam : '#c7ced9';
    g.fillRect(x(s.start), bandTop, Math.max(1, x(s.end) - x(s.start)), bandHeight);
  }

  // 波形(拡大時は細かい方を使う)
  const waveTop = 20, waveHeight = bandTop - 28;
  g.fillStyle = 'rgba(37, 99, 235, .55)';
  const detail = CUT.detail;
  if (detail && detail.values.length && span <= 120) {
    const rate = detail.rate;
    const px = width / span;
    const stepIdx = Math.max(1, Math.floor(1 / (px / rate)));
    for (let i = 0; i < detail.values.length; i += stepIdx) {
      const t = detail.t0 + i / rate;
      if (t < v0 - 1 || t > v1 + 1) continue;
      const v = Math.max(...detail.values.slice(i, i + stepIdx));
      const h = Math.max(1, v * waveHeight);
      g.fillRect(x(t), waveTop + (waveHeight - h) / 2, Math.max(1, px / rate * stepIdx), h);
    }
  } else if (CUT.wave && CUT.wave.length) {
    const n = CUT.wave.length;
    for (let i = 0; i < n; i++) {
      const t0 = i / n * ep.duration, t1 = (i + 1) / n * ep.duration;
      if (t1 < v0 || t0 > v1) continue;
      const h = Math.max(1, CUT.wave[i] * waveHeight);
      g.fillRect(x(t0), waveTop + (waveHeight - h) / 2, Math.max(1, x(t1) - x(t0)), h);
    }
  } else {
    g.fillStyle = '#9aa4b4';
    g.fillText('音の波形を読み込んでいます…', 10, waveTop + waveHeight / 2);
  }

  // 削る範囲(赤い網掛け)。端のつまみをドラッグで伸ばし縮みできる
  for (const t of trimsOf(ep)) {
    if (t.end < v0 || t.start > v1) continue;
    const on = Math.abs(t.start - S.trimAt) < 1e-6;
    const x0 = clampX(t.start), x1 = clampX(t.end);
    g.fillStyle = on ? 'rgba(209, 67, 67, .20)' : 'rgba(209, 67, 67, .13)';
    g.fillRect(x0, 0, x1 - x0, height);
    hatch(g, x0, x1, height, on ? 'rgba(209, 67, 67, .50)' : 'rgba(209, 67, 67, .38)');
    g.strokeStyle = on ? '#d14343' : 'rgba(209, 67, 67, .75)';
    g.lineWidth = on ? 2.5 : 1.5;
    g.beginPath();
    g.moveTo(x(t.start), 0); g.lineTo(x(t.start), height);
    g.moveTo(x(t.end), 0); g.lineTo(x(t.end), height);
    g.stroke();
    for (const px of [x(t.start), x(t.end)]) {     // 端のつまみ
      if (px < -10 || px > width + 10) continue;
      g.fillStyle = on ? '#d14343' : 'rgba(209, 67, 67, .6)';
      g.beginPath();
      g.roundRect(px - 4.5, height / 2 - 15, 9, 30, 3);
      g.fill();
      g.strokeStyle = 'rgba(255, 255, 255, .85)';
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(px, height / 2 - 8); g.lineTo(px, height / 2 + 8);
      g.stroke();
    }
    if (x1 - x0 > 46) {
      g.fillStyle = '#b23b3b';
      g.font = 'bold 11px system-ui';
      g.fillText(`削る ${fmtDuration(t.end - t.start)}`, x0 + 5, 30);
    }
  }

  // カット番号
  g.font = 'bold 12px system-ui';
  for (const part of parts) {
    if (part.end < v0 || part.start > v1) continue;
    g.fillStyle = part.no ? '#516074' : '#a33';
    g.fillText(part.no ? `カット${part.no}` : 'まるごと削除', Math.max(4, x(part.start) + 6), 14);
  }

  // 区切り(削る範囲の中にあるものは効かないので灰色)
  for (const t of (ep.cuts || [])) {
    if (t < v0 || t > v1) continue;
    const px = x(t);
    const dead = !!inTrim(ep, t);
    const on = Math.abs(t - S.cutAt) < 1e-6;
    g.strokeStyle = dead ? '#9aa4b4' : accent;
    g.lineWidth = on ? 3 : 2;
    g.beginPath();
    g.moveTo(px, 0);
    g.lineTo(px, height);
    g.stroke();
    g.fillStyle = dead ? '#9aa4b4' : accent;
    g.lineWidth = 2;
    g.beginPath();
    g.roundRect(px - 7, 2, 14, 14, 3);
    g.fill();
  }

  // 選んでいるものには「×」(押すと消せる)
  for (const kind of ['cut', 'trim']) {
    const box = closeBox(ep, kind, width);
    if (!box) continue;
    g.fillStyle = kind === 'trim' ? '#d14343' : accent;
    g.beginPath();
    g.roundRect(box.x - box.half, 1, box.half * 2, 17, 4);
    g.fill();
    g.strokeStyle = '#fff';
    g.lineWidth = 1.9;
    g.beginPath();
    g.moveTo(box.x - 3.6, 5.8); g.lineTo(box.x + 3.6, 13.2);
    g.moveTo(box.x + 3.6, 5.8); g.lineTo(box.x - 3.6, 13.2);
    g.stroke();
  }
}

function drawCutMain() {
  const canvas = CUT.canvas;
  const ep = cutEpisode();
  if (!canvas || !ep) return;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    CUT.bgKey = '';
  }
  const key = [width, height, dpr, CUT.view[0].toFixed(3), CUT.view[1].toFixed(3), S.cutAt, S.trimAt,
               (ep.cuts || []).join(','), trimsOf(ep).map((t) => `${t.start}-${t.end}`).join(','),
               CUT.wave ? CUT.wave.length : -1, CUT.detail ? CUT.detailKey : ''].join('|');
  if (!CUT.bg) CUT.bg = document.createElement('canvas');
  if (CUT.bgKey !== key) {
    CUT.bgKey = key;
    CUT.bg.width = canvas.width;
    CUT.bg.height = canvas.height;
    const bg = CUT.bg.getContext('2d');
    bg.setTransform(dpr, 0, 0, dpr, 0, 0);
    paintCutBase(bg, ep, width, height);
  }
  const g = canvas.getContext('2d');
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, canvas.width, canvas.height);
  g.drawImage(CUT.bg, 0, 0);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);

  const [v0, v1] = CUT.view;
  const x = (t) => (t - v0) / (v1 - v0) * width;

  // ドラッグ中の範囲
  if (CUT.drag) {
    const a = Math.min(CUT.drag.a, CUT.drag.b), b = Math.max(CUT.drag.a, CUT.drag.b);
    const x0 = Math.max(0, Math.min(width, x(a))), x1 = Math.max(0, Math.min(width, x(b)));
    g.fillStyle = 'rgba(209, 67, 67, .22)';
    g.fillRect(x0, 0, x1 - x0, height);
    g.strokeStyle = '#d14343';
    g.lineWidth = 2;
    g.strokeRect(x0, 1, Math.max(1, x1 - x0), height - 2);
    g.fillStyle = '#b23b3b';
    g.font = 'bold 12px system-ui';
    g.fillText(fmtDuration(b - a), x0 + 6, height / 2);
  }

  // 再生位置
  if (CUT.cursor >= v0 && CUT.cursor <= v1) {
    const cx = x(CUT.cursor);
    g.strokeStyle = '#111827';
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(cx, 0);
    g.lineTo(cx, height);
    g.stroke();
    g.fillStyle = '#111827';
    g.beginPath();
    g.moveTo(cx - 5, 0);
    g.lineTo(cx + 5, 0);
    g.lineTo(cx, 7);
    g.closePath();
    g.fill();
  }
}

/** 全体の見取り図(いまどこを映しているか) */
function drawCutMini() {
  const canvas = CUT.mini;
  const ep = cutEpisode();
  if (!canvas || !ep) return;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    CUT.miniKey = '';
  }
  const key = [width, height, CUT.view[0].toFixed(2), CUT.view[1].toFixed(2), (ep.cuts || []).join(','),
               trimsOf(ep).map((t) => `${t.start}-${t.end}`).join(','), CUT.wave ? CUT.wave.length : -1].join('|');
  if (CUT.miniKey === key) return;                 // 変わっていなければ描き直さない
  CUT.miniKey = key;
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, width, height);
  const x = (t) => t / ep.duration * width;
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue('--accent').trim() || '#2563eb';
  for (const s of ep.segments) {
    g.fillStyle = (s.enabled && s.video) ? css.getPropertyValue('--cam').trim() || '#ef7d32' : '#c7ced9';
    g.fillRect(x(s.start), height - 8, Math.max(1, x(s.end) - x(s.start)), 8);
  }
  if (CUT.wave && CUT.wave.length) {
    g.fillStyle = 'rgba(37, 99, 235, .45)';
    const n = CUT.wave.length;
    const step = width / n;
    for (let i = 0; i < n; i++) {
      const h = Math.max(1, CUT.wave[i] * (height - 12));
      g.fillRect(i * step, (height - 10 - h) / 2 + 2, Math.max(1, step), h);
    }
  }
  for (const t of trimsOf(ep)) {
    g.fillStyle = 'rgba(209, 67, 67, .30)';
    g.fillRect(x(t.start), 0, Math.max(1, x(t.end) - x(t.start)), height);
  }
  for (const t of (ep.cuts || [])) {
    g.strokeStyle = inTrim(ep, t) ? '#9aa4b4' : accent;
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(x(t), 0);
    g.lineTo(x(t), height);
    g.stroke();
  }
  // 映している範囲
  g.strokeStyle = '#111827';
  g.lineWidth = 2;
  g.strokeRect(x(CUT.view[0]) + 1, 1, Math.max(3, x(CUT.view[1]) - x(CUT.view[0]) - 2), height - 2);
  g.fillStyle = 'rgba(17, 24, 39, .06)';
  g.fillRect(x(CUT.view[0]), 0, Math.max(3, x(CUT.view[1]) - x(CUT.view[0])), height);
}

/* --- プレビュー --- */

function updateCutPreview(playing) {
  const ep = cutEpisode();
  const box = CUT.preview;
  if (!ep || !box) return;
  const time = Math.max(0, Math.min(CUT.cursor, ep.duration - 0.05));
  const seg = ep.segments[segAt(ep, time)];
  const label = $('cutPreviewLabel');
  const video = box.querySelector('video');
  const img = box.querySelector('img');
  const note = box.querySelector('.wait');
  const part = partAt(ep, time);
  const trim = inTrim(ep, time);
  box.classList.toggle('dropped', !!trim);
  if (label) {
    const where = trim ? '削る所' : (part.no ? `カット${part.no}` : '書き出しません');
    label.textContent = `${fmt(time, false)} / ${fmtDuration(ep.duration)} ・ ${where}`
      + (seg && seg.enabled && seg.video ? ` ・ ${basename(seg.video)}` : ' ・ 静止画');
  }
  if (seg && seg.enabled && seg.video) {
    const url = playableUrl(seg.video);
    if (!url) {
      video.hidden = img.hidden = true;
      note.hidden = false;
      note.textContent = mediaWaitMessage(seg.video);
      return;
    }
    img.hidden = true;
    note.hidden = true;
    video.hidden = false;
    video.muted = true;
    video.playbackRate = 1;
    const want = seg.source + (time - seg.start);
    if (CUT.previewSrc !== url) {
      CUT.previewSrc = url;
      video.src = url;
      video.addEventListener('loadedmetadata', () => {
        video.currentTime = want;
        if (CUT.playing) video.play().catch(() => {});
      }, { once: true });
      return;
    }
    if (video.readyState < 1) return;
    const diff = video.currentTime - want;
    if (playing) {
      // 再生中は動画自身に任せ、ずれが目に見えるときだけ合わせ直す
      if (!video.seeking && Math.abs(diff) > 0.4) video.currentTime = want;
      if (video.paused) video.play().catch(() => {});
    } else {
      if (!video.paused) video.pause();
      if (Math.abs(diff) <= 0.04) return;
      if (video.seeking) {                 // 前の移動が終わってから、最後の位置へ飛ぶ
        CUT.wantAt = want;
        if (!CUT.seekHooked) {
          CUT.seekHooked = true;
          video.addEventListener('seeked', () => {
            CUT.seekHooked = false;
            const at = CUT.wantAt;
            CUT.wantAt = null;
            if (at != null && Math.abs(video.currentTime - at) > 0.04) video.currentTime = at;
          }, { once: true });
        }
        return;
      }
      video.currentTime = want;
    }
  } else {
    const still = seg ? stillOf(seg) : '';
    video.hidden = true;
    if (!video.paused) video.pause();
    if (still) {
      img.hidden = false;
      note.hidden = true;
      if (img.dataset.src !== still) {
        img.dataset.src = still;
        img.src = mediaUrl(still);
      }
    } else {
      img.hidden = true;
      note.hidden = false;
      note.textContent = 'ここは静止画になります(画像は未設定)';
    }
  }
}

function refreshCutLive() {
  if (CUT.raf) return;
  CUT.raf = requestAnimationFrame(() => {
    CUT.raf = 0;
    drawCut();
    updateCutPreview(CUT.playing);                 // 再生中は止めない
    const ep = cutEpisode();
    const clock = $('cutClock');
    if (clock) clock.textContent = (ep.cuts || []).length ? fmt(S.cutAt, false) : '—';
    const sel = currentTrim(ep);
    if (sel) {
      const a = $('trimStart'), b = $('trimEnd'), len = $('trimLen');
      if (a) a.textContent = fmt(sel.start, false);
      if (b) b.textContent = fmt(sel.end, false);
      if (len) len.textContent = `長さ ${fmtDuration(sel.end - sel.start)}`;
    }
    const zoom = $('cutZoomLabel');
    if (zoom) zoom.textContent = `表示 ${fmtDuration(CUT.view[1] - CUT.view[0])}`;
    movePlayBar('cutBar', CUT.cursor, ep.duration);
  });
}

/* --- 区切りの操作 --- */

/** 正規化して入れるだけ(元に戻す用の記録はしない) */
function applyCuts(list) {
  const ep = cutEpisode();
  ep.cuts = [...new Set(list.map((t) => Math.round(t * 1000) / 1000)
    .filter((t) => t > 0.2 && t < ep.duration - 0.2))].sort((a, b) => a - b);
  return ep.cuts;
}

function setCuts(list, note = '') {
  const ep = cutEpisode();
  commit(() => { applyCuts(list); });
  if (note) toast(note + '(Ctrl+Z で元に戻せます)');
  renderCut();
}

function addCut(t) {
  const ep = cutEpisode();
  const at = Math.round(t * CUT_FPS) / CUT_FPS;
  if ((ep.cuts || []).some((x) => Math.abs(x - at) < 0.5)) return;
  S.cutAt = at;
  CUT.touched = 'cut';
  setCuts([...(ep.cuts || []), at], `${fmt(at, false)} で区切りました`);
}

/** ドラッグやスライダーで動かしている間(終わってから「元に戻す」に積む) */
function dragCutTo(from, to) {
  const ep = cutEpisode();
  const cuts = (ep.cuts || []).slice().sort((a, b) => a - b);
  const i = cuts.findIndex((t) => Math.abs(t - from) < 1e-6);
  if (i < 0) return from;
  if (!CUT.snap) CUT.snap = snapshot();
  const lower = (cuts[i - 1] ?? 0) + 0.5;
  const upper = (cuts[i + 1] ?? ep.duration) - 0.5;
  const next = Math.round(Math.max(lower, Math.min(upper, to)) * CUT_FPS) / CUT_FPS;
  cuts[i] = next;
  ep.cuts = cuts;
  S.cutAt = next;
  CUT.touched = 'cut';
  CUT.cursor = next;
  CUT.bgKey = '';
  refreshCutLive();
  return next;
}

function endCutDrag() {
  const ep = cutEpisode();
  const before = CUT.snap;
  CUT.snap = null;
  applyCuts(ep.cuts);
  if (before) pushUndo(before);
  renderCut();
}

function selectCut(dir) {
  const ep = cutEpisode();
  const cuts = (ep.cuts || []).slice().sort((a, b) => a - b);
  if (!cuts.length) return;
  const i = cuts.findIndex((t) => Math.abs(t - S.cutAt) < 1e-6);
  const next = cuts[Math.max(0, Math.min((i < 0 ? 0 : i + dir), cuts.length - 1))];
  S.cutAt = next;
  CUT.touched = 'cut';
  seekCut(next);
  ensureVisible(next);
  renderCut();
}

function removeCutAt(t) {
  const ep = cutEpisode();
  if (!(ep.cuts || []).length) return;
  setCuts((ep.cuts || []).filter((x) => Math.abs(x - t) > 1e-6), `${fmt(t, false)} の区切りを消しました`);
}

function deleteSelectedCut() {
  removeCutAt(S.cutAt);
}

function jumpBlank(dir) {
  const ep = cutEpisode();
  const blanks = blankRanges(ep);
  const next = dir > 0
    ? blanks.find((b) => b.start > CUT.cursor + 0.2)
    : [...blanks].reverse().find((b) => b.start < CUT.cursor - 0.2);
  if (!next) return toast(dir > 0 ? 'これより後に映像のない所はありません' : 'これより前に映像のない所はありません');
  seekCut(next.start);
  setView(next.start - (CUT.view[1] - CUT.view[0]) / 2, next.start + (CUT.view[1] - CUT.view[0]) / 2);
  toast(`映像のない所:${fmt(next.start, false)} 〜 ${fmt(next.end, false)}(${fmtDuration(next.end - next.start)})`);
}

/** プレビューの軽さを切り替える(軽い=360p の控え、重い=元の画質) */
function setLightPreview(on) {
  S.light = on;
  try { localStorage.setItem('radio-sync-light', on ? 'on' : 'off'); } catch { /* 使えなくても動く */ }
  if ($('chkLight')) $('chkLight').checked = on;
  CUT.previewSrc = '';
  P.camUrl = '';
  renderAll();
  syncPlayer(isPlaying());
  toast(on ? 'プレビューを軽くしました(つまみ送りが速くなります)' : '元の画質でプレビューします');
}

function setCutMode(mode) {
  CUT.mode = CUT.mode === mode ? 'seek' : mode;
  CUT.drag = null;
  renderCut();
}

function cutKey(e) {
  if (S.step !== 'cut') return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const ep = cutEpisode();
  if (!ep) return;
  const map = {
    ' ': () => cutTogglePlay(),
    s: () => addCut(CUT.cursor),
    S: () => addCut(CUT.cursor),
    d: () => togglePartTrim(partAt(ep, CUT.cursor)),
    D: () => togglePartTrim(partAt(ep, CUT.cursor)),
    Delete: () => (CUT.touched === 'trim' && currentTrim(ep) ? removeSelectedTrim() : deleteSelectedCut()),
    Backspace: () => (CUT.touched === 'trim' && currentTrim(ep) ? removeSelectedTrim() : deleteSelectedCut()),
    ArrowLeft: () => seekCut(CUT.cursor - (e.shiftKey ? 1 : 1 / CUT_FPS)),
    ArrowRight: () => seekCut(CUT.cursor + (e.shiftKey ? 1 : 1 / CUT_FPS)),
    '+': () => zoomCut(0.5, CUT.cursor),
    '-': () => zoomCut(2, CUT.cursor),
  };
  const fn = map[e.key];
  if (fn) {
    e.preventDefault();
    fn();
  }
}

/* --- 画面 --- */

/* ---------- スマホ用のカット画面 ----------
   スマホの動画編集アプリと同じ並び:上にプレビュー、真ん中にタイムライン、下に道具。
   1 画面に収めて、縦スクロールしないで編集できるようにする。 */

/** 下から出てくるパネル(一覧や設定を、画面を離れずに見る) */
function openSheet(title, ...body) {
  const modal = $('modal');
  const close = () => { modal.hidden = true; modal.replaceChildren(); modal.onclick = null; };
  modal.replaceChildren(h('div', { class: 'bottomsheet' },
    h('div', { class: 'sheet-grip' }),
    h('div', { class: 'spread' }, h('h3', {}, title), h('button', { class: 'ghost', onclick: close }, '閉じる')),
    h('div', { class: 'sheet-body' }, ...body)));
  modal.hidden = false;
  modal.onclick = (e) => { if (e.target === modal) close(); };
  return close;
}

/** いまある部品を下のパネルへ一時的に移し、閉じたら元の場所へ戻す */
function borrowInSheet(title, node) {
  const mark = document.createComment('borrowed');
  node.parentNode.insertBefore(mark, node);
  const back = () => {
    if (mark.parentNode) {
      mark.parentNode.insertBefore(node, mark);
      mark.remove();
    }
  };
  const modal = $('modal');
  const watch = new MutationObserver(() => {
    if (modal.hidden) {
      watch.disconnect();
      back();
    }
  });
  watch.observe(modal, { attributes: true, attributeFilter: ['hidden'] });
  openSheet(title, node);
}

function phoneTool(label, sub, on, fn, kind = '') {
  return h('button', { class: 'ptool ' + kind + (on ? ' on' : ''), onclick: fn },
    h('span', { class: 'pico' }, label), h('span', { class: 'plabel' }, sub));
}

/** 書き出すファイルと、区切り・削る範囲の一覧(下から出す) */
function openCutList(ep) {
  const cuts = (ep.cuts || []).slice().sort((a, b) => a - b);
  const trims = trimsOf(ep);
  const parts = cutParts(ep);
  const row = (left, right, onclick, danger) => h('div', { class: 'listrow', onclick },
    h('div', {}, left), h('div', { class: 'row' }, right,
      danger && h('button', { class: 'danger small', onclick: (e) => { e.stopPropagation(); danger(); } }, '消す')));
  openSheet('一覧',
    h('h4', {}, `書き出すファイル(${parts.filter((x) => x.out >= 0.2).length})`),
    ...parts.map((part) => {
      const dead = part.out < 0.2;
      return row(
        h('div', {}, h('b', {}, dead ? '—' : `カット${part.no}`),
          h('div', { class: 'muted small mono' }, `${fmt(part.start, false)} 〜 ${fmt(part.end, false)}`)),
        h('span', { class: 'mono ' + (dead ? 'muted' : '') }, dead ? 'まるごと削除' : fmtDuration(part.out)),
        () => { seekCut(part.start + 0.2); setView(part.start - 2, part.start + 8); renderCut(); });
    }),
    h('h4', {}, `区切り(${cuts.length})`),
    cuts.length ? cuts.map((t, i) => row(
      h('div', {}, h('b', {}, `${i + 1}つ目`), inTrim(ep, t) && h('span', { class: 'muted small' }, ' 削る範囲の中')),
      h('span', { class: 'mono' }, fmt(t, false)),
      () => { S.cutAt = t; CUT.touched = 'cut'; seekCut(t); ensureVisible(t); renderCut(); },
      () => removeCutAt(t))) : h('p', { class: 'muted small' }, 'まだ区切っていません。'),
    h('h4', {}, `削る範囲(${trims.length})`),
    trims.length ? trims.map((t, i) => row(
      h('div', {}, h('b', {}, `${i + 1}か所目`),
        h('div', { class: 'muted small mono' }, `${fmt(t.start, false)} 〜 ${fmt(t.end, false)}`)),
      h('span', { class: 'mono' }, fmtDuration(t.end - t.start)),
      () => { selectTrim(t); seekCut(Math.max(0, t.start - 1)); ensureVisible(t.start); renderCut(); },
      () => removeTrim(t))) : h('p', { class: 'muted small' }, 'いまは何も削っていません。'),
    h('div', { class: 'row', style: 'margin-top:12px' },
      cuts.length > 0 && h('button', { class: 'ghost', onclick: () => setCuts([], 'すべての区切りを消しました') }, 'すべての区切りを消す'),
      trims.length > 0 && h('button', { class: 'ghost', onclick: () => setTrims([], 'すべて元に戻しました') }, '削るのをすべてやめる')));
}

function renderCutPhone() {
  const ep = cutEpisode();
  const cuts = (ep.cuts || []).slice().sort((a, b) => a - b);
  const trims = trimsOf(ep);
  const parts = cutParts(ep);
  if (cuts.length && !cuts.some((t) => Math.abs(t - S.cutAt) < 1e-6)) S.cutAt = cuts[0];
  const kept = parts.filter((x) => x.out >= 0.2);
  const total = keptLength(ep);
  const removed = ep.duration - total;
  if (!CUT.view[1]) CUT.view = [0, ep.duration];
  CUT.bgKey = '';

  const { canvas } = makeCutTimeline(ep, cuts);
  const preview = h('div', { class: 'cutshot' },
    h('video', { muted: true, playsinline: true, preload: 'auto', disablePictureInPicture: true }),
    h('img', { alt: '', hidden: true }),
    h('div', { class: 'wait', hidden: true }));
  CUT.preview = preview;
  CUT.previewSrc = '';

  const sel = currentTrim(ep);
  const showTrim = CUT.touched === 'trim' && sel;
  const selNo = sel ? trims.findIndex((t) => Math.abs(t.start - sel.start) < 1e-6) + 1 : 0;
  const cutNo = cuts.length ? cuts.findIndex((t) => Math.abs(t - S.cutAt) < 1e-6) + 1 : 0;

  // いま選んでいるもの(区切り / 削る範囲)の細かい調整
  const nudge = (label, fn, disabled) => h('button', { class: 'icon', disabled, onclick: fn }, label);
  const strip = showTrim
    ? h('div', { class: 'pstrip trim' },
        h('div', { class: 'pstrip-head' },
          h('b', {}, `削る範囲 ${selNo}/${trims.length}`),
          h('span', { class: 'mono', id: 'trimStart' }, fmt(sel.start, false)),
          h('span', { class: 'muted' }, '〜'),
          h('span', { class: 'mono', id: 'trimEnd' }, fmt(sel.end, false)),
          h('span', { class: 'muted small', id: 'trimLen' }, fmtDuration(sel.end - sel.start)),
          h('span', { style: 'flex:1' }),
          h('button', { class: 'danger small', onclick: removeSelectedTrim }, '消す')),
        h('div', { class: 'pstrip-row' },
          h('span', { class: 'muted small' }, '始め'),
          nudge('−1', () => nudgeTrim('start', -1)), nudge('−0.1', () => nudgeTrim('start', -0.1)),
          nudge('+0.1', () => nudgeTrim('start', 0.1)), nudge('+1', () => nudgeTrim('start', 1)),
          h('span', { class: 'sep' }),
          h('span', { class: 'muted small' }, '終わり'),
          nudge('−1', () => nudgeTrim('end', -1)), nudge('−0.1', () => nudgeTrim('end', -0.1)),
          nudge('+0.1', () => nudgeTrim('end', 0.1)), nudge('+1', () => nudgeTrim('end', 1))))
    : h('div', { class: 'pstrip cut' },
        h('div', { class: 'pstrip-head' },
          h('b', {}, cuts.length ? `区切り ${cutNo}/${cuts.length}` : '区切りはまだありません'),
          h('span', { class: 'mono', id: 'cutClock' }, cuts.length ? fmt(S.cutAt, false) : '—'),
          h('span', { style: 'flex:1' }),
          CUT.mode === 'cut' && h('button', { class: 'primary small', onclick: () => addCut(CUT.cursor) }, 'ここで区切る'),
          CUT.mode === 'trim' && h('button', { class: 'small', onclick: () => togglePartTrim(partAt(ep, CUT.cursor)) }, 'このカットを削る'),
          h('button', { class: 'danger small', disabled: !cuts.length, onclick: deleteSelectedCut }, '消す')),
        h('div', { class: 'pstrip-row' },
          h('button', { class: 'icon', disabled: !cuts.length, onclick: () => selectCut(-1) }, '◀'),
          h('button', { class: 'icon', disabled: !cuts.length, onclick: () => selectCut(1) }, '▶'),
          h('span', { class: 'sep' }),
          nudge('−1', () => { dragCutTo(S.cutAt, S.cutAt - 1); ensureVisible(S.cutAt); endCutDrag(); }, !cuts.length),
          nudge('−0.1', () => { dragCutTo(S.cutAt, S.cutAt - 0.1); ensureVisible(S.cutAt); endCutDrag(); }, !cuts.length),
          nudge('+0.1', () => { dragCutTo(S.cutAt, S.cutAt + 0.1); ensureVisible(S.cutAt); endCutDrag(); }, !cuts.length),
          nudge('+1', () => { dragCutTo(S.cutAt, S.cutAt + 1); ensureVisible(S.cutAt); endCutDrag(); }, !cuts.length)));

  const hint = CUT.mode === 'trim' ? 'なぞった範囲が「削る所」になります'
    : CUT.mode === 'cut' ? '押した所で区切ります'
      : 'なぞって移動・2本指で拡大';

  $('view-cut').replaceChildren(h('div', { class: 'phoneview' },
    h('div', { class: 'ptop' },
      h('button', { class: 'ghost small', onclick: () => { cutPause(); setStep('review'); } }, '← 確認'),
      h('b', {}, `第${S.cutEp + 1}話`),
      h('span', { class: 'muted small' }, `${fmtDuration(total)} / カット${kept.length}`
        + (removed > 0.2 ? `(${fmtDuration(removed)}削除)` : '')),
      h('span', { style: 'flex:1' }),
      h('button', { class: 'primary small', disabled: !kept.length, onclick: () => { cutPause(); setStep('export'); } }, '書き出し →')),

    preview,

    h('div', { class: 'pplay' },
      playBar('cutBar', CUT.cursor, ep.duration, (t) => { seekCut(t); }, trims.map((t) => ({ ...t, kind: 'drop' })))),
    h('div', { class: 'pbtns' },
      h('button', { class: 'icon', onclick: () => seekCut(CUT.cursor - 5) }, '−5秒'),
      h('button', { class: 'primary big', id: 'cutPlay', onclick: cutTogglePlay }, CUT.playing ? '‖' : '▶'),
      h('button', { class: 'icon', onclick: () => seekCut(CUT.cursor + 5) }, '+5秒'),
      h('span', { style: 'flex:1' }),
      h('label', { class: 'check small' },
        h('input', { type: 'checkbox', checked: CUT.skip, onchange: (e) => { CUT.skip = e.target.checked; } }),
        '削る所を飛ばす')),

    h('div', { class: 'ptl' }, canvas,
      h('div', { class: 'ptlbar' },
        h('span', { class: 'muted small' }, hint),
        h('span', { style: 'flex:1' }),
        h('button', { class: 'icon', onclick: () => zoomCut(0.5, CUT.cursor) }, '＋'),
        h('button', { class: 'icon', onclick: () => zoomCut(2, CUT.cursor) }, '−'),
        h('button', { class: 'icon', onclick: () => setView(0, ep.duration) }, '全体'),
        h('span', { class: 'muted small', id: 'cutZoomLabel' }, fmtDuration(CUT.view[1] - CUT.view[0])))),

    strip,

    h('div', { class: 'ptools' },
      phoneTool('▶', '見る', CUT.mode === 'seek', () => setCutMode('seek')),
      phoneTool('│', '区切る', CUT.mode === 'cut', () => setCutMode('cut'), 'cut'),
      phoneTool('×', '削る', CUT.mode === 'trim', () => setCutMode('trim'), 'trim'),
      phoneTool('≡', '一覧', false, () => openCutList(ep)))));

  requestAnimationFrame(() => { measureChrome(); drawCut(true); updateCutPreview(CUT.playing); });
}

/** タイムラインの canvas を作って、指やマウスの操作をつなぐ(PC でもスマホでも同じ) */
function makeCutTimeline(ep, cuts) {
  const canvas = h('canvas', { class: 'cuttl', id: 'cutCanvas' });
  CUT.canvas = canvas;
  const mini = h('canvas', { class: 'cutmini', id: 'cutMini', hidden: isNarrow() });
  CUT.mini = mini;
  CUT.miniKey = '';

  const timeAt = (el, clientX) => {
    const rect = el.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return el === mini ? ratio * ep.duration : CUT.view[0] + ratio * (CUT.view[1] - CUT.view[0]);
  };
  const xOf = (t) => (t - CUT.view[0]) / (CUT.view[1] - CUT.view[0]) * canvas.clientWidth;

  const pinch = { at: new Map(), base: null };
  const pinchGap = () => { const [a, b] = [...pinch.at.values()]; return Math.max(8, Math.abs(a - b)); };
  const pinchMid = () => { const [a, b] = [...pinch.at.values()]; return (a + b) / 2; };
  const endPinch = (e) => {
    pinch.at.delete(e.pointerId);
    if (pinch.at.size < 2) pinch.base = null;
  };
  canvas.addEventListener('pointerup', endPinch);
  canvas.addEventListener('pointercancel', endPinch);

  canvas.addEventListener('pointermove', (e) => {
    if (pinch.at.has(e.pointerId)) pinch.at.set(e.pointerId, e.clientX);
    if (pinch.at.size === 2 && pinch.base) {         // つまんだ指の間隔で拡大・縮小
      const span = Math.max(2, Math.min(pinch.base.span * pinch.base.gap / pinchGap(), ep.duration));
      const ratio = (pinchMid() - canvas.getBoundingClientRect().left) / canvas.clientWidth;
      setView(pinch.base.center - span * ratio, pinch.base.center + span * (1 - ratio));
      return;
    }
    if (CUT.snap || CUT.drag || CUT.pan) return;     // 動かしている最中は変えない
    const t = timeAt(canvas, e.clientX);
    const grab = (CUT.view[1] - CUT.view[0]) * 0.012;
    const edge = trimEdgeAt(ep, t, grab);
    const onCut = !edge && cuts.some((x) => Math.abs(x - t) < grab);
    const onClose = e.offsetY <= 20 && (closeBoxOf(ep, 'cut', canvas) || closeBoxOf(ep, 'trim', canvas));
    canvas.style.cursor = onClose && hitClose(e, ep, canvas) ? 'pointer'
      : edge ? 'ew-resize' : onCut ? 'col-resize'
      : CUT.mode === 'trim' ? 'crosshair' : 'grab';
  });

  canvas.addEventListener('pointerdown', (e) => {
    pinch.at.set(e.pointerId, e.clientX);
    if (pinch.at.size === 2) {                       // 2 本目の指が触れたら、つまむ操作に切り替える
      CUT.drag = null;
      CUT.pan = false;
      pinch.base = { gap: pinchGap(), span: CUT.view[1] - CUT.view[0], center: timeAt(canvas, pinchMid()) };
      drawCut();
      return;
    }
    const t = timeAt(canvas, e.clientX);
    const grab = (CUT.view[1] - CUT.view[0]) * 0.012;

    // ① 選んでいるものの「×」を押したら消す
    const close = hitClose(e, ep, canvas);
    if (close === 'cut') { e.preventDefault(); removeCutAt(S.cutAt); return; }
    if (close === 'trim') { e.preventDefault(); removeSelectedTrim(); return; }

    // ② 削る範囲の端をつかんだら、伸ばし縮みさせる(どのモードでも)
    const edge = trimEdgeAt(ep, t, grab);
    if (edge) {
      e.preventDefault();
      try { canvas.setPointerCapture(e.pointerId); } catch { /* 指が離れていても続けられる */ }
      selectTrim(edge.trim);
      refreshCutLive();
      const move = (ev) => { if (pinch.at.size < 2) dragTrimEdge(edge.trim, edge.side, timeAt(canvas, ev.clientX)); };
      const up = () => {
        canvas.removeEventListener('pointermove', move);
        canvas.removeEventListener('pointerup', up);
        endTrimDrag();
      };
      canvas.addEventListener('pointermove', move);
      canvas.addEventListener('pointerup', up);
      return;
    }

    // ③ 区切りの線をつかんだら動かす
    const near = cuts.find((x) => Math.abs(x - t) < grab);
    if (near !== undefined) {
      e.preventDefault();
      try { canvas.setPointerCapture(e.pointerId); } catch { /* 指が離れていても続けられる */ }
      let current = near;
      S.cutAt = near;
      CUT.touched = 'cut';
      refreshCutLive();
      const move = (ev) => { if (pinch.at.size < 2) current = dragCutTo(current, timeAt(canvas, ev.clientX)); };
      const up = () => {
        canvas.removeEventListener('pointermove', move);
        canvas.removeEventListener('pointerup', up);
        endCutDrag();
      };
      canvas.addEventListener('pointermove', move);
      canvas.addEventListener('pointerup', up);
      return;
    }

    // ④ 「削る」モードなら、ドラッグした範囲を削る
    if (CUT.mode === 'trim') {
      e.preventDefault();
      try { canvas.setPointerCapture(e.pointerId); } catch { /* 指が離れていても続けられる */ }
      const from = t;
      CUT.drag = { a: from, b: from };
      const move = (ev) => { if (CUT.drag) { CUT.drag.b = timeAt(canvas, ev.clientX); drawCut(); } };
      const up = (ev) => {
        canvas.removeEventListener('pointermove', move);
        canvas.removeEventListener('pointerup', up);
        const to = timeAt(canvas, ev.clientX);
        CUT.drag = null;
        if (Math.abs(to - from) < (CUT.view[1] - CUT.view[0]) * 0.004) {
          const hit = inTrim(ep, from);              // 短く押したら、その範囲を選ぶ
          if (hit) { selectTrim(hit); renderCut(); }
          else { seekCut(from); drawCut(); }
          return;
        }
        addTrim(from, to);
      };
      canvas.addEventListener('pointermove', move);
      canvas.addEventListener('pointerup', up);
      return;
    }

    // ⑤ それ以外は、ドラッグでタイムラインを左右に動かす(クリックだけなら再生位置)
    e.preventDefault();
    try { canvas.setPointerCapture(e.pointerId); } catch { /* 指が離れていても続けられる */ }
    const startX = e.clientX;
    const startView = CUT.view.slice();
    const move = (ev) => {
      if (pinch.at.size >= 2) return;               // つまむ操作に切り替わったら動かさない
      const dx = ev.clientX - startX;
      if (!CUT.pan && Math.abs(dx) < 4) return;
      CUT.pan = true;
      canvas.style.cursor = 'grabbing';
      const span = startView[1] - startView[0];
      const shift = -dx / canvas.clientWidth * span;
      setView(startView[0] + shift, startView[1] + shift);
    };
    const up = (ev) => {
      canvas.removeEventListener('pointermove', move);
      canvas.removeEventListener('pointerup', up);
      canvas.style.cursor = 'grab';
      if (CUT.pan) { CUT.pan = false; return; }
      const at = timeAt(canvas, ev.clientX);
      const hit = inTrim(ep, at);
      if (hit) selectTrim(hit);
      if (CUT.mode === 'cut') addCut(at);
      else { seekCut(at); if (hit) renderCut(); }
    };
    canvas.addEventListener('pointermove', move);
    canvas.addEventListener('pointerup', up);
  });

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey || Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      zoomCut(e.deltaY > 0 ? 1.25 : 0.8, timeAt(canvas, e.clientX));
    } else {
      const span = CUT.view[1] - CUT.view[0];
      setView(CUT.view[0] + e.deltaX / 400 * span, CUT.view[1] + e.deltaX / 400 * span);
    }
  }, { passive: false });
  canvas.addEventListener('dblclick', (e) => {
    const part = partAt(ep, timeAt(canvas, e.clientX));
    if (part) togglePartTrim(part);
  });

  // 見取り図はクリックでもドラッグでも、見る場所を動かせる
  mini.addEventListener('pointerdown', (e) => {
    try { mini.setPointerCapture(e.pointerId); } catch { /* 同上 */ }
    const jump = (ev) => {
      const t = timeAt(mini, ev.clientX);
      const span = CUT.view[1] - CUT.view[0];
      setView(t - span / 2, t + span / 2);
    };
    jump(e);
    const up = () => {
      mini.removeEventListener('pointermove', jump);
      mini.removeEventListener('pointerup', up);
    };
    mini.addEventListener('pointermove', jump);
    mini.addEventListener('pointerup', up);
  });
  return { canvas, mini };
}

function renderCut() {
  const p = S.project;
  const ep = cutEpisode();
  if (!ep) return;
  if (isNarrow()) return renderCutPhone();      // スマホは 1 画面に収まる並びにする
  const cuts = (ep.cuts || []).slice().sort((a, b) => a - b);
  const trims = trimsOf(ep);
  const parts = cutParts(ep);
  if (cuts.length && !cuts.some((t) => Math.abs(t - S.cutAt) < 1e-6)) S.cutAt = cuts[0];
  const kept = parts.filter((x) => x.out >= 0.2);
  const total = keptLength(ep);
  const removed = ep.duration - total;
  if (!CUT.view[1]) CUT.view = [0, ep.duration];
  CUT.bgKey = '';

  const { canvas, mini } = makeCutTimeline(ep, cuts);

  const preview = h('div', { class: 'cutshot' },
    h('video', { muted: true, playsinline: true, preload: 'auto', disablePictureInPicture: true }),
    h('img', { alt: '', hidden: true }),
    h('div', { class: 'wait', hidden: true }));
  CUT.preview = preview;
  CUT.previewSrc = '';

  const modeButton = (mode, label, hint) => h('button', {
    class: 'modebtn' + (CUT.mode === mode ? ' on ' + mode : ''), title: hint,
    onclick: () => setCutMode(mode),
  }, label);

  /* 選んでいるものだけを出す調整欄(タイムラインの操作と二重にしない) */
  const sel = currentTrim(ep);
  const selNo = sel ? trims.findIndex((t) => Math.abs(t.start - sel.start) < 1e-6) + 1 : 0;
  const showTrim = CUT.touched === 'trim' && sel;
  const cutNudge = (label, delta) => h('button', {
    class: 'icon', disabled: !cuts.length, title: `区切りを${label}動かす`,
    onclick: () => { dragCutTo(S.cutAt, S.cutAt + delta); ensureVisible(S.cutAt); endCutDrag(); },
  }, label);
  const edgeNudge = (side, delta, label) => h('button', {
    class: 'icon', disabled: !sel, title: `${side === 'start' ? '始め' : '終わり'}を${label}動かす`,
    onclick: () => nudgeTrim(side, delta),
  }, label);

  const inspector = showTrim
    ? h('div', { class: 'inspect trim' },
        h('span', { class: 'tag' }, '削る範囲'),
        h('span', { class: 'muted small' }, `${selNo}/${trims.length}`),
        h('button', { class: 'icon', onclick: () => selectTrimStep(-1), title: '前の削る範囲' }, '◀'),
        h('button', { class: 'icon', onclick: () => selectTrimStep(1), title: '次の削る範囲' }, '▶'),
        h('span', { class: 'sep' }),
        h('span', { class: 'muted small trimside' }, '始め'),
        h('span', { class: 'mono cutclock', id: 'trimStart' }, fmt(sel.start, false)),
        edgeNudge('start', -1, '−1秒'), edgeNudge('start', -0.1, '−0.1秒'),
        edgeNudge('start', 0.1, '+0.1秒'), edgeNudge('start', 1, '+1秒'),
        h('span', { class: 'sep' }),
        h('span', { class: 'muted small trimside' }, '終わり'),
        h('span', { class: 'mono cutclock', id: 'trimEnd' }, fmt(sel.end, false)),
        edgeNudge('end', -1, '−1秒'), edgeNudge('end', -0.1, '−0.1秒'),
        edgeNudge('end', 0.1, '+0.1秒'), edgeNudge('end', 1, '+1秒'),
        h('span', { style: 'flex:1' }),
        h('span', { class: 'muted small', id: 'trimLen' }, `長さ ${fmtDuration(sel.end - sel.start)}`),
        h('button', { class: 'danger', onclick: removeSelectedTrim }, 'この赤い範囲を消す'))
    : h('div', { class: 'inspect cut' },
        h('span', { class: 'tag' }, '区切り'),
        h('span', { class: 'muted small' }, cuts.length
          ? `${cuts.findIndex((t) => Math.abs(t - S.cutAt) < 1e-6) + 1}/${cuts.length}` : 'なし'),
        h('button', { class: 'icon', disabled: !cuts.length, onclick: () => selectCut(-1), title: '前の区切り' }, '◀'),
        h('button', { class: 'icon', disabled: !cuts.length, onclick: () => selectCut(1), title: '次の区切り' }, '▶'),
        h('span', { class: 'sep' }),
        h('span', { class: 'mono cutclock', id: 'cutClock' }, cuts.length ? fmt(S.cutAt, false) : '—'),
        cutNudge('−1秒', -1), cutNudge('−0.1秒', -0.1), cutNudge('+0.1秒', 0.1), cutNudge('+1秒', 1),
        h('span', { style: 'flex:1' }),
        h('span', { class: 'muted small' }, cuts.length
          ? '線をドラッグしても動かせます' : 'まだ区切っていません(ファイルは1つ)'),
        h('button', { class: 'danger', disabled: !cuts.length, onclick: deleteSelectedCut }, 'この区切りを消す'));

  $('view-cut').replaceChildren(h('div', { class: 'page' },
    h('div', {},
      h('h1', {}, 'カット'),
      h('p', { class: 'muted' }, '「区切る」で書き出すファイルを分け、「削る」でいらない部分を取り除きます。操作はすべて Ctrl+Z で元に戻せます。')),
    p.episodes.length > 1 && h('div', { class: 'card soft' }, h('div', { class: 'row' },
      h('b', {}, '話'),
      ...p.episodes.map((e, i) => h('button', {
        class: i === S.cutEp ? 'primary' : '',
        onclick: () => {
          cutPause();
          S.cutEp = i; S.cutAt = 0; S.trimAt = -1; CUT.cursor = 0; CUT.view = [0, e.duration];
          CUT.detailKey = ''; CUT.drag = null; CUT.snap = null;
          renderCut(); loadCutWave(); loadCutAudio();
        },
      }, `第${i + 1}話`)))),

    /* プレビューとタイムライン */
    h('div', { class: 'card', style: 'display:grid;gap:10px' },
      h('div', { class: 'spread' },
        h('h3', {}, `第${S.cutEp + 1}話 ・ ${fmtDuration(ep.duration)}`),
        h('span', { class: 'muted mono', id: 'cutPreviewLabel' }, '')),
      preview,
      playBar('cutBar', CUT.cursor, ep.duration, (t) => { seekCut(t); }, trims.map((t) => ({ ...t, kind: 'drop' }))),
      h('div', { class: 'cutrow' },
        h('button', { class: 'primary', id: 'cutPlay', onclick: cutTogglePlay }, CUT.playing ? '‖ 一時停止' : '▶ 再生'),
        h('button', { class: 'icon', onclick: () => seekCut(CUT.cursor - 5), title: '5秒戻る' }, '−5秒'),
        h('button', { class: 'icon', onclick: () => seekCut(CUT.cursor + 5), title: '5秒進む' }, '+5秒'),
        h('label', { class: 'check' },
          h('input', { type: 'checkbox', checked: CUT.skip, onchange: (e) => { CUT.skip = e.target.checked; } }),
          '削る所は飛ばして再生'),
        h('label', { class: 'check', title: '軽い控えを使うと、つまみ送りが速くなります(画質は下がります)' },
          h('input', { type: 'checkbox', checked: S.light, onchange: (e) => setLightPreview(e.target.checked) }),
          'なめらか優先'),
        h('span', { style: 'flex:1' }),
        h('span', { class: 'muted small' }, 'Space 再生 / ← → コマ送り / Shift+← → 1秒')),

      /* タイムライン(編集ソフトのように、プレビューのすぐ下) */
      h('div', { class: 'stagesep' }),
      h('div', { class: 'cutbar' },
        h('div', { class: 'modes' },
          modeButton('seek', '▶ 見る', 'クリックで再生位置・ドラッグで左右に移動'),
          modeButton('cut', '区切る', 'クリックした所で、書き出すファイルを分けます'),
          modeButton('trim', '削る', 'ドラッグした範囲を書き出しから取り除きます')),
        h('span', { class: 'sep' }),
        h('button', { onclick: () => addCut(CUT.cursor), title: '再生位置で区切る' }, 'ここで区切る (S)'),
        h('button', { onclick: () => togglePartTrim(partAt(ep, CUT.cursor)), title: 'いまのカットをまるごと削る' }, 'このカットを削る (D)'),
        h('span', { style: 'flex:1' }),
        h('button', { class: 'icon', disabled: !S.undo.length, title: '元に戻す (Ctrl+Z)', onclick: undo }, '↶ 元に戻す'),
        h('button', { class: 'icon', disabled: !S.redo.length, title: 'やり直す (Ctrl+Y)', onclick: redo }, '↷ やり直す')),
      h('div', { class: 'muted small' }, CUT.mode === 'trim'
        ? 'ドラッグした範囲が「削る所」になります。赤い範囲は、端のつまみで伸ばし縮み・上の「×」で取り消せます。'
        : CUT.mode === 'cut'
          ? 'クリックした所に区切り(青い線)が入ります。線はドラッグで移動、選んでいる線の「×」で取り消せます。'
          : 'クリックでその場面を再生、左右にドラッグするとタイムラインが動きます。青い線・赤い範囲の端もドラッグできます。'),
      mini,
      canvas,
      h('div', { class: 'cutrow' },
        h('span', { class: 'muted small' }, '拡大'),
        h('button', { class: 'icon', onclick: () => zoomCut(0.5, CUT.cursor), title: '拡大 (+)' }, '＋'),
        h('button', { class: 'icon', onclick: () => zoomCut(2, CUT.cursor), title: '縮小 (−)' }, '−'),
        h('button', { onclick: () => setView(0, ep.duration) }, '全体'),
        h('button', { onclick: () => setView(CUT.cursor - 15, CUT.cursor + 15) }, '前後30秒'),
        h('button', { onclick: () => setView(CUT.cursor - 2.5, CUT.cursor + 2.5) }, '前後5秒'),
        h('span', { class: 'muted small', id: 'cutZoomLabel' }, `表示 ${fmtDuration(CUT.view[1] - CUT.view[0])}`),
        h('span', { style: 'flex:1' }),
        h('button', { onclick: () => jumpBlank(-1), title: '前の「映像がない所」へ' }, '◀ 空白'),
        h('button', { onclick: () => jumpBlank(1), title: '次の「映像がない所」へ' }, '空白 ▶')),
      inspector,
      h('div', { class: 'cutlegend' },
        h('span', {}, h('i', { class: 'k-cam' }), '映像あり'),
        h('span', {}, h('i', { class: 'k-none' }), '映像なし(静止画)'),
        h('span', {}, h('i', { class: 'k-cut' }), '区切り(ファイルの境目)'),
        h('span', {}, h('i', { class: 'k-cursor' }), '再生位置'),
        h('span', {}, h('i', { class: 'k-drop' }), '削る範囲'),
        h('span', { style: 'flex:1' }),
        h('span', { class: 'cutsum' },
          `元 ${fmtDuration(ep.duration)}`, h('span', { class: 'arrow' }, '→'),
          h('b', {}, `書き出し ${fmtDuration(total)}`),
          removed > 0.2 && h('span', { class: 'cut-removed' }, `(${fmtDuration(removed)} を削除)`),
          h('span', { class: 'muted' }, `・カット${kept.length}つ`)))),

    /* ③ 書き出すファイルと、詳しい一覧 */
    h('div', { class: 'card', style: 'display:grid;gap:10px' },
      h('h3', {}, '書き出すファイル'),
      h('table', { class: 'plan', id: 'cutPlanTable' },
        h('thead', {}, h('tr', {}, h('th', {}, 'カット'), h('th', {}, '範囲'), h('th', {}, '長さ'), h('th', {}, ''), h('th', {}, ''))),
        h('tbody', {}, parts.map((part) => {
          const dead = part.out < 0.2;
          const shortened = !dead && part.out < part.end - part.start - 0.2;
          return h('tr', {
            class: dead ? 'off' : '', style: 'cursor:pointer',
            onclick: () => { seekCut(part.start + 0.2); setView(part.start - 2, part.start + 8); },
          },
            h('td', {}, part.no ? `カット${part.no}` : '—'),
            h('td', { class: 'mono' }, `${fmt(part.start, false)} 〜 ${fmt(part.end, false)}`),
            h('td', { class: 'mono' }, dead ? '—' : fmtDuration(part.out)
              + (shortened ? ` (元 ${fmtDuration(part.end - part.start)})` : '')),
            h('td', {}, h('button', {
              class: 'ghost small',
              onclick: () => { seekCut(part.start + 0.2); setView(part.start - 2, part.start + 8); },
            }, '見る')),
            h('td', {}, h('button', {
              class: dead ? 'ghost small' : 'danger small',
              onclick: (e) => { e.stopPropagation(); togglePartTrim(part); },
            }, dead ? '↺ 元に戻す' : 'このカットを削る')));
        }))),
      !kept.length && h('div', { class: 'notice warn' }, 'すべて削られています。1つ以上を残してください。'),

      h('details', { class: 'lists', open: CUT.listsOpen },
        h('summary', { onclick: () => { CUT.listsOpen = !CUT.listsOpen; } },
          `区切りと削る範囲の一覧(区切り ${cuts.length} ・ 削る範囲 ${trims.length})`),
        h('div', { class: 'listgrid' },
          h('div', {},
            h('div', { class: 'spread' },
              h('b', {}, '区切り(ファイルの境目)'),
              h('div', { class: 'row' },
                cuts.length > 0 && h('button', { class: 'ghost small', onclick: () => setCuts([], 'すべての区切りを消しました') }, 'すべて消す'),
                h('button', {
                  class: 'ghost small',
                  onclick: guard(async () => {
                    const { suggestions } = await api('/api/cut/suggest', { project: S.project });
                    setCuts(suggestions[S.cutEp] || [], 'カメラが替わる所に戻しました');
                  }),
                }, '↺ カメラが替わる所に戻す'))),
            cuts.length
              ? h('table', { class: 'plan' }, h('tbody', {}, cuts.map((t, i) => h('tr', {
                  class: (CUT.touched !== 'trim' && Math.abs(t - S.cutAt) < 1e-6 ? 'on cutrow-on ' : '') + (inTrim(ep, t) ? 'off' : ''),
                  style: 'cursor:pointer',
                  onclick: () => { S.cutAt = t; CUT.touched = 'cut'; seekCut(t); ensureVisible(t); renderCut(); },
                },
                  h('td', {}, `${i + 1}つ目`),
                  h('td', { class: 'mono' }, fmt(t, false)),
                  h('td', { class: 'muted small' }, inTrim(ep, t) ? '削る範囲の中(効きません)' : ''),
                  h('td', {}, h('button', {
                    class: 'danger small',
                    onclick: (e) => { e.stopPropagation(); removeCutAt(t); },
                  }, '消す'))))))
              : h('p', { class: 'muted small' }, 'まだ区切っていません。')),
          h('div', {},
            h('div', { class: 'spread' },
              h('b', {}, '削る範囲'),
              trims.length > 0 && h('button', { class: 'ghost small', onclick: () => setTrims([], 'すべて元に戻しました') }, 'すべて元に戻す')),
            trims.length
              ? h('table', { class: 'plan' }, h('tbody', {}, trims.map((t, i) => h('tr', {
                  class: sel && Math.abs(t.start - sel.start) < 1e-6 ? 'on' : '',
                  style: 'cursor:pointer',
                  onclick: () => { selectTrim(t); seekCut(Math.max(0, t.start - 1)); ensureVisible(t.start); renderCut(); },
                },
                  h('td', {}, `${i + 1}か所目`),
                  h('td', { class: 'mono' }, `${fmt(t.start, false)} 〜 ${fmt(t.end, false)}`),
                  h('td', { class: 'mono' }, fmtDuration(t.end - t.start)),
                  h('td', {}, h('button', {
                    class: 'danger small',
                    onclick: (e) => { e.stopPropagation(); removeTrim(t); },
                  }, '消す'))))))
              : h('p', { class: 'muted small' }, 'いまは何も削っていません。')))),
    ),

    h('div', { class: 'footer-nav' },
      h('button', { onclick: () => { cutPause(); setStep('review'); } }, '← 確認・調整に戻る'),
      h('button', { class: 'primary big', disabled: !kept.length, onclick: () => { cutPause(); setStep('export'); } }, '次へ:書き出し →'))));

  requestAnimationFrame(() => { drawCut(true); updateCutPreview(CUT.playing); });
}

/* ---------- step 5: export ---------- */


const QUALITY = [
  { key: 'standard', label: '標準', note: '早く作れて軽い', detail: '書き出しが速く、ファイルも小さめ。まず見てもらう用に。' },
  { key: 'high', label: '高画質', note: 'おすすめ', detail: '標準より時間は約2倍、大きさは約2倍。YouTube に上げるならこちら。' },
  { key: 'best', label: '最高画質', note: '元に近い', detail: '時間は約3倍、大きさは約3倍。元の映像にいちばん近くなります。' },
];

const qualityOf = () => S.project.quality || 'high';

function setQuality(key) {
  commit(() => { S.project.quality = key; });
  refreshPlan();
}

async function refreshPlan() {
  if (!S.outdir || !S.project.episodes.length) return;
  try {
    S.plan = await api('/api/export/plan', { project: S.project, folder: S.outdir, quality: qualityOf() });
  } catch (e) {
    S.plan = null;
    toast(e.message, 'bad');
  }
  if (S.step === 'export') renderExport();
}

function renderExport() {
  const p = S.project;
  const reviews = reviewCount();
  const plan = S.plan;
  const job = S.jobs.export;
  const missing = plan?.missing || [];
  const lowSpace = plan && plan.free < plan.need;
  const overruns = [];
  p.episodes.forEach((e, i) => e.segments.forEach((s, j) => {
    const length = S.media[s.video]?.duration;
    if (s.enabled && s.video && length && s.source + (s.end - s.start) > length + 0.25) overruns.push([i, j]);
  }));

  const check = (state, text, action) =>
    h('li', { class: state }, h('span', { class: 'mark' }, state === 'ok' ? '' : '!'), h('span', { class: 'txt' }, text), action);

  $('view-export').replaceChildren(h('div', { class: 'page' },
    h('div', {},
      h('h1', {}, '書き出し'),
      h('p', { class: 'muted' }, '話ごとにMP4(1280×720・30fps)を作ります。元のファイルは変更しません。同じ名前のファイルがあるときは、別の名前にして上書きを防ぎます。')),
    h('div', { class: 'card' },
      h('h3', {}, '書き出す前の確認'),
      h('ul', { class: 'checks' },
        plan && plan.stale && check('bad',
          'ツールが新しくなっています。このまま書き出すと、直る前の状態で書き出されます。'
          + 'いまの作業を保存してから、start_windows.bat で開き直してください。'),
        check(reviews ? 'warn' : 'ok',
          reviews ? `要確認の区間が ${reviews} か所残っています。このまま書き出すと、その部分は静止画になります。` : '要確認の区間はありません',
          reviews ? h('button', { onclick: () => { setStep('review'); gotoReview(1); } }, '確認する') : null),
        check(p.still ? 'ok' : 'warn',
          p.still ? `映像がない所の画像:${basename(p.still)}` : '映像がない所の画像は未設定です(濃い青灰色の背景になります)',
          p.still ? null : h('button', { onclick: () => setStep('files') }, '設定する')),
        overruns.length > 0 && check('bad',
          `動画の長さを超えた位置を指定している区間が ${overruns.length} か所あります。直さないと書き出せません。`,
          h('button', { onclick: () => { setStep('review'); selectSegment(...overruns[0]); } }, '直す')),
        missing.length > 0 && check('bad', '見つからないファイルがあります:' + missing.map(basename).join('、')),
        lowSpace && check('bad', `空き容量が足りない可能性があります(必要な目安 ${fmtGB(plan.need)} / 空き ${fmtGB(plan.free)})`))),
    h('div', { class: 'card', style: 'display:grid;gap:10px' },
      h('h3', {}, '画質'),
      h('div', { class: 'modes' }, QUALITY.map((q) => h('button', {
        class: 'modebtn' + (qualityOf() === q.key ? ' on cut' : ''), title: q.detail,
        disabled: running('export'),
        onclick: () => setQuality(q.key),
      }, `${q.label}(${q.note})`))),
      h('p', { class: 'muted small' }, QUALITY.find((q) => q.key === qualityOf()).detail
        + (plan && plan.sizes ? ` 目安の大きさ:${fmtGB(plan.sizes.reduce((a, b) => a + b, 0))}` : ''))),
    h('div', { class: 'card', style: 'display:grid;gap:12px' },
      h('h3', {}, '書き出し先'),
      h('div', { class: 'row' },
        h('div', { class: 'pathbox', title: S.outdir }, S.outdir || 'フォルダを選んでください'),
        h('button', { onclick: guard(pickOutdir), disabled: running('export') }, S.outdir ? '変更' : 'フォルダを選ぶ')),
      plan && h('div', { style: 'overflow-x:auto' }, h('table', { class: 'plan' },
        h('thead', {}, h('tr', {}, h('th', {}, '話'), h('th', {}, 'ファイル名'), h('th', {}, '長さ'), h('th', {}, ''))),
        h('tbody', {}, plan.items.map((it) => h('tr', {},
          h('td', {}, `第${it.index + 1}話` + (it.parts > 1 ? ` のカット${it.part + 1}/${it.parts}` : '')),
          h('td', {}, it.name),
          h('td', { class: 'mono' }, fmt(it.duration, false)),
          h('td', { class: 'muted small' }, it.renamed ? '同名のファイルがあるため別名にします' : '')))))),
      plan && !lowSpace && h('p', { class: 'muted small' }, `必要な空き容量の目安 ${fmtGB(plan.need)}(空き ${fmtGB(plan.free)})`)),
    running('export')
      ? progressCard('export', '書き出しています…')
      : h('div', { class: 'footer-nav' },
          S.exported && h('div', { class: 'notice ok', style: 'flex:1' },
            `書き出しが終わりました(${S.exported.files.length} 本)`,
            h('button', { onclick: guard(() => api('/api/open-folder', { path: S.exported.folder })) }, 'フォルダを開く')),
          job?.state === 'cancelled' && h('span', { class: 'muted' }, '中止しました。途中までの話のファイルは残っています。'),
          h('button', {
            class: 'primary big',
            disabled: !plan || missing.length > 0 || overruns.length > 0 || !plan.items.length,
            onclick: guard(startExport),
          }, '▶ 書き出しを開始'))));
}

async function pickOutdir() {
  const { paths } = await api('/api/pick', { kind: 'outdir' });
  if (!paths.length) return;
  S.outdir = paths[0];
  S.exported = null;
  await refreshPlan();
}

async function startExport() {
  await refreshPlan();
  const plan = S.plan;
  if (!plan) return;
  const reviews = reviewCount();
  if (reviews) {
    const go = await ask('要確認の区間が残っています', `${reviews} か所が静止画のまま書き出されます。このまま書き出しますか?`,
      [{ label: 'このまま書き出す', value: true, primary: true }, { label: 'やめる', value: false }]);
    if (!go) return;
  }
  if (plan.free < plan.need) {
    const go = await ask('空き容量が足りない可能性があります', `必要な目安 ${fmtGB(plan.need)} に対して、空きは ${fmtGB(plan.free)} です。続けますか?`,
      [{ label: '続ける', value: true, danger: true }, { label: 'やめる', value: false }]);
    if (!go) return;
  }
  S.exported = null;
  const r = await api('/api/export', { project: S.project, folder: S.outdir, quality: qualityOf(),
    items: plan.items.map(({ index, part, name }) => ({ index, part, name })) });
  watchJob(r.job, 'export');
}

/* ---------- QR コード ----------
   スマホのカメラで読み取ってもらうための、小さな QR 作成。
   外から何も持ってこないで済むよう、バージョン3・誤り訂正 M に絞って作る。
   入るのは 42 バイトまで(http://192.168.100.100:65535/#p=123456 でも 38 バイト)。 */

/** QR から開いたときの合言葉(URL に入っている) */
const PIN_IN_URL = (() => {
  const m = location.hash.match(/p=(\d{6})/);
  if (m) {
    try { history.replaceState(null, '', location.pathname); } catch { /* そのままでも動く */ }
    return m[1];
  }
  return null;
})();

/** この画面が、ほかの端末(スマホなど)から開かれたか */
const FROM_PHONE = !['127.0.0.1', 'localhost', '::1', ''].includes(location.hostname);

const QR_SIDE = 29;          // バージョン3 の一辺
const QR_DATA = 44;          // データ語の数
const QR_ECC = 26;           // 誤り訂正語の数
const QR_MAX = QR_DATA - 2;  // 実際に入れられる文字数(モードと長さの分を引く)

const GF_EXP = new Uint8Array(512);
const GF_LOG = new Uint8Array(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
})();

const gfMul = (a, b) => (a && b ? GF_EXP[GF_LOG[a] + GF_LOG[b]] : 0);

/** 誤り訂正語を作る(リード・ソロモン) */
function qrEcc(data, count) {
  let gen = [1];
  for (let i = 0; i < count; i++) {
    const next = new Array(gen.length + 1).fill(0);
    for (let j = 0; j < gen.length; j++) {
      next[j] ^= gen[j];
      next[j + 1] ^= gfMul(gen[j], GF_EXP[i]);
    }
    gen = next;
  }
  const work = new Uint8Array(data.length + count);
  work.set(data);
  for (let i = 0; i < data.length; i++) {
    const factor = work[i];
    if (!factor) continue;
    for (let j = 0; j < gen.length; j++) work[i + j] ^= gfMul(gen[j], factor);
  }
  return work.slice(data.length);
}

/** 文字列をデータ語に直す */
function qrData(text) {
  const bytes = new TextEncoder().encode(text);
  if (bytes.length > QR_MAX) return null;
  const bits = [];
  const push = (value, n) => { for (let i = n - 1; i >= 0; i--) bits.push((value >> i) & 1); };
  push(0b0100, 4);              // バイトモード
  push(bytes.length, 8);        // 文字数
  for (const b of bytes) push(b, 8);
  const room = QR_DATA * 8;
  for (let i = 0; i < 4 && bits.length < room; i++) bits.push(0);   // 終わりの印
  while (bits.length % 8) bits.push(0);
  const out = new Uint8Array(QR_DATA);
  for (let i = 0; i < bits.length; i += 8) {
    let v = 0;
    for (let j = 0; j < 8; j++) v = (v << 1) | bits[i + j];
    out[i >> 3] = v;
  }
  const pad = [0xEC, 0x11];
  for (let i = bits.length >> 3, k = 0; i < QR_DATA; i++, k++) out[i] = pad[k % 2];
  return out;
}

/** 位置合わせなどの決まった模様を置く */
function qrFrame() {
  const grid = Array.from({ length: QR_SIDE }, () => new Array(QR_SIDE).fill(null));
  const put = (r, c, v) => { if (r >= 0 && r < QR_SIDE && c >= 0 && c < QR_SIDE) grid[r][c] = v; };
  const finder = (r0, c0) => {
    for (let r = -1; r <= 7; r++) {
      for (let c = -1; c <= 7; c++) {
        const inside = r >= 0 && r <= 6 && c >= 0 && c <= 6;
        const dark = inside && (r === 0 || r === 6 || c === 0 || c === 6
          || (r >= 2 && r <= 4 && c >= 2 && c <= 4));
        put(r0 + r, c0 + c, dark);
      }
    }
  };
  finder(0, 0);
  finder(0, QR_SIDE - 7);
  finder(QR_SIDE - 7, 0);
  for (let i = 8; i < QR_SIDE - 8; i++) {      // タイミング
    grid[6][i] = i % 2 === 0;
    grid[i][6] = i % 2 === 0;
  }
  for (let r = -2; r <= 2; r++) {              // 位置合わせ(バージョン3 は 1 か所)
    for (let c = -2; c <= 2; c++) {
      grid[22 + r][22 + c] = Math.max(Math.abs(r), Math.abs(c)) !== 1;
    }
  }
  grid[QR_SIDE - 8][8] = true;                 // いつも黒いところ
  for (let i = 0; i < 9; i++) {                // 形式情報の場所を空けておく
    if (grid[8][i] === null) grid[8][i] = false;
    if (grid[i][8] === null) grid[i][8] = false;
  }
  for (let i = 0; i < 8; i++) {
    if (grid[8][QR_SIDE - 1 - i] === null) grid[8][QR_SIDE - 1 - i] = false;
    if (grid[QR_SIDE - 1 - i][8] === null) grid[QR_SIDE - 1 - i][8] = false;
  }
  return grid;
}

const QR_MASKS = [
  (r, c) => (r + c) % 2 === 0,
  (r, c) => r % 2 === 0,
  (r, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => ((((r * c) % 2) + ((r * c) % 3)) % 2) === 0,
  (r, c) => ((((r + c) % 2) + ((r * c) % 3)) % 2) === 0,
];

/** 形式情報(誤り訂正 M と、使ったマスク) */
function qrFormat(mask) {
  const data = (0b00 << 3) | mask;             // M は 00
  let value = data << 10;
  for (let i = 4; i >= 0; i--) {
    if (value & (1 << (i + 10))) value ^= 0b10100110111 << i;
  }
  return ((data << 10) | value) ^ 0b101010000010010;
}

/** 読みにくさの点数(小さいほど良い) */
function qrPenalty(m) {
  let score = 0;
  const side = QR_SIDE;
  for (let r = 0; r < side; r++) {
    for (let c = 0; c < side; c++) {
      for (const [dr, dc] of [[0, 1], [1, 0]]) {   // 同じ色が続く
        let run = 1;
        while (c + dc * run < side && r + dr * run < side && m[r + dr * run][c + dc * run] === m[r][c]) run++;
        if (run >= 5 && ((dr === 0 && (c === 0 || m[r][c - 1] !== m[r][c]))
          || (dc === 0 && (r === 0 || m[r - 1][c] !== m[r][c])))) score += 3 + (run - 5);
      }
      if (r + 1 < side && c + 1 < side
        && m[r][c] === m[r][c + 1] && m[r][c] === m[r + 1][c] && m[r][c] === m[r + 1][c + 1]) score += 3;
    }
  }
  let dark = 0;
  for (const row of m) for (const v of row) if (v) dark++;
  score += Math.floor(Math.abs(dark * 100 / (side * side) - 50) / 5) * 10;
  return score;
}

/** 文字列から QR の白黒を作る。長すぎるときは null */
function qrMatrix(text, forceMask = -1) {
  const data = qrData(text);
  if (!data) return null;
  const all = new Uint8Array(QR_DATA + QR_ECC);
  all.set(data);
  all.set(qrEcc(data, QR_ECC), QR_DATA);

  const base = qrFrame();
  const bits = [];
  for (const byte of all) for (let i = 7; i >= 0; i--) bits.push((byte >> i) & 1);

  const filled = base.map((row) => row.slice());
  let at = 0, up = true;
  for (let right = QR_SIDE - 1; right > 0; right -= 2) {
    if (right === 6) right = 5;                 // タイミングの列は飛ばす
    for (let step = 0; step < QR_SIDE; step++) {
      const r = up ? QR_SIDE - 1 - step : step;
      for (const c of [right, right - 1]) {
        if (filled[r][c] !== null) continue;
        filled[r][c] = at < bits.length ? bits[at++] === 1 : false;
      }
    }
    up = !up;
  }

  let best = null;
  for (let mask = 0; mask < 8; mask++) {
    if (forceMask >= 0 && mask !== forceMask) continue;
    const m = filled.map((row, r) => row.map((v, c) => (base[r][c] === null ? (QR_MASKS[mask](r, c) ? !v : !!v) : !!v)));
    const format = qrFormat(mask);
    for (let i = 0; i < 15; i++) {          // 形式情報は 2 か所に、決まった順で置く
      const on = ((format >> i) & 1) === 1;
      if (i < 6) m[i][8] = on;
      else if (i === 6) m[7][8] = on;
      else if (i === 7) m[8][8] = on;
      else if (i === 8) m[8][7] = on;
      else m[8][14 - i] = on;
      if (i < 8) m[8][QR_SIDE - 1 - i] = on;
      else m[QR_SIDE - 15 + i][8] = on;
    }
    m[QR_SIDE - 8][8] = true;
    const score = qrPenalty(m);
    if (!best || score < best.score) best = { score, m };
  }
  return best.m;
}

/** QR を絵にする */
function qrImage(text, px = 240) {
  const m = qrMatrix(text);
  if (!m) return null;
  const quiet = 4;
  const side = QR_SIDE + quiet * 2;
  const parts = [];
  for (let r = 0; r < QR_SIDE; r++) {
    for (let c = 0; c < QR_SIDE; c++) {
      if (m[r][c]) parts.push(`M${c + quiet} ${r + quiet}h1v1h-1z`);
    }
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${side} ${side}" width="${px}" height="${px}" shape-rendering="crispEdges" role="img" aria-label="つなぐための QR コード">`
    + `<rect width="${side}" height="${side}" fill="#fff"/><path d="${parts.join('')}" fill="#000"/></svg>`;
  const box = h('div', { class: 'qr' });
  box.innerHTML = svg;
  return box;
}

/** 画面が狭いときの、まとめたメニュー */
function openMenu() {
  const modal = $('modal');
  const close = () => { modal.hidden = true; modal.replaceChildren(); modal.onclick = null; };
  const item = (label, fn, kind = '') => h('button', {
    class: 'menuitem ' + kind,
    onclick: () => { close(); fn(); },
  }, label);
  modal.replaceChildren(h('div', { class: 'sheet menu' },
    h('h3', {}, 'メニュー'),
    item('保存', guard(() => save(false)), 'primary'),
    item('開く', guard(openProject)),
    item('別名で保存', guard(() => save(true))),
    item('新規', guard(newProject)),
    item('↶ 元に戻す', undo),
    item('↷ やり直す', redo),
    !FROM_PHONE && item('スマホからつなぐ', guard(openPhoneDoor)),
    h('button', { class: 'ghost', onclick: close }, '閉じる')));
  modal.hidden = false;
  modal.onclick = (e) => { if (e.target === modal) close(); };
}

/* ---------- ほかの端末との同期 ---------- */

/** 相手の画面で保存されていないか見て、必要なら取り込む */
async function checkOtherScreen(saved) {
  if (!saved || !saved.at || saved.by === CLIENT_ID) return;
  if (saved.at <= (S.syncedAt || 0) + 0.001) return;
  if (S.unsaved || S.jobs.analyze || S.jobs.export) {   // こちらに預けていない変更があるときは聞く
    S.pending = saved.at;
    showBanner('ほかの端末(PC またはスマホ)で変更されました。取り込むと、この画面の未保存の変更は置き換わります。',
      h('button', { class: 'primary small', onclick: guard(() => pullOtherScreen(false)) }, '⟳ 取り込む'));
    return;
  }
  await pullOtherScreen(true);
}

/** 相手の画面の内容をこの画面に取り込む */
async function pullOtherScreen(quiet) {
  let st;
  try {
    st = await api('/api/state');
  } catch (e) {
    return toast(e.message, 'bad');
  }
  const saved = st.autosave;
  if (!saved || !saved.project) return;
  pause();
  leaveCut();
  S.project = saved.project;
  S.path = saved.path || S.path;
  S.syncedAt = saved.saved_at || 0;
  S.pending = 0;
  S.dirty = false;
  S.unsaved = false;
  S.plan = null;
  clampSelection();
  showBanner('');
  renderAll();
  toast(quiet ? 'ほかの端末の変更を取り込みました' : '取り込みました');
  if (S.step === 'export' && S.outdir) refreshPlan();
}

/* ---------- 再生バー(動画サイトのような、位置と長さが見える帯) ---------- */

/**
 * 再生位置の帯を作る。
 * id       … あとで進み具合を書き換えるための目印
 * total    … 全体の長さ(秒)
 * onSeek   … つまんだ位置に移動する処理
 * ranges   … 帯の上に薄く重ねる範囲([{start, end, kind}])。削る所などに使う
 */
function playBar(id, current, total, onSeek, ranges = []) {
  const ratio = total > 0 ? Math.max(0, Math.min(1, current / total)) : 0;
  const fill = h('i', { class: 'pbfill', style: `width:${ratio * 100}%` });
  const knob = h('b', { class: 'pbknob', style: `left:${ratio * 100}%` });
  const marks = ranges.map((r) => h('span', {
    class: 'pbmark ' + (r.kind || ''),
    style: `left:${(r.start / total) * 100}%;width:${Math.max(0.4, ((r.end - r.start) / total) * 100)}%`,
  }));
  const rail = h('div', { class: 'pbrail' }, ...marks, fill, knob);
  const at = (clientX) => {
    const box = rail.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - box.left) / box.width)) * total;
  };
  rail.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    try { rail.setPointerCapture(e.pointerId); } catch { /* 指が外れても続けられる */ }
    onSeek(at(e.clientX));
    const move = (ev) => onSeek(at(ev.clientX));
    const up = () => {
      rail.removeEventListener('pointermove', move);
      rail.removeEventListener('pointerup', up);
    };
    rail.addEventListener('pointermove', move);
    rail.addEventListener('pointerup', up);
  });
  return h('div', { class: 'playbar', id },
    h('span', { class: 'mono pbnow' }, fmt(current, false)),
    rail,
    h('span', { class: 'mono muted pbtotal' }, fmtDuration(total)));
}

/** 再生バーの進み具合だけを書き換える(作り直さない) */
function movePlayBar(id, current, total) {
  const bar = $(id);
  if (!bar) return;
  const ratio = total > 0 ? Math.max(0, Math.min(1, current / total)) : 0;
  const fill = bar.querySelector('.pbfill');
  const knob = bar.querySelector('.pbknob');
  const now = bar.querySelector('.pbnow');
  if (fill) fill.style.width = `${ratio * 100}%`;
  if (knob) knob.style.left = `${ratio * 100}%`;
  if (now) now.textContent = fmt(current, false);
}

/* ---------- スマホからつなぐ・ファイルを送る ---------- */

/** PC の画面:同じ Wi-Fi のスマホに向けて開け閉めする */
async function openPhoneDoor() {
  const info = await api('/api/lan', { open: true, port: location.port || 8765 });
  showPhoneDialog(info);
}

function showPhoneDialog(info) {
  const modal = $('modal');
  let timer = 0;
  const close = () => {
    clearInterval(timer);
    modal.hidden = true;
    modal.replaceChildren();
    modal.onclick = null;
  };
  const left = h('span', { class: 'mono' }, '');
  const tick = () => {
    const sec = Math.max(0, Math.round((info.until || 0) - Date.now() / 1000));
    left.textContent = sec ? `あと ${Math.floor(sec / 60)}分${String(sec % 60).padStart(2, '0')}秒` : '期限切れ';
    if (!sec) clearInterval(timer);
  };
  info.until = Date.now() / 1000 + (info.seconds || 0);
  tick();
  timer = setInterval(tick, 1000);

  const box = h('div', { class: 'sheet phone' },
    h('h3', {}, 'スマホからつなぐ'),
    info.urls.length
      ? h('div', { class: 'phonegrid' },
          h('div', {},
            qrImage(`${info.urls[0].replace(/\/$/, '')}/#p=${info.pin}`, 230)
              || h('p', { class: 'muted small' }, 'QR を作れませんでした。下のアドレスと合言葉をお使いください。'),
            h('p', { class: 'muted small qrnote' }, 'スマホのカメラで読み取る')),
          h('ol', { class: 'steps-list' },
            h('li', {}, 'スマホを、この PC と同じ Wi-Fi につなぐ'),
            h('li', {}, '左の QR をスマホのカメラで読み取る',
              h('div', { class: 'muted small' }, 'それだけでつながります(合言葉を打つ必要はありません)')),
            h('li', {}, 'QR が読めないときは、このアドレスを開いて合言葉を入れる',
              ...info.urls.map((u) => h('div', { class: 'bigcode' }, u)),
              h('div', { class: 'bigcode pin' }, info.pin || '—')),
            h('li', { class: 'muted small' }, left, ' で合言葉が使えなくなります(つないだあとのスマホはそのまま使えます)')))
      : h('p', { class: 'bad-text' }, 'この PC のネットワークアドレスが分かりませんでした。Wi-Fi につながっているか確認してください。'),
    h('div', { class: 'notice warn' },
      '同じ Wi-Fi にいる人が、合言葉を知っていればつなげます。'
      + '自宅や事務所など、信頼できる Wi-Fi で使ってください(公衆 Wi-Fi では使わないでください)。'),
    h('div', { class: 'row', style: 'justify-content:flex-end' },
      h('button', {
        class: 'danger',
        onclick: guard(async () => { await api('/api/lan', { open: false, port: location.port || 8765 }); close(); toast('スマホからの接続を閉じました'); }),
      }, 'つながるのをやめる'),
      h('button', { class: 'primary', onclick: close }, '閉じる')));
  modal.replaceChildren(box);
  modal.hidden = false;
  modal.onclick = (e) => { if (e.target === modal) close(); };
}

const PIN_PAGE = { page: null, hidden: [] };

/** スマホ側:合言葉を入れて鍵をもらう画面 */
function askPin(problem = '') {
  const input = h('input', {
    type: 'text', inputmode: 'numeric', pattern: '[0-9]*', maxlength: 6, placeholder: '6桁の数字',
    autocomplete: 'off', style: 'font-size:28px;text-align:center;letter-spacing:.3em;width:100%',
  });
  const message = h('p', { class: 'bad-text' }, problem);
  const send = guard(async () => {
    message.textContent = '';
    const pin = (input.value || '').replace(/\D/g, '');
    if (pin.length !== 6) { message.textContent = '6桁の数字を入れてください。'; return; }
    let r;
    try {
      r = await api('/api/pair', { pin });
    } catch (e) {
      message.textContent = e.message;
      input.value = '';
      return;
    }
    TOKEN = r.token;
    try { sessionStorage.setItem('radio-sync-token', TOKEN); } catch {}
    PIN_PAGE.page?.remove();
    for (const el of PIN_PAGE.hidden || []) el.hidden = false;
    boot();
  });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });

  const keep = ['banner', 'toasts', 'modal'];
  const hidden = [...document.body.children].filter((el) => !keep.includes(el.id));
  for (const el of hidden) el.hidden = true;
  const page = h('div', { class: 'fatal pinpage' },
    h('h1', {}, 'Radio Sync'),
    h('p', {}, 'PC の画面で「スマホからつなぐ」を押して、出てきた6桁の合言葉を入れてください。'),
    input,
    h('button', { class: 'primary big', style: 'width:100%;margin-top:12px', onclick: send }, 'つなぐ'),
    message,
    h('p', { class: 'muted small' }, 'PC 側でこのツールが開いている必要があります。'));
  PIN_PAGE.page = page;
  PIN_PAGE.hidden = hidden;
  document.body.append(page);
  setTimeout(() => input.focus(), 100);
}

/* --- ファイルを送る(スマホの動画・音声をそのまま取り込む) --- */

function sendOneFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/upload?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader('X-Token', TOKEN);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch {}
      if (xhr.status === 200 && data.item) resolve(data.item);
      else reject(new Error(data.error || `送れませんでした (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('送信中に接続が切れました。'));
    xhr.ontimeout = () => reject(new Error('時間内に送れませんでした。'));
    xhr.send(file);
  });
}

async function uploadFiles(files) {
  const list = [...files].filter((f) => f.size > 0);
  if (!list.length) return;
  const total = list.reduce((sum, f) => sum + f.size, 0);
  S.upload = { name: '', index: 0, count: list.length, ratio: 0, done: 0, total };
  renderAll();
  const added = [];
  try {
    for (const [i, file] of list.entries()) {
      S.upload.name = file.name;
      S.upload.index = i + 1;
      S.upload.ratio = 0;
      renderAll();
      const item = await sendOneFile(file, (r) => {
        S.upload.ratio = r;
        const bar = $('uploadBar');
        const label = $('uploadLabel');
        if (bar) bar.style.width = `${Math.round(((S.upload.done + r * file.size) / total) * 100)}%`;
        if (label) label.textContent = `${i + 1}/${list.length} ${file.name} … ${Math.round(r * 100)}%`;
      });
      S.upload.done += file.size;
      S.media[item.path] = item;
      added.push(item);
    }
  } catch (e) {
    toast(e.message, 'bad');
  }
  S.upload = null;
  if (added.length) {
    commit(() => {
      for (const item of added) {
        const target = item.kind === 'audio' ? 'audios' : 'videos';
        if (!S.project[target].includes(item.path)) S.project[target].push(item.path);
      }
    });
    toast(`${added.length} 個のファイルを取り込みました`);
  }
  renderAll();
}

/* ---------- project files ---------- */

async function confirmDiscard() {
  if (!S.dirty) return true;
  const choice = await ask('保存していない変更があります', '保存してから続けますか?',
    [{ label: '保存する', value: 'save', primary: true }, { label: '保存しない', value: 'discard', danger: true }, { label: 'やめる', value: 'cancel' }]);
  if (choice === 'save') return save(false);
  return choice === 'discard';
}

async function save(as) {
  let path = S.path;
  if (as || !path) {
    const r = await api('/api/pick', { kind: 'save' });
    if (!r.paths.length) return false;
    path = r.paths[0];
    if (!/\.json$/i.test(path)) path += '.json';
  }
  await api('/api/save', { project: S.project, path });
  S.path = path;
  S.dirty = false;
  renderHeader();
  toast('保存しました');
  return true;
}

async function openProject() {
  if (!(await confirmDiscard())) return;
  const { paths } = await api('/api/pick', { kind: 'open' });
  if (!paths.length) return;
  const r = await api('/api/load', { path: paths[0] });
  await useProject(r.project, r.path, false);
  toast('プロジェクトを開きました');
}

async function newProject() {
  if (!(await confirmDiscard())) return;
  await api('/api/autosave/clear', {});
  await useProject(emptyProject(), null, false);
}

async function useProject(project, path, dirty) {
  pause();
  S.project = { ...emptyProject(), ...project };
  S.path = path;
  S.dirty = dirty;
  S.undo = [];
  S.redo = [];
  S.ep = 0;
  S.seg = 0;
  S.plan = null;
  S.exported = null;
  P.radioUrl = '';
  P.camUrl = '';
  radio.removeAttribute('src');
  cam.removeAttribute('src');
  const p = S.project;
  if (p.still && !p.stills.includes(p.still)) p.stills.push(p.still);
  await inspect([...p.audios, ...p.videos, p.still, ...p.stills, ...p.episodes.flatMap((e) => e.segments.map((x) => x.still)),
    ...p.episodes.map((e) => e.audio),
    ...p.episodes.flatMap((e) => e.segments.map((s) => s.video))]).catch(() => {});
  S.step = p.episodes.length ? 'review' : 'files';
  renderAll();
  if (S.step === 'review') {
    const first = reviewCount() ? null : 0;
    if (first === null) gotoReview(1);
    loadEpisodeAudio();
  }
}

/* ---------- boot ---------- */

function wire() {
  $('btnUndo').onclick = undo;
  $('btnRedo').onclick = redo;
  $('btnNew').onclick = guard(newProject);
  $('btnOpen').onclick = guard(openProject);
  $('btnSave').onclick = guard(() => save(false));
  $('btnSaveAs').onclick = guard(() => save(true));
  $('btnPlay').onclick = togglePlay;
  $('btnSegStart').onclick = () => seek(curSeg()?.start || 0);
  $('btnBack').onclick = () => seek(Math.max(0, playTime() - 2));
  $('chkCamAudio').onchange = () => { cam.muted = !$('chkCamAudio').checked; };
  $('chkLight').checked = S.light;
  $('chkLight').onchange = () => setLightPreview($('chkLight').checked);
  for (const b of $('sideTabs').querySelectorAll('button')) b.onclick = () => setTab(b.dataset.tab);
  $('btnSampleNext').onclick = () => nextSample(1);
  $('btnSamplePrev').onclick = () => nextSample(-1);
  $('btnSampleStop').onclick = () => { stopSampling(); pause(); };
  $('btnCapture').onclick = guard(captureStill);
  wireWave();
  $('btnPhone').onclick = guard(openPhoneDoor);
  $('btnMenu').onclick = openMenu;
  window.addEventListener('resize', () => {
    if (isNarrow() !== S.narrow) {
      S.narrow = isNarrow();
      renderAll();
    }
  });
  for (const b of $('steps').querySelectorAll('button')) b.onclick = () => setStep(b.dataset.step);

  document.addEventListener('keydown', (e) => {
    if (!$('modal').hidden) return;
    if (S.step === 'cut' && !e.ctrlKey && !e.metaKey) cutKey(e);
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName);
    if ((e.ctrlKey || e.metaKey) && !typing) {
      const k = e.key.toLowerCase();
      if (k === 'z') { e.preventDefault(); return e.shiftKey ? redo() : undo(); }
      if (k === 'y') { e.preventDefault(); return redo(); }
      if (k === 's') { e.preventDefault(); return guard(() => save(false))(); }
    }
    if (typing || S.step !== 'review' || e.ctrlKey || e.metaKey || e.altKey) return;
    const handled = {
      ' ': () => togglePlay(),
      ArrowLeft: () => nudgeKey(-(e.shiftKey ? 10 : 1) / FPS),
      ArrowRight: () => nudgeKey((e.shiftKey ? 10 : 1) / FPS),
      Enter: () => approveAndNext(),
      Home: () => seek(curSeg()?.start || 0),
      n: () => gotoReview(1),
      p: () => gotoReview(-1),
    }[e.key.length === 1 ? e.key.toLowerCase() : e.key];
    if (handled) {
      e.preventDefault();
      handled();
    }
  });

  window.addEventListener('resize', () => { if (S.step === 'review') drawWave(); });
  window.addEventListener('beforeunload', (e) => {
    if (S.dirty) { e.preventDefault(); e.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => navigator.sendBeacon(`/api/bye?t=${encodeURIComponent(TOKEN)}`));
  setInterval(() => api('/api/ping', {}).then((r) => {
    if (r.stale) showBanner('ツールが新しくなっています。いまの作業を保存してから、start_windows.bat で開き直してください(開き直すまで、直した内容は使われません)。');
    else checkOtherScreen(r.saved);
  }).catch(() => {}), 5000);
}

async function boot() {
  if (!TOKEN && PIN_IN_URL) {           // QR から開いたときは、そのままつなぐ
    try {
      TOKEN = (await api('/api/pair', { pin: PIN_IN_URL })).token;
      try { sessionStorage.setItem('radio-sync-token', TOKEN); } catch { /* 使えなくても動く */ }
    } catch (e) {
      return askPin(e.message);
    }
  }
  if (!TOKEN) return askPin();          // スマホから開いたときは合言葉を聞く
  wire();
  renderAll();
  let st;
  try {
    st = await api('/api/state');
  } catch (e) {
    return showError(e.message);
  }
  const saved = st.autosave;
  const p = saved?.project;
  S.syncedAt = saved?.saved_at || 0;
  if (p && (p.audios?.length || p.videos?.length) && FROM_PHONE) {
    // ほかの端末から開いた画面は、PC と同じ状態で始める(聞かずに合わせる)
    await useProject(p, saved.path || null, true);
    S.backupAt = new Date(saved.saved_at * 1000);
    renderHeader();
    toast('PC と同じ状態で開きました');
    for (const job of st.jobs || []) watchJob(job.id, job.kind);
    return;
  }
  if (p && (p.audios?.length || p.videos?.length)) {
    const when = new Date(saved.saved_at * 1000).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const name = saved.path ? `「${basename(saved.path)}」` : '保存していないプロジェクト';
    const resume = await ask('前回の作業が残っています',
      `${name}(${when} 時点、音声 ${p.audios.length} 本・動画 ${p.videos.length} 本)の続きから再開できます。`,
      [{ label: '続きから再開', value: true, primary: true }, { label: '新しく始める', value: false }]);
    if (resume) {
      await useProject(p, saved.path || null, true);
      S.backupAt = new Date(saved.saved_at * 1000);
      renderHeader();
    } else {
      await api('/api/autosave/clear', {}).catch(() => {});
    }
  }
  for (const job of st.jobs || []) watchJob(job.id, job.kind);
}

boot();
