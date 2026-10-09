'use strict';

const $ = (id) => document.getElementById(id);

/* ---------- small helpers (same as Radio Sync) ---------- */

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

function fmtDuration(t) {
  if (!Number.isFinite(t) || t <= 0) return '長さ不明';
  const s = Math.round(t);
  const hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60;
  if (hh) return `${hh}時間${mm}分`;
  if (mm) return `${mm}分${ss ? ss + '秒' : ''}`;
  return `${ss}秒`;
}

function fmtClock(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

// Caption times are nudged in tenths of a second, so show that digit.
function fmtTime(sec) {
  const m = Math.floor(sec / 60), s = (sec % 60).toFixed(1);
  return `${m}:${(+s < 10 ? '0' : '') + s}`;
}

function fmtShift(sec) {
  return (sec > 0 ? '+' : '') + sec.toFixed(2) + '秒';
}

function fmtMinutes(seconds) {
  if (seconds < 60) return '1分未満';
  if (seconds < 3600) return `約${Math.round(seconds / 60)}分`;
  return `約${Math.floor(seconds / 3600)}時間${Math.round(seconds % 3600 / 60)}分`;
}

const fmtSize = (b) => (b >= 2 ** 30 ? (b / 2 ** 30).toFixed(1) + ' GB' : b >= 2 ** 20 ? Math.round(b / 2 ** 20) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');
const fmtDate = (t) => new Date(t * 1000).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });

let TOKEN = (() => {
  const m = location.hash.match(/t=([\w-]+)/);
  try {
    if (m) {
      sessionStorage.setItem('clipper-token', m[1]);
      history.replaceState(null, '', location.pathname);
      return m[1];
    }
    return sessionStorage.getItem('clipper-token');
  } catch {
    return m ? m[1] : null;
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
    if (++failures >= 2) showBanner('動画クリッパーが終了しています。start_windows.bat から起動し直してください(処理結果は保存されています)。');
    throw new Error('動画クリッパーと接続できませんでした。');
  }
  failures = 0;
  showBanner('');
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `エラーが発生しました (${res.status})`);
  return data;
}

const mediaUrl = (path) => `/media?t=${encodeURIComponent(TOKEN)}&p=${encodeURIComponent(path)}`;

/** 画面が狭いか(スマホ) */
const isNarrow = () => window.innerWidth <= 820;

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

function toast(text, kind = '') {
  const el = h('div', { class: 'toast ' + kind }, text);
  if (!$('toasts')) return;
  $('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 4000);
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
      if (e.key === 'Escape') { e.preventDefault(); close(buttons[buttons.length - 1].value); }
    };
    modal.replaceChildren(h('div', { class: 'mbox', role: 'dialog', 'aria-modal': 'true' },
      h('h3', {}, title), h('p', {}, body),
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

const CAN_PLAY_HEVC = (() => {
  try {
    const v = document.createElement('video');
    return ['hvc1.2.4.L153.B0', 'hvc1.1.6.L153.B0'].some((c) => v.canPlayType(`video/mp4; codecs="${c}"`) !== '');
  } catch {
    return false;
  }
})();

/* ---------- state ---------- */

const S = {
  step: 'video',
  video: null,      // the chosen source video (inspect result)
  thumb: '',        // a frame of it, behind the caption preview
  settings: null,   // processing settings, saved on the server as the next defaults
  fonts: [],
  recent: [],
  results: '',
  job: null,
  project: null,    // the result being reviewed (project_view)
  projectThumb: '',
  edit: null,       // caption edits on the review step
  narrow: false,    // 画面が狭いか(スマホ)
  upload: null,     // ファイルを送っている最中の様子
};

const STATUS_LABEL = { done: '完成', running: '処理中', cancelled: '中止', error: 'エラー', interrupted: '中断' };
const STATUS_CHIP = { done: 'done', running: 'review', cancelled: 'still', error: 'review', interrupted: 'still' };

/* ---------- navigation ---------- */

function setStep(step) {
  if (step === 'settings' && !S.video) return;
  if (step === 'review' && !S.project) return;
  if (step !== 'review') detachVideos();
  S.step = step;
  renderAll();
  window.scrollTo(0, 0);
}

function renderAll() {
  renderHeader();
  renderSteps();
  for (const v of ['video', 'settings', 'run', 'review']) $('view-' + v).hidden = S.step !== v;
  ({ video: renderVideo, settings: renderSettings, run: renderRun, review: renderReview })[S.step]();
}

function renderHeader() {
  const running = S.job?.state === 'running';
  $('docName').textContent = S.step === 'review' && S.project ? basename(S.project.source) : S.video ? basename(S.video.path) : '';
  $('docState').textContent = running ? `${S.job.kind === 'reburn' ? '作り直し中' : '処理中'} ${S.job.percent}%` : '';
}

function renderSteps() {
  const jobState = S.job?.state;
  const enabled = { video: true, settings: !!S.video, run: !!jobState && jobState !== 'idle', review: !!S.project };
  const complete = { video: !!S.video, settings: jobState === 'running' || !!S.project, run: jobState === 'done', review: false };
  for (const b of $('steps').querySelectorAll('button')) {
    const step = b.dataset.step;
    b.disabled = !enabled[step];
    b.classList.toggle('current', S.step === step);
    b.classList.toggle('complete', S.step !== step && complete[step]);
    b.querySelector('.count')?.remove();
    if (step === 'run' && jobState === 'running') b.append(h('span', { class: 'count' }, `${S.job.percent}%`));
  }
}

/* ---------- step 1: choose the video ---------- */

async function useVideoPath(path, note) {
  const { info } = await api('/api/inspect', { path });
  setVideo(info);
  if (note) toast(note);
}

function setVideo(info) {
  S.video = info;
  S.thumb = '';
  if (S.step !== 'run') setStep('video');
  else renderAll();
  api('/api/thumbnail', { path: info.path }).then((r) => {
    if (S.video?.path !== info.path) return;
    S.thumb = r.url;
    if (S.step === 'video') renderVideo();
    else if (S.step === 'settings') { settingsEditor?.update(); settingsLogo?.update(); }
  }).catch(() => {});
}

async function pickVideo() {
  const r = await api('/api/pick', {});
  if (r.info) setVideo(r.info);
}

/** スマホの中の動画を送るところ(スマホから開いたときだけ出す) */
function uploadCard() {
  const up = S.upload;
  if (up) {
    return h('div', { class: 'card soft', style: 'display:grid;gap:8px' },
      h('b', {}, '送っています…'),
      h('div', { class: 'progress' }, h('div', { id: 'uploadBar', style: `width:${Math.round(up.ratio * 100)}%` })),
      h('div', { class: 'muted small', id: 'uploadLabel' }, `${up.name} … ${Math.round(up.ratio * 100)}%`),
      h('p', { class: 'muted small' }, '送り終わるまで、この画面を閉じないでください。'));
  }
  const input = h('input', {
    type: 'file', accept: 'video/*,.mov,.mp4', style: 'display:none', id: 'uploadInput',
    onchange: (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) guard(() => uploadVideo(f))(); },
  });
  return h('div', { class: 'card soft spread' },
    h('div', {},
      h('b', {}, 'この端末から送って使う'),
      h('p', { class: 'muted small' }, 'スマホの中の動画を、この PC に送って処理します。大きい動画は Wi-Fi でも数分かかります。')),
    h('div', {}, input, h('button', { class: 'primary', onclick: () => $('uploadInput').click() }, '動画を送る')));
}

function renderVideo() {
  const v = S.video;
  $('view-video').replaceChildren(h('div', { class: 'page' },
    h('div', {},
      h('h1', {}, '動画を選ぶ'),
      h('p', { class: 'muted' }, 'テロップを入れる・切り抜きを作る動画を選びます。PCの中の動画をそのまま使い、元のファイルは変更しません。')),
    v ? sourceCard(v) : h('div', { class: 'card' },
      h('div', { class: 'pick-empty' },
        h('b', { style: 'font-size:16px' }, 'まだ動画が選ばれていません'),
        h('p', { class: 'muted' }, 'mp4 / mov などの動画を選んでください。制作ハブの「テロップ・切り抜きへ送る」で送った動画は、自動でここに入ります。'),
        h('button', { class: 'primary big', onclick: guard(pickVideo) }, '動画ファイルを選ぶ'))),
    FROM_PHONE && uploadCard(),
    h('div', { class: 'card', id: 'recentCard' }, recentList()),
    h('div', { class: 'footer-nav' },
      h('span', { class: 'muted' }, v ? '準備ができました' : '動画を選んでください'),
      h('button', { class: 'primary big', disabled: !v, onclick: () => setStep('settings') }, '次へ:処理の設定 →'))));
}

function sourceCard(v) {
  const playable = v.playable || (v.hevc && CAN_PLAY_HEVC);
  return h('div', { class: 'card source' },
    h('div', { class: 'media' },
      playable ? h('video', { src: mediaUrl(v.path), controls: true, playsinline: true, disablePictureInPicture: true, controlsList: 'nofullscreen nodownload noremoteplayback', preload: 'metadata' })
        : S.thumb ? h('img', { src: S.thumb, alt: '動画の1コマ' })
          : h('span', { class: 'none' }, 'この形式は画面で再生できません(処理はできます)')),
    h('div', { style: 'display:grid;gap:14px' },
      h('div', {}, h('h3', { style: 'margin:0;overflow-wrap:anywhere' }, v.name)),
      h('dl', { class: 'meta' },
        h('dt', {}, '長さ'), h('dd', {}, fmtDuration(v.duration)),
        h('dt', {}, '解像度'), h('dd', {}, `${v.width} × ${v.height}`),
        h('dt', {}, 'サイズ'), h('dd', {}, fmtSize(v.size)),
        h('dt', {}, '場所'), h('dd', {}, v.folder)),
      !v.has_audio && h('div', { class: 'notice bad' }, '音声が入っていない動画です。文字起こしや盛り上がり検出はできません。'),
      h('div', { class: 'row' },
        h('button', { onclick: guard(pickVideo) }, '別の動画を選ぶ'),
        h('button', { class: 'ghost', onclick: guard(() => showPlace(v.path)) }, '場所を表示'))));
}

function recentList() {
  return [
    h('div', { class: 'card-head' },
      h('div', {}, h('h3', {}, '最近の処理'), h('p', { class: 'muted small' }, '開くと、完成した動画の確認やテロップの修正を続けられます。'))),
    S.recent.length
      ? h('ul', { class: 'files' }, S.recent.map((p) => h('li', { class: 'file' },
          h('span', { class: 'chip ' + (STATUS_CHIP[p.status] || 'still') }, STATUS_LABEL[p.status] || p.status),
          h('div', { class: 'fmeta' },
            h('div', { class: 'fname', title: p.source }, basename(p.source)),
            h('div', { class: 'fsub muted' },
              h('span', {}, fmtDate(p.created)),
              h('span', {}, [p.has_captioned && 'テロップ付き動画', p.clips && `切り抜き${p.clips}本`].filter(Boolean).join('・') || '出力なし'))),
          h('button', { onclick: guard(() => openProject(p.name)) }, '開く'))))
      : h('div', { class: 'empty' }, 'まだありません'),
  ];
}

/* ---------- caption look (shared by the settings and the review step) ---------- */

const PREV_FONT = {
  gothic: '"Meiryo", sans-serif',
  yu: '"Yu Gothic UI", "Yu Gothic", sans-serif',
  mincho: '"Yu Mincho", serif',
  msgothic: '"MS Gothic", monospace',
};
const PRESETS = {
  simple: { label: 'シンプル', color: '#FFFFFF', outline_color: '#000000', outline: 2, shadow: 0, bold: false, box: false },
  bold: { label: '強調', color: '#FFFFFF', outline_color: '#000000', outline: 4, shadow: 0, bold: true, box: false },
  box: { label: 'ボックス', color: '#FFFFFF', outline_color: '#000000', outline: 6, shadow: 0, bold: false, box: true },
  stylish: { label: 'スタイリッシュ', color: '#FFFFFF', outline_color: '#2563EB', outline: 3, shadow: 2, bold: true, box: false },
  minimal: { label: 'ミニマル', color: '#F4F4F5', outline_color: '#3F3F46', outline: 1, shadow: 1, bold: false, box: false },
};
const POS_DEFS = [[0.1, 0.15], [0.5, 0.15], [0.9, 0.15], [0.1, 0.5], [0.5, 0.5], [0.9, 0.5], [0.1, 0.9], [0.5, 0.9], [0.9, 0.9]];

function hexToRgba(hex, a) {
  const x = hex.replace('#', '');
  return `rgba(${parseInt(x.slice(0, 2), 16)},${parseInt(x.slice(2, 4), 16)},${parseInt(x.slice(4, 6), 16)},${a})`;
}

// libass (the burn-in) fits the font's whole line height (winAscent + winDescent) to the font size,
// so the letters are smaller than a CSS font of the same size by this per-font ratio.
const FONT_EM = { gothic: 2048 / 3072, yu: 2048 / 2724, mincho: 2048 / 2636, msgothic: 1 };
// Mirrors src/subtitle.py (PlayRes 384x288, build_style's margins and colours).
const PLAY_X = 384, PLAY_Y = 288;

// A round outline, rx wide sideways and ry up and down (libass strokes with round corners and scales the
// border by the width and height ratios separately; a CSS text stroke would spike at corners).
function roundOutline(rx, ry, color) {
  const out = [];
  const r = Math.max(rx, ry);
  for (const k of r > 3 ? [1, 0.5] : [1]) {
    const n = Math.max(8, Math.ceil(Math.PI * r * k));
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2;
      out.push(`${(Math.cos(a) * rx * k).toFixed(2)}px ${(Math.sin(a) * ry * k).toFixed(2)}px 0 ${color}`);
    }
  }
  return out.join(',');
}

// 見出しは BorderStyle=3(帯)で、t_pos の場所に焼き込まれる。src/subtitle.py の build_title_style と同じ計算。
const TITLE_PAD = 3;
const TITLE_DEFAULT = { t_font: 'gothic', t_size: 11, t_color: '#FFFFFF', t_bold: false, t_bg: '#000000', t_bg_opacity: 0.75, t_pos: 'tl', t_mx: 10, t_my: 10 };
const TITLE_SPOTS = [['tl', '左上'], ['tc', '中央上'], ['tr', '右上'], ['bl', '左下'], ['br', '右下']];

/** 見出しを別に設定できるようになる前の設定には、これまでと同じ見た目を補う(src/subtitle.py と同じ) */
function withTitleStyle(c) {
  if ('t_size' in c) return c;
  return Object.assign(c, TITLE_DEFAULT, { t_font: c.font, t_size: Math.round(Math.min(c.size, Math.max(11, Math.min(c.size * 0.62, 20))) * 10) / 10 });
}

function applyTitleCss(el, c, box) {
  const height = box.clientHeight;
  if (!height) return;
  const scale = height / PLAY_Y, sx = box.clientWidth / PLAY_X;
  const line = c.t_size * scale;
  const padX = TITLE_PAD * sx, padY = TITLE_PAD * scale;
  const s = el.style;
  s.fontFamily = PREV_FONT[c.t_font] || PREV_FONT.gothic;
  s.fontSize = (line * (FONT_EM[c.t_font] || FONT_EM.gothic)).toFixed(2) + 'px';
  s.lineHeight = line.toFixed(2) + 'px';
  s.padding = `${padY.toFixed(2)}px ${padX.toFixed(2)}px`;
  s.color = c.t_color;
  s.fontWeight = c.t_bold ? 700 : 400;
  s.background = hexToRgba(c.t_bg, Math.round(c.t_bg_opacity * 255) / 255);
  // 焼き込みでは帯が余白の外側に広がるので、その分だけ端へ寄せる
  const x = (Math.round(c.t_mx) * sx - padX).toFixed(2) + 'px';
  const y = (Math.round(c.t_my) * scale - padY).toFixed(2) + 'px';
  const pos = c.t_pos || 'tl';
  s.left = pos === 'tc' ? '50%' : pos.endsWith('l') ? x : 'auto';
  s.right = pos.endsWith('r') ? x : 'auto';
  s.top = pos.startsWith('t') ? y : 'auto';
  s.bottom = pos.startsWith('b') ? y : 'auto';
  s.transform = pos === 'tc' ? 'translateX(-50%)' : 'none';
}

function captionMargins(c) {
  const v = Math.max(6, Math.min(Math.round((1 - c.pos_y) * PLAY_Y), PLAY_Y - 40));
  const shift = Math.round((c.pos_x - 0.5) * PLAY_X);
  return { v, l: Math.max(0, 2 * shift), r: Math.max(0, -2 * shift) };
}

// Draws the caption the way the burn-in does, so the preview matches the finished video.
function applyCaptionCss(text, c, box) {
  const height = box.clientHeight;
  if (!height) return;
  const scale = height / PLAY_Y;             // font size, vertical borders
  const sx = box.clientWidth / PLAY_X;       // horizontal borders and margins
  const line = c.size * scale;
  const em = line * (FONT_EM[c.font] || FONT_EM.gothic);
  // The box is drawn per line (as libass does), so the text sits in an inline span.
  let inner = text.firstElementChild;
  if (!inner) {
    inner = document.createElement('span');
    inner.textContent = text.textContent;
    text.replaceChildren(inner);
  }
  const s = text.style;
  const bs = inner.style;
  s.fontFamily = PREV_FONT[c.font] || PREV_FONT.gothic;
  s.fontSize = em.toFixed(2) + 'px';
  s.lineHeight = line.toFixed(2) + 'px';
  s.color = c.color;
  s.fontWeight = c.bold ? 700 : 400;
  let padX = 0;
  if (c.box) {
    // BorderStyle=3: a box in the outline colour (alpha 0x50) around the lines, as wide as the outline.
    const b = Math.max(c.outline, 2);
    padX = b * sx;
    bs.background = hexToRgba(c.outline_color, (255 - 0x50) / 255);
    bs.padding = `${(b * scale).toFixed(2)}px ${padX.toFixed(2)}px`;
    s.textShadow = 'none';
    s.filter = 'none';
  } else {
    bs.background = 'transparent';
    bs.padding = '0';
    s.textShadow = c.outline > 0 ? roundOutline(c.outline * sx, c.outline * scale, c.outline_color) : 'none';
    // The ASS shadow is a copy of the outlined text, so drop-shadow the whole element (outline included).
    const dx = (c.shadow * sx).toFixed(2), dy = (c.shadow * scale).toFixed(2);
    s.filter = c.shadow > 0 ? `drop-shadow(${dx}px ${dy}px 0 rgba(0,0,0,${((255 - 0x60) / 255).toFixed(2)}))` : 'none';
  }
  // Alignment=2: the bottom of the last line sits MarginV above the bottom edge, centred between MarginL/MarginR.
  const m = captionMargins(c);
  s.left = ((m.l + (PLAY_X - m.l - m.r) / 2) / PLAY_X * 100) + '%';
  // libass wraps by the ink width, CSS by the advance width: allow for the last letter's side bearing.
  s.maxWidth = `calc(${(PLAY_X - m.l - m.r) / PLAY_X * 100}% + ${(em * 0.15 + padX * 2).toFixed(2)}px)`;
  s.top = ((PLAY_Y - m.v) / PLAY_Y * 100) + '%';
  s.transform = 'translate(-50%, -100%)';
}

const THUMB_RATIO = {};  // thumbnail url -> width / height

function thumbRatio(url, onLoad) {
  if (!url) return 16 / 9;
  if (!THUMB_RATIO[url]) {
    THUMB_RATIO[url] = 16 / 9;
    const img = new Image();
    img.onload = () => {
      if (!img.naturalWidth || !img.naturalHeight) return;
      THUMB_RATIO[url] = img.naturalWidth / img.naturalHeight;
      onLoad();
    };
    img.src = url;
  }
  return THUMB_RATIO[url];
}

// 見た目の欄で選んでいる動画('main' 元動画 / 'clip' 切り抜き)
const CAP_TARGET = { now: 'main' };

/**
 * The caption look editor. Built once and updated in place, so sliders and dragging keep working.
 * opts: styles() -> {main, clip}, hasClip(), vertical(), thumb(), sample(), onChange()
 *       titleOn() / setTitleOn(on) … 話題の見出しを出すか(渡すと見出しの設定も出す)、titleSample() … 見出しの例
 *       logo(target) / saveLogo(target, logo) … 元動画('main')・切り抜き('clip')それぞれのロゴ
 * テロップ・見出し・ロゴは、元動画と切り抜き動画で別々に持つ。
 */
function captionEditor(opts) {
  let target = CAP_TARGET.now;      // 描き直しても、選んでいた動画(元動画・切り抜き)のまま
  const cur = () => withTitleStyle(opts.styles()[target]);
  const changed = (custom) => {
    if (custom) cur().preset = 'custom';
    update();
    opts.onChange?.();
  };

  const tabs = h('div', { class: 'switch' },
    h('button', { 'data-t': 'main', onclick: () => setTarget('main') }, '元動画(本編)'),
    h('button', { 'data-t': 'clip', onclick: () => setTarget('clip') }, '切り抜き動画'));
  // 話題の見出し
  const tOn = h('input', { type: 'checkbox', onchange: (e) => { opts.setTitleOn?.(e.target.checked); update(); opts.onChange?.(); } });
  const tFont = h('select', { onchange: (e) => { cur().t_font = e.target.value; changed(); } },
    S.fonts.map((f) => h('option', { value: f.value }, f.label)));
  const setTSize = (v) => { cur().t_size = Math.max(6, Math.min(60, Math.round(+v * 2) / 2 || 11)); changed(); };
  const tSize = h('input', { type: 'range', min: 6, max: 48, step: 0.5, oninput: (e) => setTSize(e.target.value) });
  const tSizeNum = h('input', { type: 'number', min: 6, max: 60, step: 0.5, onchange: (e) => setTSize(e.target.value) });
  const tSpots = h('div', { class: 'switch tspots', style: 'grid-template-columns:repeat(5, 1fr)' }, TITLE_SPOTS.map(([k, label]) =>
    h('button', { 'data-p': k, onclick: () => { cur().t_pos = k; changed(); } }, label)));
  const tColor = h('input', { type: 'color', oninput: (e) => { cur().t_color = e.target.value.toUpperCase(); changed(); } });
  const tBg = h('input', { type: 'color', oninput: (e) => { cur().t_bg = e.target.value.toUpperCase(); changed(); } });
  const tOpacity = h('input', { type: 'range', min: 0, max: 1, step: 0.05, oninput: (e) => { cur().t_bg_opacity = +e.target.value; changed(); } });
  const tOpacityVal = h('span', { class: 'mono' });
  const tBold = h('input', { type: 'checkbox', onchange: (e) => { cur().t_bold = e.target.checked; changed(); } });
  const tBody = h('div', { class: 'tbody' },
    h('div', { class: 'two' },
      h('div', { class: 'field' }, h('label', {}, '見出しのフォント'), tFont),
      h('div', { class: 'field' }, h('label', {}, '見出しの文字サイズ'), h('div', { class: 'sizerow' }, tSize, tSizeNum))),
    h('div', { class: 'field' }, h('label', {}, '見出しの場所'), tSpots),
    h('div', { class: 'adv-grid' },
      h('label', {}, '文字色', tColor),
      h('label', {}, '帯の色', tBg),
      h('label', {}, h('span', {}, '帯の濃さ ', tOpacityVal), tOpacity),
      h('label', { style: 'display:flex;gap:6px;align-items:center' }, tBold, '太字にする')));
  const titleField = opts.setTitleOn ? h('div', { class: 'field title-field' },
    h('label', { class: 'check tcheck' }, tOn, h('b', {}, '話題の見出しを出す'),
      h('span', { class: 'muted small' }, '(いま何の話かを短い言葉で表示します)')),
    tBody) : null;
  const font = h('select', { onchange: (e) => { cur().font = e.target.value; changed(); } },
    S.fonts.map((f) => h('option', { value: f.value }, f.label)));
  const setSize = (v) => { cur().size = Math.max(10, Math.min(80, Math.round(+v) || 18)); changed(); };
  const size = h('input', { type: 'range', min: 10, max: 72, step: 1, oninput: (e) => setSize(e.target.value) });
  const sizeNum = h('input', { type: 'number', min: 10, max: 80, step: 1, onchange: (e) => setSize(e.target.value) });
  const pos = h('div', { class: 'posgrid' }, POS_DEFS.map(([x, y]) =>
    h('button', { title: 'この位置へ移動', 'data-x': x, 'data-y': y, onclick: () => { cur().pos_x = x; cur().pos_y = y; changed(); } }, '●')));
  const presets = h('div', { class: 'presets' }, Object.entries(PRESETS).map(([k, p]) =>
    h('button', {
      'data-p': k,
      onclick: () => {
        Object.assign(cur(), { color: p.color, outline_color: p.outline_color, outline: p.outline, shadow: p.shadow, bold: p.bold, box: p.box, preset: k });
        changed();
      },
    }, p.label)));
  const color = h('input', { type: 'color', oninput: (e) => { cur().color = e.target.value.toUpperCase(); changed(true); } });
  const ocolor = h('input', { type: 'color', oninput: (e) => { cur().outline_color = e.target.value.toUpperCase(); changed(true); } });
  const outline = h('input', { type: 'range', min: 0, max: 8, step: 0.5, oninput: (e) => { cur().outline = +e.target.value; changed(true); } });
  const shadow = h('input', { type: 'range', min: 0, max: 4, step: 0.5, oninput: (e) => { cur().shadow = +e.target.value; changed(true); } });
  const bold = h('input', { type: 'checkbox', onchange: (e) => { cur().bold = e.target.checked; changed(true); } });
  const outlineVal = h('span', { class: 'mono' });
  const shadowVal = h('span', { class: 'mono' });
  const tag = h('span', { class: 'tag' });
  const text = h('div', { class: 'cap-text' });
  const title = h('div', { class: 'cap-title' });
  const logoImg = h('img', { class: 'cap-logo', alt: 'ロゴ', draggable: 'false' });
  logoImg.addEventListener('load', () => update());
  const preview = h('div', { class: 'cap-preview' }, logoImg, tag, title, text);
  // ロゴ(元動画・切り抜き共通)。このプレビューの上で動かす
  // ロゴを置く画面の横縦比。縦の切り抜きは、上下の黒い部分も含めた 9:16 の画面に置く(src/clip.py と同じ)
  const logoRatio = () => (target === 'clip' && opts.vertical() ? 9 / 16 : thumbRatio(opts.thumb(), update));
  const logoCtl = opts.saveLogo ? logoPanel({
    get: () => opts.logo(target), save: (logo) => opts.saveLogo(target, logo), thumb: opts.thumb, img: logoImg, stage: preview,
    ratio: logoRatio, onChange: () => update(),
  }) : null;

  text.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    try { text.setPointerCapture(e.pointerId); } catch { /* keep dragging while over the preview */ }
    // Move by the distance dragged, so the caption does not jump to the pointer where it was grabbed.
    const start = { x: e.clientX, y: e.clientY, px: cur().pos_x, py: cur().pos_y };
    const move = (ev) => {
      const r = preview.getBoundingClientRect();
      let x = start.px + (ev.clientX - start.x) / r.width;
      const y = start.py + (ev.clientY - start.y) / r.height;
      if (Math.abs(x - 0.5) < 0.03) x = 0.5;  // snap to the centre
      cur().pos_x = Math.min(0.97, Math.max(0.03, x));
      // The burn-in keeps the bottom of the caption between 6 and 248 of 288 from the bottom edge.
      cur().pos_y = Math.min(0.98, Math.max(0.14, y));
      update();
    };
    const up = () => {
      text.removeEventListener('pointermove', move);
      text.removeEventListener('pointerup', up);
      opts.onChange?.();
    };
    text.addEventListener('pointermove', move);
    text.addEventListener('pointerup', up);
  });

  // 見出しをドラッグして、端からの余白を変える(角は「見出しの場所」で選ぶ)
  title.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    try { title.setPointerCapture(e.pointerId); } catch { /* 外れてもドラッグは続く */ }
    const c = cur();
    const start = { x: e.clientX, y: e.clientY, mx: c.t_mx, my: c.t_my };
    const move = (ev) => {
      const r = preview.getBoundingClientRect();
      const dx = (ev.clientX - start.x) / r.width * PLAY_X, dy = (ev.clientY - start.y) / r.height * PLAY_Y;
      const spot = c.t_pos || 'tl';
      if (spot !== 'tc') c.t_mx = Math.round(clampTo(start.mx + (spot.endsWith('r') ? -dx : dx), 0, 180));
      c.t_my = Math.round(clampTo(start.my + (spot.startsWith('b') ? -dy : dy), 0, 140));
      update();
    };
    const up = () => {
      title.removeEventListener('pointermove', move);
      title.removeEventListener('pointerup', up);
      opts.onChange?.();
    };
    title.addEventListener('pointermove', move);
    title.addEventListener('pointerup', up);
  });

  // テロップ・見出し・ロゴは、それぞれ開け閉めできる。閉じていても、今の設定の要点を見出しの横に出す
  const fold = (id, name, info, open, ...body) => {
    const box = h('details', { class: 'capfold', open: sectionOpen(id, open) },
      h('summary', {}, h('b', {}, name), info),
      h('div', { class: 'capfold-body' }, ...body));
    box.addEventListener('toggle', () => rememberOpen(id, box.open));
    return box;
  };
  const capInfo = h('span', { class: 'muted small' });
  const titleInfo = h('span', { class: 'muted small' });
  const logoInfo = h('span', { class: 'muted small' });

  const el = h('div', { class: 'captioner' },
    h('div', { class: 'controls' },
      h('div', { class: 'field', 'data-role': 'tabs' }, h('label', {}, '設定する動画'), tabs,
        h('p', { class: 'hint' }, 'テロップ・見出し・ロゴは、元動画と切り抜き動画で別々に設定できます。')),
      fold('cap-caption', 'テロップ', capInfo, true,
      h('div', { class: 'two' },
        h('div', { class: 'field' }, h('label', {}, 'フォント'), font),
        h('div', { class: 'field' }, h('label', {}, '文字サイズ'), h('div', { class: 'sizerow' }, size, sizeNum))),
      h('div', { class: 'two' },
        h('div', { class: 'field' }, h('label', {}, 'スタイル'), presets),
        h('div', { class: 'field' }, h('label', {}, '位置'), pos)),
      h('details', { class: 'adv' },
        h('summary', {}, '色・縁取り・影を細かく調整'),
        h('div', { class: 'adv-grid' },
          h('label', {}, '文字色', color),
          h('label', {}, '縁取り色(ボックスでは背景色)', ocolor),
          h('label', {}, h('span', {}, '縁取りの太さ ', outlineVal), outline),
          h('label', {}, h('span', {}, '影の強さ ', shadowVal), shadow),
          h('label', { style: 'display:flex;gap:6px;align-items:center' }, bold, '太字にする')))),
      titleField && fold('cap-title', '話題の見出し', titleInfo, false, titleField),
      logoCtl && fold('cap-logo', 'ロゴ', logoInfo, false, h('div', { class: 'field cap-logo-field' }, logoCtl.el))),
    h('div', { class: 'cap-side' }, preview, h('p', { class: 'hint' }, 'プレビューの上でドラッグして動かせます。')));

  function setTarget(t) {
    target = CAP_TARGET.now = t;
    update();
  }

  function update() {
    if (target === 'clip' && !opts.hasClip()) target = 'main';
    const c = cur();
    tabs.hidden = !opts.hasClip();
    el.querySelector('[data-role="tabs"]').hidden = !opts.hasClip();
    for (const b of tabs.querySelectorAll('button')) b.classList.toggle('on', b.dataset.t === target);
    font.value = c.font;
    size.value = c.size;
    sizeNum.value = c.size;
    color.value = c.color;
    ocolor.value = c.outline_color;
    outline.value = c.outline;
    shadow.value = c.shadow;
    bold.checked = c.bold;
    outlineVal.textContent = c.outline;
    shadowVal.textContent = c.shadow;
    for (const b of presets.querySelectorAll('button')) b.classList.toggle('on', b.dataset.p === c.preset);
    for (const b of pos.querySelectorAll('button')) {
      b.classList.toggle('on', Math.abs(+b.dataset.x - c.pos_x) < 0.03 && Math.abs(+b.dataset.y - c.pos_y) < 0.03);
    }
    const titleOn = !!opts.titleOn?.();
    tOn.checked = titleOn;
    tBody.hidden = !titleOn;
    tFont.value = c.t_font;
    tSize.value = c.t_size;
    tSizeNum.value = c.t_size;
    tColor.value = c.t_color;
    tBg.value = c.t_bg;
    tOpacity.value = c.t_bg_opacity;
    tOpacityVal.textContent = `${Math.round(c.t_bg_opacity * 100)}%`;
    tBold.checked = c.t_bold;
    for (const b of tSpots.querySelectorAll('button')) b.classList.toggle('on', b.dataset.p === c.t_pos);
    const fontName = (k) => (S.fonts.find((f) => f.value === k)?.label || k).replace(/^.*[((]|[))]$/g, '');
    capInfo.textContent = `${fontName(c.font)} ${c.size}・${PRESETS[c.preset]?.label || 'カスタム'}`;
    titleInfo.textContent = titleOn
      ? `${TITLE_SPOTS.find(([k]) => k === c.t_pos)?.[1] || '左上'}・${fontName(c.t_font)} ${c.t_size}` : '出さない';
    const vertical = target === 'clip' && opts.vertical();
    const thumb = opts.thumb();
    // Same shape as the finished video: 9:16 for vertical clips, otherwise the source's own ratio.
    const ratio = vertical ? 9 / 16 : thumbRatio(thumb, update);
    preview.classList.toggle('vert', ratio < 1);
    preview.style.aspectRatio = vertical ? '' : String(ratio);
    preview.style.backgroundImage = thumb ? `url("${thumb}")` : '';
    const logo = opts.logo?.(target);
    const showLogo = !!(logo && logo.on && logo.path);
    logoImg.hidden = !showLogo;
    if (showLogo) {
      if (logoImg.dataset.src !== logo.path) {
        logoImg.dataset.src = logo.path;
        logoImg.src = mediaUrl(logo.path);
      }
      const s0 = logoImg.style;
      s0.left = `${logo.x * 100}%`;
      s0.width = `${logo.w * 100}%`;
      s0.top = `${logo.y * 100}%`;
    }
    logoImg.classList.toggle('grab', showLogo && !!logoCtl);
    logoInfo.textContent = showLogo ? `重ねる(大きさ ${Math.round(logo.w * 100)}%)` : '重ねない';
    logoCtl?.update();
    // keep the label off a caption (or a logo) near the top
    tag.textContent = target === 'main' ? '元動画のイメージ' : vertical ? '切り抜き(縦 9:16)のイメージ' : '切り抜き(元動画と同じ比率)のイメージ';
    text.textContent = opts.sample() || 'こんな感じのテロップになります';
    const heading = opts.titleOn?.() ? (opts.titleSample?.() || '見出しの例') : '';
    title.textContent = heading;
    title.hidden = !heading;
    tag.classList.toggle('low', c.pos_y < 0.4 || (showLogo && logo.y < 0.4) || (!!heading && (c.t_pos || 'tl').startsWith('t')));
    requestAnimationFrame(() => {
      applyCaptionCss(text, c, preview);
      if (heading) applyTitleCss(title, c, preview);
    });
  }

  return { el, update, setTarget };
}

/* ---------- ロゴ ----------
   位置と大きさは、動画の画面に対する割合(x, y はロゴの左上、w は幅)。src/logo.py と同じ。 */

const LOGO_DEFAULT = { on: false, path: '', x: 0.835, y: 0.04, w: 0.14 };
const LOGO_MARGIN = { x: 0.025, y: 0.04 };   // 四隅に寄せるときの余白
const round4 = (v) => Math.round(v * 10000) / 10000;
const clampTo = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** ロゴの高さ(動画の高さに対する割合)。ratio は動画の 幅/高さ */
function logoHeight(logo, img, ratio) {
  const shape = img && img.naturalWidth ? img.naturalHeight / img.naturalWidth : 0.5;
  return logo.w * shape * ratio;
}

/**
 * ロゴを選んで、ドラッグで動かし、スライダーで大きさを変える欄。
 * opts: get() -> ロゴの設定, save(ロゴの設定), thumb() -> 背景にする動画の1場面, onChange(), note
 *       img / stage を渡すと、そのプレビュー(テロップの見た目)の上のロゴを動かす(自分のプレビューは持たない)。
 *       ratio() … ロゴを置く画面の横縦比(省略すると動画の横縦比。縦の切り抜きでは 9/16)
 */
function logoPanel(opts) {
  const cur = () => ({ ...LOGO_DEFAULT, ...(opts.get() || {}) });
  const external = !!opts.img;
  const img = opts.img || h('img', { class: 'logo-box', alt: 'ロゴ', draggable: 'false' });
  const stage = opts.stage || h('div', { class: 'logo-stage' }, img);
  const onBox = h('input', { type: 'checkbox', onchange: (e) => apply({ on: e.target.checked }) });
  const name = h('div', { class: 'pathbox' });
  const pickBtn = h('button', { onclick: guard(pick) });
  const offBtn = h('button', { class: 'ghost', onclick: () => apply({ path: '', on: false }) }, '外す');
  const slider = h('input', { type: 'range', class: 'logo-size', min: 3, max: 70, step: 0.5, 'aria-label': 'ロゴの大きさ' });
  const sizeLabel = h('span', { class: 'mono' });
  const note = h('p', { class: 'hint' });
  // 決まった場所に寄せる。side は 'l'(左)・'c'(中央)・'r'(右)
  const corner = (label, side, bottom) => h('button', {
    class: 'ghost',
    onclick: () => {
      const l = cur();
      const hgt = logoHeight(l, img, ratio());
      const x = side === 'r' ? 1 - l.w - LOGO_MARGIN.x : side === 'c' ? (1 - l.w) / 2 : LOGO_MARGIN.x;
      apply({ x: round4(x), y: round4(bottom ? 1 - hgt - LOGO_MARGIN.y : LOGO_MARGIN.y) });
    },
  }, label);
  const body = h('div', { class: 'logo-body' },
    !external && stage,
    h('div', { class: 'row' },
      h('span', {}, '大きさ'), slider, sizeLabel,
      h('span', { class: 'corners' }, corner('左上', 'l', false), corner('中央上', 'c', false), corner('右上', 'r', false),
        corner('左下', 'l', true), corner('右下', 'r', true))),
    note);
  const el = h('div', { class: 'logo-panel' },
    h('div', { class: 'row' }, h('label', { class: 'check logo-on' }, onBox, 'ロゴを重ねる'), name, pickBtn, offBtn),
    body);

  const ratio = () => opts.ratio?.() || thumbRatio(opts.thumb(), update);
  function apply(change) {
    opts.save({ ...cur(), ...change });
    update();
    opts.onChange?.();
  }
  async function pick() {
    const r = await api('/api/pick', { kind: 'logo' });
    if (r.path) apply({ path: r.path, on: true });
  }
  function place(l) {
    img.style.left = `${l.x * 100}%`;
    img.style.top = `${l.y * 100}%`;
    img.style.width = `${l.w * 100}%`;
  }

  // ドラッグで動かす(画面の外には出さない)
  let drag = null;
  img.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    try { img.setPointerCapture(ev.pointerId); } catch { /* 外れてもドラッグは続く */ }
    drag = { box: stage.getBoundingClientRect(), sx: ev.clientX, sy: ev.clientY, from: cur(), now: cur() };
  });
  img.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const l = drag.from;
    const hgt = logoHeight(l, img, ratio());
    drag.now = { ...l,
      x: clampTo(l.x + (ev.clientX - drag.sx) / drag.box.width, 0, 1 - l.w),
      y: clampTo(l.y + (ev.clientY - drag.sy) / drag.box.height, 0, Math.max(0, 1 - hgt)) };
    place(drag.now);
  });
  const drop = () => {
    if (!drag) return;
    const { x, y } = drag.now;
    drag = null;
    apply({ x: round4(x), y: round4(y) });
  };
  img.addEventListener('pointerup', drop);
  img.addEventListener('pointercancel', drop);

  // 大きさを変える。画面の端に近い側(右上に置いていれば右上の角)は動かさない
  const resized = (w) => {
    const l = cur();
    const r = ratio();
    const hgt0 = logoHeight(l, img, r);
    const next = { ...l, w };
    const hgt = logoHeight(next, img, r);
    next.x = clampTo(l.x + l.w / 2 > 0.5 ? l.x + l.w - w : l.x, 0, 1 - w);
    next.y = clampTo(l.y + hgt0 / 2 > 0.5 ? l.y + hgt0 - hgt : l.y, 0, Math.max(0, 1 - hgt));
    return next;
  };
  slider.addEventListener('input', () => {
    const next = resized(+slider.value / 100);
    place(next);
    sizeLabel.textContent = `${Math.round(next.w * 100)}%`;
  });
  slider.addEventListener('change', () => {
    const next = resized(+slider.value / 100);
    apply({ x: round4(next.x), y: round4(next.y), w: round4(next.w) });
  });
  if (!external) img.addEventListener('load', () => { update(); opts.onChange?.(); });

  function update() {
    const l = cur();
    onBox.checked = l.on && !!l.path;
    onBox.disabled = !l.path;
    name.textContent = l.path ? basename(l.path) : '画像が選ばれていません';
    name.title = l.path;
    pickBtn.textContent = l.path ? '選び直す' : '画像を選ぶ';
    offBtn.hidden = !l.path;
    body.hidden = !l.path;
    if (!external) {
      if (l.path && img.dataset.src !== l.path) {
        img.dataset.src = l.path;
        img.src = mediaUrl(l.path);
      }
      img.classList.toggle('off', !l.on);
      const thumb = opts.thumb();
      stage.style.aspectRatio = String(ratio());
      stage.style.backgroundImage = thumb ? `url("${thumb}")` : '';
      place(l);
    }
    slider.value = String(l.w * 100);
    sizeLabel.textContent = `${Math.round(l.w * 100)}%`;
    note.textContent = (external ? '' : 'ドラッグで動かせます。元動画と切り抜き動画に同じ位置で入ります。')
      + (l.on ? '' : '「ロゴを重ねる」が外れているため、動画には入りません。');
    note.hidden = !note.textContent;
  }

  update();
  return { el, update };
}

/* ---------- step 2: settings ---------- */

let settingsEditor = null;
let settingsTimer = 0;

function saveSettingsSoon() {
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(() => api('/api/settings', { settings: S.settings }).catch(() => {}), 800);
}

/** オープニング・エンディングを選ぶ1行。kind は 'intro' か 'outro'。 */
function bookendRow(kind, label, note) {
  const st = S.settings;
  const path = st[kind] || '';
  return h('div', { class: 'bookend' + (path ? ' on' : '') },
    h('div', { class: 'be-name' },
      h('b', {}, label),
      h('span', { class: 'muted small' }, path ? basename(path) : note)),
    h('div', { class: 'row' },
      h('button', {
        onclick: guard(async () => {
          const r = await api('/api/pick', { kind });
          if (!r.path) return;
          st[kind] = r.path;
          saveSettingsSoon();
          renderSettings();
        }),
      }, path ? '選び直す' : '動画を選ぶ'),
      path && h('button', {
        class: 'ghost',
        onclick: () => { st[kind] = ''; saveSettingsSoon(); renderSettings(); },
      }, '外す')));
}

let settingsLogo = null;

function renderSettings() {
  const st = S.settings;
  const setOption = (k, v) => { st[k] = v; saveSettingsSoon(); renderSettings(); };
  const task = (k, title, desc) => h('button', { class: 'task' + (st[k] ? ' on' : ''), onclick: () => setOption(k, !st[k]) },
    h('b', {}, title), h('span', {}, desc));
  const logoKey = (t) => (t === 'clip' ? 'logo_clip' : 'logo');
  settingsEditor = st.do_transcribe ? captionEditor({
    styles: () => st.style,
    hasClip: () => st.do_clip,
    vertical: () => st.orientation === 'vertical',
    titleOn: () => st.show_titles,
    setTitleOn: (on) => { st.show_titles = on; saveSettingsSoon(); renderSettings(); },
    titleSample: () => '',
    thumb: () => S.thumb,
    sample: () => '',
    logo: (t) => st[logoKey(t)] || st.logo,
    saveLogo: (t, logo) => { st[logoKey(t)] = logo; saveSettingsSoon(); },
    onChange: saveSettingsSoon,
  }) : null;
  // テロップを作らないときのロゴは、元動画・切り抜きで同じものを使う
  settingsLogo = settingsEditor ? null : logoPanel({
    get: () => st.logo,
    save: (logo) => { st.logo = logo; st.logo_clip = logo; saveSettingsSoon(); },
    thumb: () => S.thumb,
    onChange: () => settingsEditor?.update(),
  });
  const running = S.job?.state === 'running';

  $('view-settings').replaceChildren(h('div', { class: 'page wide' },
    h('div', {},
      h('h1', {}, '処理の設定'),
      h('p', { class: 'muted' }, `「${basename(S.video.path)}」の処理内容を選びます。設定は次回もそのまま使われます。`)),
    h('div', { class: 'card', style: 'display:grid;gap:12px' },
      h('h3', {}, '処理内容'),
      h('div', { class: 'tasks' },
        task('do_transcribe', '文字起こし + テロップ', '音声を自動で文字起こしして、テロップを動画に焼き込みます。完成後に文字や見た目を直せます。'),
        task('do_clip', '切り抜き作成', '盛り上がった場面を上位5か所、根拠つきで選んで切り抜きます。'))),
    settingsEditor && h('div', { class: 'card', style: 'display:grid;gap:12px' },
      h('h3', {}, 'テロップ・見出し・ロゴ'),
      h('p', { class: 'muted small' }, '設定は次の動画にも引き継ぎます。'),
      settingsEditor.el),
    settingsLogo && h('div', { class: 'card', style: 'display:grid;gap:12px' },
      h('h3', {}, 'ロゴ'),
      h('p', { class: 'muted small' }, '番組ロゴなどの画像を、元動画と切り抜き動画に重ねます。'),
      settingsLogo.el),
    st.do_clip && h('div', { class: 'card', style: 'display:grid;gap:14px' },
      h('h3', {}, '切り抜きの設定'),
      h('div', { class: 'two' },
        h('div', { class: 'field' }, h('label', {}, '向き'),
          h('div', { class: 'switch' },
            h('button', { class: st.orientation === 'horizontal' ? 'on' : '', onclick: () => setOption('orientation', 'horizontal') }, '横 16:9'),
            h('button', { class: st.orientation === 'vertical' ? 'on' : '', onclick: () => setOption('orientation', 'vertical') }, '縦 9:16'))),
        h('div', { class: 'field' },
          h('label', {}, '1本の長さの目安', h('span', { class: 'hint', id: 'clipLenLabel' }, `${st.clip_length}秒`)),
          h('div', { class: 'range-row' },
            h('input', {
              type: 'range', min: 10, max: 60, step: 5, value: st.clip_length,
              oninput: (e) => { st.clip_length = +e.target.value; $('clipLenLabel').textContent = `${st.clip_length}秒`; saveSettingsSoon(); },
            })))),
      h('p', { class: 'hint' }, '話の途中で切れないよう、実際の長さは目安の0.7〜2倍になります。'
        + (st.orientation === 'vertical' ? '縦は元の動画を中央に置き、上下を黒にします。' : ''))),
    h('div', { class: 'card', style: 'display:grid;gap:14px' },
      h('h3', {}, '仕上がり'),
      h('div', { class: 'tasks' },
        task('hq', '画質優先モード', '元動画の解像度を保ち、より高画質に出力します。時間がかかり、ファイルも大きくなります。'),
        st.do_transcribe && task('check_captions', '焼き込む前にテロップを確かめる',
          '文字起こしのあと一度止まり、テロップの文字を直してから焼き込みます(一括置換や、AI に誤字を探してもらうこともできます)。作り直しが要りません。')),
      h('div', { class: 'field' },
        h('label', {}, 'オープニング・エンディング'),
        h('div', { class: 'bookends' },
          bookendRow('intro', 'オープニング', '本編の前につなぎます'),
          bookendRow('outro', 'エンディング', '本編を徐々に暗くしてから、後ろにつなぎます')),
        h('p', { class: 'hint' }, '元動画の前後につなぎます(切り抜きには付きません)。エンディングの前は、本編を2秒かけて暗くします。')),
      st.do_transcribe && st.show_titles && h('div', { class: 'field' },
        h('label', {}, '見出しの作り方'),
        h('div', { class: 'switch', style: 'max-width:520px' },
          h('button', { class: st.title_maker !== 'words' ? 'on' : '', onclick: () => setOption('title_maker', 'ai') }, 'AI に作ってもらう'),
          h('button', { class: st.title_maker === 'words' ? 'on' : '', onclick: () => setOption('title_maker', 'words') }, '言葉を拾う(無料)')),
        h('p', { class: 'hint' }, st.title_maker === 'words'
          ? '話によく出てくる言葉を見出しにします。'
          : S.hasKey
            ? '文字起こしのあと、Claude が見出しと概要欄を作ります(35分の回で約20〜30円)。'
            : '文字起こしのあと一度止まり、ChatGPT などで作った見出しで焼き込みます。概要欄も同時にできます。')),
      st.do_transcribe && st.show_titles && st.title_maker === 'words' && h('div', { class: 'field' },
        h('label', {}, '見出しの細かさ'),
        h('div', { class: 'switch', style: 'max-width:640px' },
          h('button', { class: st.title_scope === 'fine' ? 'on' : '', onclick: () => setOption('title_scope', 'fine') }, '話ごと'),
          h('button', { class: st.title_scope === 'corner' ? 'on' : '', onclick: () => setOption('title_scope', 'corner') }, 'コーナーごと'),
          h('button', { class: st.title_scope === 'whole' ? 'on' : '', onclick: () => setOption('title_scope', 'whole') }, '動画に1つ')),
        h('p', { class: 'hint' }, '話ごと=1〜4分、コーナーごと=3〜6分で変わります。動画に1つ=その回のテーマをずっと出します。')),
      st.do_transcribe && h('div', { class: 'field' },
        h('label', {}, '文字起こしの精度'),
        h('div', { class: 'switch', style: 'max-width:520px' },
          h('button', { class: st.accuracy === 'standard' ? 'on' : '', onclick: () => setOption('accuracy', 'standard') }, '標準(速い)'),
          h('button', { class: st.accuracy === 'high' ? 'on' : '', onclick: () => setOption('accuracy', 'high') }, '高精度(1.5倍ほどの時間)')),
        h('p', { class: 'hint' }, '仕上げには高精度がおすすめです(初回だけ約1.6GBのモデルを取得します)。'))),
    h('div', { class: 'footer-nav' },
      h('span', { class: 'muted' }, running ? '別の処理が実行中です' : !(st.do_transcribe || st.do_clip) ? '処理内容を1つ以上選んでください' : ''),
      h('button', { onclick: () => setStep('video') }, '← 動画を選ぶ'),
      h('button', { class: 'primary big', disabled: running || !(st.do_transcribe || st.do_clip), onclick: guard(startProcess) }, '▶ 処理を開始'))));
  settingsEditor?.update();
}

async function startProcess() {
  await api('/api/settings', { settings: S.settings }).catch(() => {});
  const r = await api('/api/process', { path: S.video.path, settings: S.settings });
  S.job = { state: 'running', kind: 'process', project: r.project, steps: [], step: -1, percent: 0, message: '準備しています…' };
  setStep('run');
  poll();
}

/* ---------- step 3: progress ---------- */

function renderRun() {
  const job = S.job || { state: 'idle' };
  const title = job.kind === 'reburn' ? '作り直しています…' : '処理しています…';
  let body;
  const waiting = job.state === 'running' && (job.wait === 'titles' || job.wait === 'captions');
  if (waiting) {
    body = titlesWaitView(job);
  } else if (job.state === 'running') {
    body = h('div', { class: 'card', 'data-job': '1', style: 'display:grid;gap:12px' },
      h('div', { class: 'spread' }, h('b', {}, title), h('span', { class: 'pct mono' }, `${job.percent}%`)),
      h('div', { class: 'progress' }, h('div', { style: `width:${job.percent}%` })),
      h('div', { class: 'rail' }),
      h('div', { class: 'spread' },
        h('span', { class: 'msg muted small' }, job.message),
        h('span', { class: 'row' },
          h('span', { class: 'eta muted small' }),
          h('button', {
            class: 'danger',
            onclick: guard(async () => {
              const go = await ask('処理を中止しますか?', '作りかけの動画は保存されません(完成済みの動画はそのまま残ります)。',
                [{ label: '中止する', value: true, danger: true }, { label: '続ける', value: false }]);
              if (go) await api('/api/cancel', {});
            }),
          }, '中止'))),
      h('p', { class: 'muted small' }, 'この画面を閉じても処理は続きます。もう一度開くと、進み具合を確認できます。'));
  } else if (job.state === 'error') {
    body = h('div', { class: 'card errbox' },
      h('div', { class: 'notice bad' }, job.message || 'エラーが発生しました'),
      job.hint && h('p', {}, job.hint),
      job.detail && h('div', { class: 'detail' }, '詳細情報(配布者への連絡用): ' + job.detail),
      h('div', { class: 'row' },
        S.video && h('button', { class: 'primary', onclick: () => setStep('settings') }, '設定に戻る'),
        job.project && h('button', { onclick: guard(() => openProject(job.project)) }, 'できた所まで確認する')));
  } else if (job.state === 'cancelled') {
    body = h('div', { class: 'card', style: 'display:grid;gap:10px' },
      h('b', {}, '中止しました'),
      h('div', { class: 'row' },
        S.video && h('button', { class: 'primary', onclick: () => setStep('settings') }, '設定に戻る'),
        job.project && h('button', { onclick: guard(() => openProject(job.project)) }, '結果を確認する')));
  } else {
    body = h('div', { class: 'card' }, h('p', { class: 'muted' }, '実行中の処理はありません。'));
  }
  $('view-run').replaceChildren(h('div', { class: 'page' + (waiting ? ' wide' : '') },
    h('div', {}, h('h1', {}, '処理'), h('p', { class: 'muted' }, '処理はこのPCの中だけで行い、動画を外部に送ることはありません。')),
    body));
  updateProgress();
}

/**
 * 文字起こしが終わって止まっているときの画面。
 * wait='titles' … 見出し(概要欄)を作り、その見出しで焼き込む(テロップも直せる)
 * wait='captions' … テロップを確かめて直してから焼き込む
 */
function titlesWaitView(job) {
  const onlyCaptions = job.wait === 'captions';
  const go = guard(async (mode) => {
    const d = CH.data;
    const segments = waitSegments(job);
    if (segments && !segments.some((s) => s.text)) return toast('テロップの文字がすべて空です', 'warn');
    if (mode === 'chapters') {
      if (!d || !d.chapters.some((c) => c.title.trim())) return toast('見出しがありません', 'warn');
      if (d.source === 'draft') {
        const ok = await ask('下書きのまま続けますか?',
          'まだ自動で拾った言葉の見出し(下書き)のままです。ChatGPT などで作った見出しを使うときは、先に「返事を貼り付け」をしてください。',
          [{ label: 'このまま続ける', value: true }, { label: '戻る', value: false, primary: true }]);
        if (!ok) return;
      }
      const long = longLabels(d.chapters);
      if (long.length) {
        const fix = await ask('画面の見出しが長すぎます',
          `${long.map((c) => `「${c.label}」(${labelWidth(c.label)}文字)`).join('、')} は、2行になることがあります。`
          + `${LABEL_MAX}文字以内に直すと1行に収まります。`,
          [{ label: '直す', value: true, primary: true }, { label: 'このまま進める', value: false }]);
        if (fix) {
          $('chapCard')?.querySelector('.clabel.long')?.focus();
          return;
        }
      }
    }
    clearTimeout(CH.saveTimer);
    clearTimeout(W.saveTimer);
    await api('/api/titles-continue', {
      project: job.project, mode, segments,
      ...(mode === 'chapters' ? { topics: d.topics, chapters: d.chapters, source: d.source } : {}),
    });
    toast(onlyCaptions ? 'このテロップで焼き込みます'
      : mode === 'chapters' ? 'この見出しで焼き込みを続けます' : '言葉を拾う方式で焼き込みを続けます');
    W.project = '';
    poll();
  });
  const stop = guard(async () => {
    const yes = await ask('処理を中止しますか?', '文字起こしの結果は保存されています(ここで直したテロップは使われません)。',
      [{ label: '中止する', value: true, danger: true }, { label: '続ける', value: false }]);
    if (yes) await api('/api/cancel', {});
  });
  if (onlyCaptions) {
    return h('div', { style: 'display:grid;gap:14px' },
      h('div', { class: 'card', style: 'display:grid;gap:10px' },
        h('div', { class: 'spread' },
          h('b', {}, '文字起こしが終わりました。テロップを確かめてから焼き込みます'),
          h('span', { class: 'chip review' }, '一時停止中')),
        h('p', { class: 'muted small' }, '焼き込む前に直すので、あとで作り直す必要がありません。'),
        h('ol', { class: 'waitsteps' },
          h('li', {}, 'テロップを見て、おかしいところを直す(「まとめて直す」で一括置換や、AI に誤字を探してもらうこともできます)'),
          h('li', {}, 'いちばん下の「このテロップで焼き込む」'))),
      waitCaptionsCard(job),
      h('div', { class: 'card waitgo' },
        h('button', { class: 'primary big', onclick: () => go('captions') }, 'このテロップで焼き込む ▶'),
        h('span', { style: 'flex:1' }),
        h('button', { class: 'danger', onclick: stop }, '中止')));
  }
  return h('div', { style: 'display:grid;gap:14px' },
    h('div', { class: 'card', style: 'display:grid;gap:10px' },
      h('div', { class: 'spread' },
        h('b', {}, '文字起こしが終わりました。見出しを決めてから焼き込みます'),
        h('span', { class: 'chip review' }, '一時停止中')),
      h('p', { class: 'muted small' }, '画面の見出しと YouTube の概要欄を、ここで一緒に作ります。'),
      job.wait_note && h('div', { class: 'notice warn' }, job.wait_note),
      h('ol', { class: 'waitsteps' },
        h('li', {}, S.hasKey ? '「AI で作る」を押す(または「依頼文をコピー」して ChatGPT や claude.ai に貼って送る)'
          : '「依頼文をコピー」を押して、ChatGPT や claude.ai に貼って送る'),
        !S.hasKey && h('li', {}, '返事が来たら、まるごとコピーして「返事を貼り付け」'),
        h('li', {}, '見出しを確かめて、いちばん下の「この見出しで続ける」(テロップも、下の「テロップの確認」で直せます)'))),
    chaptersCard({ name: job.project }),
    waitCaptionsCard(job),
    h('div', { class: 'card waitgo' },
      h('button', { class: 'primary big', onclick: () => go('chapters') }, 'この見出しで続ける ▶'),
      h('button', { onclick: () => go('words') }, '言葉を拾う方式で続ける'),
      h('span', { style: 'flex:1' }),
      h('button', { class: 'danger', onclick: stop }, '中止')));
}

function updateProgress() {
  const job = S.job;
  const card = document.querySelector('[data-job]');
  if (!card || job?.state !== 'running') return;
  card.querySelector('.progress > div').style.width = job.percent + '%';
  card.querySelector('.pct').textContent = job.percent + '%';
  card.querySelector('.msg').textContent = job.message || '';
  const eta = job.eta != null ? `残り ${fmtMinutes(job.eta)}` : '残り時間を計算中…';
  card.querySelector('.eta').textContent = job.elapsed != null ? `経過 ${fmtMinutes(job.elapsed)}・${eta}` : eta;
  card.querySelector('.rail').replaceChildren(...(job.steps || []).map((name, i) =>
    h('span', { class: 'chip' + (i < job.step ? ' did' : i === job.step ? ' now' : '') }, (i < job.step ? '' : '') + name)));
  renderHeader();
  renderSteps();
}

/* ---------- step 4: review & fix ---------- */

let reviewEditor = null;
let draftTimer = 0;
let stopAt = null;
let activeLine = 0;

async function openProject(name) {
  const project = await api('/api/project', { project: name });
  for (const k of ['main', 'clip']) if (project.style?.[k]) withTitleStyle(project.style[k]);   // 見出しを別に持つ前の結果
  S.project = project;
  const draft = project.draft || {};
  // A draft keeps whole lines (text and timing); drafts from older versions kept only the texts.
  // `orig` keeps what the line looked like when it was made, so "修正中" stays right after lines are added or removed.
  const segs = Array.isArray(draft.segs) && draft.segs.length
    ? draft.segs.map((s) => ({ start: +s.start, end: +s.end, text: String(s.text ?? ''), orig: s.orig || null }))
    : project.segments.map((s, i) => ({
        start: s.start, end: s.end,
        text: draft.texts && typeof draft.texts[i] === 'string' ? draft.texts[i] : s.text,
        orig: { start: s.start, end: s.end, text: s.text },
      }));
  S.edit = {
    segs,
    titles: (draft.titles || project.titles || []).map((t) => ({ start: +t.start, end: +t.end, text: String(t.text ?? '') })),
    showTitles: typeof draft.showTitles === 'boolean' ? draft.showTitles : !!project.settings.show_titles,
    shift: typeof draft.shift === 'number' ? draft.shift : 0,
    style: JSON.parse(JSON.stringify(draft.style || project.style)),
    logo: { ...LOGO_DEFAULT, ...(draft.logo || project.settings.logo || {}) },
    // 切り抜きのロゴを別に持つ前の結果では、元動画と同じロゴ
    logoClip: { ...LOGO_DEFAULT, ...(draft.logoClip || project.settings.logo_clip || project.settings.logo || {}) },
    intro: typeof draft.intro === 'string' ? draft.intro : project.settings.intro || '',
    outro: typeof draft.outro === 'string' ? draft.outro : project.settings.outro || '',
    main: true,
    clip: project.clips.length > 0,
  };
  S.projectThumb = '';
  activeLine = 0;
  setStep('review');
  if (project.source_exists) {
    api('/api/thumbnail', { path: project.source }).then((r) => {
      if (S.project?.name !== name) return;
      S.projectThumb = r.url;
      reviewEditor?.update();
    }).catch(() => {});
  }
}

function saveDraftSoon() {
  clearTimeout(draftTimer);
  refreshRebuildBar();
  const name = S.project.name;
  draftTimer = setTimeout(() => api('/api/draft', { project: name, draft: { segs: S.edit.segs, titles: S.edit.titles, showTitles: S.edit.showTitles, shift: S.edit.shift, style: S.edit.style, logo: S.edit.logo, logoClip: S.edit.logoClip, intro: S.edit.intro, outro: S.edit.outro } }).catch(() => {}), 1000);
}

function detachVideos() {
  for (const v of document.querySelectorAll('#view-review video')) {
    v.pause();
    v.removeAttribute('src');
    v.load();
  }
}

function fileUrl(name) {
  return S.project.files.find((f) => f.name === name)?.url || '';
}

/* The caption lines being edited: {start, end, text}. Timing can be nudged per line or shifted as a whole. */

function editedSegs() {
  const shift = S.edit.shift || 0;
  return S.edit.segs
    .filter((s) => s.text.trim())
    .map((s) => ({ start: Math.max(0, +(s.start + shift).toFixed(2)), end: Math.max(0.2, +(s.end + shift).toFixed(2)), text: s.text.trim() }));
}

function segChanged(i) {
  const s = S.edit.segs[i];
  const o = s.orig;
  return !o || o.text !== s.text.trim() || Math.abs(o.start - s.start) > 0.01 || Math.abs(o.end - s.end) > 0.01;
}

function afterLineEdit(focus) {
  saveDraftSoon();
  activeLine = Math.max(0, Math.min(focus, S.edit.segs.length - 1));
  renderReview();
  const el = document.querySelector(`.lines input[data-i="${activeLine}"]`);
  if (el) { el.focus(); el.scrollIntoView({ block: 'center' }); }
}

/** Split one line in two at the caret (or in the middle), sharing the time by how many characters go each way. */
function splitLine(i, caret) {
  const seg = S.edit.segs[i];
  const text = seg.text;
  const at = caret > 0 && caret < text.length ? caret : Math.ceil(text.length / 2);
  if (text.length < 2) return;
  const mid = +(seg.start + (seg.end - seg.start) * (at / text.length)).toFixed(2);
  S.edit.segs.splice(i, 1,
    { start: seg.start, end: Math.max(seg.start + 0.2, +(mid - 0.05).toFixed(2)), text: text.slice(0, at).trim(), orig: seg.orig },
    { start: mid, end: Math.max(mid + 0.2, seg.end), text: text.slice(at).trim(), orig: null });
  afterLineEdit(i + 1);
}

/** Add an empty line after this one: in the gap that follows it, or by giving up its own second half. */
function insertAfter(i) {
  const seg = S.edit.segs[i];
  const next = S.edit.segs[i + 1];
  let start, end;
  if (!next || next.start - seg.end >= 0.6) {
    start = +(seg.end + 0.05).toFixed(2);
    end = +Math.min(start + 1.5, next ? next.start - 0.05 : start + 1.5).toFixed(2);
  } else {
    const mid = +((seg.start + seg.end) / 2).toFixed(2);
    end = seg.end;
    seg.end = Math.max(seg.start + 0.2, +(mid - 0.05).toFixed(2));
    start = mid;
  }
  S.edit.segs.splice(i + 1, 0, { start, end: Math.max(start + 0.2, end), text: '', orig: null });
  afterLineEdit(i + 1);
}

function removeLine(i) {
  S.edit.segs.splice(i, 1);
  afterLineEdit(Math.max(0, i - 1));
}

function nudgeLine(i, key, delta) {
  const seg = S.edit.segs[i];
  const v = +(seg[key] + delta).toFixed(2);
  if (key === 'start') seg.start = Math.max(0, Math.min(v, seg.end - 0.2));
  else seg.end = Math.max(seg.start + 0.2, v);
  saveDraftSoon();
  relabelLines(i);
}

/** Refresh the time labels (the same line can be shown both in the main list and in a clip card). */
function relabelLines(i) {
  const sel = i == null ? '#view-review .line' : `#view-review .line[data-line="${i}"]`;
  for (const el of document.querySelectorAll(sel)) el.relabel?.();
}

/**
 * One editable caption line. `play(seg)` plays that part in the video next to it
 * (the whole video in the main list, the clip itself in a clip card).
 */
function lineRow(i, play, timeOf) {
  const seg = S.edit.segs[i];
  const label = () => {
    const shift = S.edit.shift || 0;
    const s = S.edit.segs[i];
    return timeOf ? timeOf(s) : `${fmtTime(s.start + shift)}〜${fmtTime(s.end + shift)}`;
  };
  const input = h('input', {
    value: seg.text,
    'data-i': i,
    placeholder: '(空のままだとこのテロップは出ません)',
    class: segChanged(i) ? 'changed' : '',
    oninput: (e) => {
      seg.text = e.target.value;
      e.target.classList.toggle('changed', segChanged(i));
      setActiveLine(i);
      saveDraftSoon();
    },
    onkeydown: (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.shiftKey)) { e.preventDefault(); splitLine(i, e.target.selectionStart); return; }
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const next = document.querySelector(`.lines input[data-i="${i + 1}"]`);
      if (next) { next.focus(); next.scrollIntoView({ block: 'nearest' }); }
    },
    onfocus: () => setActiveLine(i),
  });
  const nudge = (name, key) => h('span', { class: 'nudge' }, name,
    h('button', { class: 'icon', title: '0.1秒 早める', onclick: () => nudgeLine(i, key, -0.1) }, '−'),
    h('button', { class: 'icon', title: '0.1秒 遅らせる', onclick: () => nudgeLine(i, key, +0.1) }, '＋'));
  const time = h('button', { class: 't', title: 'この箇所を再生', onclick: () => play(S.edit.segs[i]) }, '▶ ' + label());
  const row = h('div', { class: 'line' + (i === activeLine ? ' active' : ''), 'data-line': i },
    h('div', { class: 'linetop' }, time, input),
    h('div', { class: 'tools' },
      h('button', { title: 'カーソルの位置で2つに分ける(Ctrl+Enter)', onclick: () => splitLine(i, input.selectionStart) }, '分割'),
      h('button', { title: 'この下に新しいテロップを入れる', onclick: () => insertAfter(i) }, '＋ 下に追加'),
      h('button', { class: 'danger', title: 'この行を消す', onclick: () => removeLine(i) }, '削除'),
      h('span', { style: 'flex:1' }),
      nudge('開始', 'start'), nudge('終了', 'end')));
  row.relabel = () => { time.textContent = '▶ ' + label(); };
  return row;
}

function setActiveLine(i) {
  if (activeLine === i) return;
  activeLine = i;
  for (const el of document.querySelectorAll('#view-review .line')) el.classList.toggle('active', +el.dataset.line === i);
  reviewEditor?.update();
}

function playIn(video, from, to) {
  if (!video) return;
  const box = video.closest('details.section');
  if (box && !box.open) box.open = true;
  video.currentTime = Math.max(0, from);
  stopAt = to;
  video.play().catch(() => {});
}

function renderReview() {
  const p = S.project;
  if (!p) return;
  detachVideos();
  const clips = p.clips.map((name, i) => ({ name, url: fileUrl(name), h: p.highlights[i] || {} })).filter((c) => c.url);
  const changedCount = S.edit.segs.filter((s, i) => segChanged(i)).length;
  const busy = S.job?.state === 'running';
  const shift = S.edit.shift || 0;
  const lead = p.lead || 0;     // できあがりの動画では、本編がオープニングの長さだけうしろにある
  const summary = [p.settings.do_transcribe && 'テロップ', p.settings.do_clip && `切り抜き(${p.settings.orientation === 'vertical' ? '縦' : '横'})`].filter(Boolean).join('・');

  reviewEditor = p.has_captioned ? captionEditor({
    styles: () => S.edit.style,
    hasClip: () => clips.length > 0,
    vertical: () => p.settings.orientation === 'vertical',
    titleOn: () => S.edit.showTitles,
    setTitleOn: p.settings.do_transcribe ? (on) => { S.edit.showTitles = on; saveDraftSoon(); renderReview(); } : null,
    titleSample: () => {
      const at = S.edit.segs[activeLine]?.start ?? 0;
      return S.edit.titles.find((t) => t.start <= at && at < t.end)?.text || '';
    },
    thumb: () => S.projectThumb,
    sample: () => S.edit.segs[activeLine]?.text || '',
    logo: (t) => (t === 'clip' ? S.edit.logoClip : S.edit.logo),
    saveLogo: (t, logo) => { if (t === 'clip') S.edit.logoClip = logo; else S.edit.logo = logo; saveDraftSoon(); },
    onChange: saveDraftSoon,
  }) : null;

  const video = p.has_captioned ? h('video', { id: 'fixVideo', controls: true, playsinline: true, disablePictureInPicture: true, controlsList: 'nofullscreen nodownload noremoteplayback', preload: 'metadata', src: fileUrl('captioned.mp4') }) : null;
  video?.addEventListener('timeupdate', () => {
    if (stopAt !== null && video.currentTime >= stopAt) {
      video.pause();
      stopAt = null;
    }
  });
  const playMain = (seg) => {
    document.querySelectorAll('.line.playing').forEach((r) => r.classList.remove('playing'));
    document.querySelector(`.line[data-line="${S.edit.segs.indexOf(seg)}"]`)?.classList.add('playing');
    playIn(video, seg.start + shift + lead, seg.end + shift + lead);
  };
  const lines = p.has_captioned ? h('div', { class: 'lines' }, S.edit.segs.map((s, i) => lineRow(i, playMain))) : null;

  // 話題の見出し。自動で作ったものをここで直せる
  const titlesBody = [
    h('p', { class: 'muted small' }, '空にした区間は出ません。概要欄で作った見出しを使うこともできます。'),
    p.title_note && h('div', { class: 'notice warn' }, p.title_note),
    S.edit.titles.length
      ? h('div', { class: 'lines titles' }, S.edit.titles.map((t) => h('div', { class: 'line' },
          h('div', { class: 'linetop' },
            h('button', {
              class: 't', title: 'この箇所を再生',
              onclick: () => playIn(video, t.start + shift + lead, Math.min(t.start + shift + 6, t.end + shift) + lead),
            }, `▶ ${fmtClock(t.start + shift)}〜${fmtClock(t.end + shift)}`),
            h('input', {
              value: t.text, placeholder: '(空にすると出しません)', disabled: !S.edit.showTitles,
              oninput: (e) => { t.text = e.target.value; saveDraftSoon(); reviewEditor?.update(); },
            })))))
      : h('div', { class: 'empty' }, '見出しはありません。下の「YouTube の概要欄」で作って入れられます。'),
  ];

  const timing = h('div', { class: 'timing' },
    h('label', {}, 'テロップ全体のタイミング'),
    h('input', {
      type: 'range', min: -1, max: 1, step: 0.05, value: shift,
      oninput: (e) => {
        S.edit.shift = +e.target.value;
        saveDraftSoon();
        document.querySelector('.timing .val').textContent = fmtShift(+e.target.value);
        relabelLines(null);
      },
    }),
    h('span', { class: 'val mono' }, fmtShift(shift)),
    h('p', { class: 'hint' }, 'テロップ全体を早める・遅らせます(＋で遅く)。作り直すと各行の時刻に入り、目盛りは0に戻ります。'));

  const logoOn = S.edit.logo.on && S.edit.logo.path;
  $('view-review').replaceChildren(h('div', { class: 'page wide' },
    h('div', { class: 'spread' },
      h('div', {},
        h('h1', {}, '確認・修正'),
        h('p', { class: 'muted' }, `${basename(p.source)} ・ ${fmtDate(p.created)} ・ ${summary}`)),
      h('div', { class: 'row' },
        h('button', { onclick: guard(() => showPlace(p.folder)) }, '結果フォルダを開く'))),
    p.status !== 'done' && h('div', { class: 'notice warn' },
      p.status === 'running' ? 'この処理はまだ実行中です。「3 処理」で進み具合を確認できます。'
        : `この処理は${STATUS_LABEL[p.status] || '途中で終了'}しています。できた所までの動画を確認できます。`),
    p.has_captioned && section('captions', 'テロップの確認・修正', {
      extra: changedCount > 0 && h('span', { class: 'chip review' }, `${changedCount} 行を修正中`),
    },
      h('p', { class: 'muted small' }, '時刻のボタンで再生します。行を選ぶと分割・追加・削除・時刻の微調整ができます(Ctrl+Enter で分割、Enter で次の行)。'),
      captionTools({
        project: p.name,
        segs: () => S.edit.segs,
        titles: () => S.edit.titles,
        onChange: () => { saveDraftSoon(); renderReview(); },
      }),
      h('div', { class: 'fix' }, video, lines),
      timing,
      h('details', { class: 'adv', open: false },
        h('summary', {}, logoOn ? `テロップ・見出し・ロゴの見た目を変える(ロゴ: ${basename(S.edit.logo.path)})` : 'テロップ・見出し・ロゴの見た目を変える'),
        h('div', { style: 'margin-top:12px' }, reviewEditor.el))),
    p.has_captioned && p.settings.do_transcribe && section('titles', '話題の見出し', {
      open: false,
      extra: [
        p.titles_by === 'ai' && h('span', { class: 'chip auto' }, 'AI で作成'),
        h('label', { class: 'check' },
          h('input', {
            type: 'checkbox', checked: S.edit.showTitles,
            onchange: (e) => { S.edit.showTitles = e.target.checked; saveDraftSoon(); renderReview(); },
          }), '見出しを出す'),
      ],
    }, ...titlesBody),
    p.segments?.length > 0 && section('chapters', 'YouTube の概要欄', {}, chaptersCard(p)),
    p.has_captioned && section('bookends', 'オープニング・エンディング', {
      open: false,
      extra: h('span', { class: 'muted small' }, [S.edit.intro && 'OP あり', S.edit.outro && 'ED あり'].filter(Boolean).join('・') || 'つながない'),
    }, ...bookendBody()),
    clips.length > 0 && section('clips', '切り抜き候補', { open: false, extra: h('span', { class: 'muted small' }, `${clips.length} 本`) },
      h('p', { class: 'muted small' }, '動画の中の時間順です。スコアの内訳が選ばれた理由です。'
        + (p.has_captioned ? 'テロップはその場で直せます。' : '')),
      h('div', { class: 'clips' }, clips.map((c, i) => {
        const from = c.h.start || 0, to = c.h.end || 0;
        const clipVideo = h('video', { controls: true, playsinline: true, disablePictureInPicture: true, controlsList: 'nofullscreen nodownload noremoteplayback', preload: 'metadata', src: c.url });
        clipVideo.addEventListener('timeupdate', () => {
          if (clipVideo.dataset.stop && clipVideo.currentTime >= +clipVideo.dataset.stop) {
            clipVideo.pause();
            clipVideo.removeAttribute('data-stop');
          }
        });
        const inClip = S.edit.segs.map((s, j) => ({ s, j })).filter(({ s }) => s.end + shift > from && s.start + shift < to);
        const playClip = (seg) => {
          clipVideo.currentTime = Math.max(0, seg.start + shift - from);
          clipVideo.dataset.stop = Math.max(0, seg.end + shift - from);
          clipVideo.play().catch(() => {});
        };
        return h('div', { class: 'clipcard' },
          h('div', { class: 'head' }, h('b', {}, `候補 ${i + 1}`), h('span', { class: 'mono small' }, `スコア ${c.h.score ?? '-'}`)),
          clipVideo,
          h('div', { class: 'mono small muted' }, `${fmtClock(from)} 〜 ${fmtClock(to)}(${Math.round(to - from)}秒)`),
          c.h.text_sample && h('div', { class: 'sample' }, c.h.text_sample),
          h('div', { class: 'score-bars' }, Object.entries(c.h.components || {}).map(([k, v]) =>
            h('div', {}, h('span', {}, k), h('i', {}, h('b', { style: `width:${Math.round(v * 100)}%` })), h('span', { class: 'mono' }, v.toFixed(2))))),
          c.h.reason && h('div', { class: 'reason' }, c.h.reason),
          h('div', { class: 'kws' }, Object.entries(c.h.matched_keywords || {}).flatMap(([g, ws]) => ws.map((w) => h('span', {}, `${g}: ${w}`)))),
          p.has_captioned && h('details', { class: 'adv cliplines' },
            h('summary', {}, `この切り抜きのテロップ(${inClip.length}行)`),
            inClip.length
              ? h('div', { class: 'lines' }, inClip.map(({ j }) => lineRow(j, playClip,
                  (s) => `${fmtTime(Math.max(0, s.start + shift - from))}〜${fmtTime(Math.max(0, s.end + shift - from))}`)))
              : h('div', { class: 'empty' }, 'この範囲にテロップはありません')),
          h('div', { class: 'row' }, h('button', { class: 'ghost', onclick: guard(() => showPlace(`${p.folder}\\${c.name}`)) }, '場所を表示')));
      }))),
    section('files', 'できたファイル', { open: false, extra: h('span', { class: 'muted small' }, `${p.files.length} 個`) },
      p.files.length
        ? h('ul', { class: 'filelist' }, p.files.map((f) => h('li', {},
            h('span', {}, h('b', {}, f.name), ' ', h('span', { class: 'muted small' }, fmtSize(f.size))),
            h('button', { class: 'ghost', onclick: guard(() => showPlace(f.path)) }, '場所を表示'))))
        : h('div', { class: 'empty' }, 'まだファイルはありません'),
      h('p', { class: 'muted small' }, `保存場所: ${p.folder}`)),
    p.has_captioned && h('div', { class: 'rebuild-bar', id: 'rebuildBar' })));
  refreshRebuildBar();
  reviewEditor?.update();
}

/* ---------- 確認・修正:開け閉めできる欄と、まとめて作り直すところ ---------- */

const OPEN_KEY = 'video-clipper-sections';

function sectionOpen(id, fallback) {
  try {
    const saved = JSON.parse(localStorage.getItem(OPEN_KEY) || '{}');
    return id in saved ? !!saved[id] : fallback;
  } catch {
    return fallback;
  }
}

function rememberOpen(id, open) {
  try {
    const saved = JSON.parse(localStorage.getItem(OPEN_KEY) || '{}');
    saved[id] = open;
    localStorage.setItem(OPEN_KEY, JSON.stringify(saved));
  } catch { /* 覚えられなくても開け閉めはできる */ }
}

/** 見出しを押すと開け閉めできる欄。開け閉めは次に開いたときも覚えている */
function section(id, title, { open = true, extra = null, onopen = null } = {}, ...body) {
  const el = h('details', { class: 'card section', 'data-section': id, open: sectionOpen(id, open) },
    h('summary', {}, h('h3', {}, title), h('span', { class: 'sec-extra' }, extra)),
    h('div', { class: 'sec-body' }, ...body));
  el.addEventListener('toggle', () => {
    rememberOpen(id, el.open);
    if (el.open) onopen?.();
  });
  return el;
}

const bookendChanged = (p) => (S.edit.intro || '') !== (p.settings.intro || '') || (S.edit.outro || '') !== (p.settings.outro || '');

function logoChanged(p) {
  const sig = (l) => {
    const x = { ...LOGO_DEFAULT, ...(l || {}) };
    return x.on && x.path ? JSON.stringify([x.path, x.x, x.y, x.w]) : 'off';
  };
  return sig(S.edit.logo) !== sig(p.settings.logo)
    || sig(S.edit.logoClip) !== sig(p.settings.logo_clip || p.settings.logo);
}

/** まだ動画に入っていない変更(作り直すと入る) */
function pendingChanges(p) {
  const out = [];
  const lines = S.edit.segs.filter((s, i) => segChanged(i)).length;
  if (lines) out.push(`テロップ ${lines} 行`);
  if (S.edit.shift) out.push('タイミング');
  if (JSON.stringify(S.edit.style) !== JSON.stringify(p.style)) out.push('テロップの見た目');
  if (S.edit.showTitles !== !!p.settings.show_titles || titlesSig(S.edit.titles) !== titlesSig(p.titles || [])) out.push('話題の見出し');
  if (logoChanged(p)) out.push('ロゴ');
  if (bookendChanged(p)) out.push('オープニング・エンディング');
  return out;
}

/** 画面の下の「作り直す」。直した内容をまとめて動画に入れる */
function refreshRebuildBar() {
  const bar = $('rebuildBar');
  const p = S.project;
  if (!bar || !p) return;
  const busy = S.job?.state === 'running';
  const changes = pendingChanges(p);
  const hasClips = p.clips.length > 0;
  bar.replaceChildren(
    h('div', { class: 'rb-changes' },
      changes.length
        ? [h('b', {}, 'まだ動画に入っていない変更'), ...changes.map((c) => h('span', { class: 'chip review' }, c))]
        : h('span', { class: 'muted small' }, 'まだ動画に入っていない変更はありません')),
    h('div', { class: 'rb-actions' },
      h('b', {}, '作り直す対象'),
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: S.edit.main, onchange: (e) => { S.edit.main = e.target.checked; } }), '元動画'),
      hasClips && h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: S.edit.clip, onchange: (e) => { S.edit.clip = e.target.checked; } }), '切り抜き動画'),
      h('span', { style: 'flex:1' }),
      !p.can_fix && h('span', { class: 'bad-text small' }, '作業用の動画が見つからないため作り直せません'),
      h('button', { class: 'primary big', disabled: busy || !p.can_fix, onclick: guard(startReburn) }, 'この内容で作り直す ▶')));
}

/** オープニング・エンディングを選び直す欄の中身 */
function bookendBody() {
  const busy = S.job?.state === 'running';
  const row = (kind, label, note) => {
    const path = S.edit[kind] || '';
    return h('div', { class: 'bookend' + (path ? ' on' : '') },
      h('div', { class: 'be-name' }, h('b', {}, label), h('span', { class: 'muted small' }, path ? basename(path) : note)),
      h('div', { class: 'row' },
        h('button', {
          disabled: busy,
          onclick: guard(async () => {
            const r = await api('/api/pick', { kind });
            if (!r.path) return;
            S.edit[kind] = r.path;
            saveDraftSoon();
            renderReview();
          }),
        }, path ? '選び直す' : '動画を選ぶ'),
        path && h('button', { class: 'ghost', disabled: busy, onclick: () => { S.edit[kind] = ''; saveDraftSoon(); renderReview(); } }, '外す')));
  };
  return [
    h('p', { class: 'muted small' }, '元動画の前後につなぐ動画です(切り抜きには付きません)。'),
    h('div', { class: 'bookends' },
      row('intro', 'オープニング', '本編の前につなぎます'),
      row('outro', 'エンディング', '本編を徐々に暗くしてから、後ろにつなぎます')),
  ];
}

/* ---------- YouTube の概要欄(チャプター) ---------- */

// 作り方は「下書き / AI / 貼り付け」の3つ。どれで作っても、ここで直してから概要欄に貼る。
// 時刻は、できあがった動画(オープニングをつないだ後)の時刻。
const CH = { project: '', data: null, error: '', busy: '', saveTimer: 0 };
const CH_SOURCE = { draft: ['下書き', 'still'], ai: ['AI で作成', 'auto'], paste: ['貼り付け', 'auto'], edit: ['手で修正', 'manual'] };
const CH_MIN_GAP = 10;

function hms(sec) {
  const s = Math.max(0, Math.round(sec));
  const p2 = (n) => String(n).padStart(2, '0');
  return `${p2(Math.floor(s / 3600))}:${p2(Math.floor(s / 60) % 60)}:${p2(s % 60)}`;
}

function parseHms(text) {
  const parts = String(text || '').trim().replace(/：/g, ':').split(':');
  if (!parts.length || parts.length > 3 || !parts.every((x) => /^\d+$/.test(x.trim()))) return null;
  return parts.reduce((acc, x) => acc * 60 + Number(x), 0);
}

function chapterText(d) {
  const parts = [];
  const topics = d.topics.filter((t) => t.trim());
  if (topics.length) parts.push('▼主なトピック\n' + topics.join(' / '));
  const rows = d.chapters.filter((c) => c.title.trim());
  if (rows.length) parts.push(rows.map((c) => `${hms(c.start)} - ${c.title.trim()}`).join('\n'));
  return parts.join('\n\n');
}

// 画面の見出しは、この長さ(全角)までなら焼き込んだときに必ず1行に収まる
const LABEL_MAX = 16;

function labelWidth(text) {
  let w = 0;
  for (const ch of String(text || '')) {
    const cp = ch.codePointAt(0);
    w += cp < 0x2000 || (cp >= 0xFF61 && cp <= 0xFF9F) ? 0.5 : 1;
  }
  return w;
}

const labelTooLong = (text) => labelWidth(text) > LABEL_MAX;

/** 長すぎる画面の見出し(本編の行だけ) */
const longLabels = (chapters) => chapters.filter((c) => (c.kind || 'body') === 'body' && labelTooLong(c.label));

function chapterProblems(chapters) {
  const out = [];
  if (chapters.length < 3) out.push(`YouTube は見出しが3つ以上ないとチャプターにしません(いま${chapters.length}つ)。`);
  if (chapters.length && chapters[0].start !== 0) out.push('1つ目の見出しは 00:00:00 にしてください。');
  chapters.forEach((c, i) => {
    const next = chapters[i + 1];
    if (next && next.start <= c.start) out.push(`「${next.title || '(空)'}」の時刻が、前の見出しより前になっています。`);
    else if (next && next.start - c.start < CH_MIN_GAP) out.push(`「${c.title || '(空)'}」が10秒より短くなっています。`);
  });
  if (chapters.some((c) => !c.title.trim())) out.push('見出しが空の行があります。');
  for (const c of longLabels(chapters)) {
    out.push(`画面の見出し「${c.label}」が長すぎます(${labelWidth(c.label)}文字)。${LABEL_MAX}文字以内に直すと1行に収まります。`);
  }
  return out;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // スマホから(暗号化していない通信で)開いているときは、こちらの方法でしか写せない
    const area = h('textarea', { style: 'position:fixed;left:-9999px;top:0' });
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    area.remove();
    return ok;
  }
}

/** 自動で写せなかったときに、選んで写せるように見せる */
function showCopyBox(title, text) {
  const modal = $('modal');
  const area = h('textarea', { class: 'mono', rows: 12, readonly: true, style: 'width:100%' });
  area.value = text;
  const close = () => { modal.hidden = true; modal.replaceChildren(); };
  modal.replaceChildren(h('div', { class: 'mbox wide', role: 'dialog', 'aria-modal': 'true' },
    h('h3', {}, title),
    h('p', { class: 'muted small' }, '自動でコピーできませんでした。下の文を長押し(または Ctrl+A)で選んでコピーしてください。'),
    area,
    h('div', { class: 'mbtns' }, h('button', { class: 'primary', onclick: close }, '閉じる'))));
  modal.hidden = false;
  area.focus();
  area.select();
}

async function loadChapters(name) {
  CH.project = name;
  CH.data = null;
  CH.error = '';
  try {
    CH.data = await api('/api/chapters', { project: name });
  } catch (e) {
    CH.error = e.message;
  }
  if (CH.project === name) renderChapters();     // 結果を開く前(見出し待ち)でも描く
}

function setChapters(data) {
  CH.data = data;
  renderChapters();
}

/** 手で直したら、少し待ってから保存する(その間も、貼る文と注意はすぐ変える) */
function chapterEdited() {
  const d = CH.data;
  d.source = 'edit';
  refreshChapterOutput();
  clearTimeout(CH.saveTimer);
  const name = CH.project;
  CH.saveTimer = setTimeout(() => {
    api('/api/chapters/save', { project: name, topics: d.topics, chapters: d.chapters })
      .then((saved) => { if (CH.project === name) { d.problems = saved.problems; } })
      .catch(() => {});
  }, 700);
}

function refreshChapterOutput() {
  const box = $('chapCard');
  if (!box || !CH.data) return;
  const out = box.querySelector('.chapout textarea');
  if (out) out.value = chapterText(CH.data);
  const probs = chapterProblems(CH.data.chapters);
  box.querySelector('.chapprobs')?.replaceChildren(...(probs.length
    ? [h('div', { class: 'notice warn' }, h('div', {}, probs.map((t) => h('div', {}, t))))] : []));
  const [label, kind] = CH_SOURCE[CH.data.source] || CH_SOURCE.edit;
  const chip = box.querySelector('.chapsource');
  if (chip) { chip.className = `chip ${kind} chapsource`; chip.textContent = label; }
}

function seekChapter(sec) {
  const video = $('fixVideo');
  if (!video) return;
  stopAt = null;
  video.currentTime = sec;
  video.play().catch(() => {});
}

async function makeChaptersWithAi() {
  const d = CH.data;
  if (d.source === 'edit' || d.source === 'paste' || d.source === 'ai') {
    const go = await ask('AI で作り直しますか?', '今の見出しは、AI が作ったものに置き換わります。',
      [{ label: '作り直す', value: true, primary: true }, { label: 'やめる', value: false }]);
    if (!go) return;
  }
  const name = CH.project;
  CH.busy = 'AI が見出しを考えています(1分ほどかかります)…';
  renderChapters();
  try {
    const data = await api('/api/chapters/ai', { project: name });
    if (CH.project === name) {
      CH.data = data;
      toast('AI で見出しを作りました。おかしい所は直してから使ってください。', 'ok');
      setTimeout(offerChapterTitles, 0);
    }
  } finally {
    CH.busy = '';
    if (CH.project === name) renderChapters();
  }
}

// これより長い依頼文は、無料版の ChatGPT などでは途中までしか読まれないおそれがある
// (35分の回で約1.1万字。無料版が一度に読める量は2万字台と見て、余裕を持たせた目安)
const LONG_PROMPT = 18000;
const CHAT_SITES = { chatgpt: 'https://chatgpt.com/', claude: 'https://claude.ai/new' };

async function copyChapterPrompt() {
  const { text } = await api('/api/chapters/prompt', { project: CH.project });
  if (!(await copyText(text))) return showCopyBox('AI への依頼文', text);
  const long = text.length > LONG_PROMPT
    ? `\n\n※ 依頼文が長めです(約${Math.round(text.length / 1000)}千字)。無料版の ChatGPT などでは途中までしか読まれず、`
      + '後半の見出しが抜けることがあります。抜けたときは、有料版や「Thinking」系のモデルを使うか、'
      + 'Claude(claude.ai、または「AI で作る」)をお使いください。'
    : '';
  const next = await ask('依頼文をコピーしました',
    '1. ChatGPT や claude.ai を開いて、新しいチャットに貼り付けて送ります\n'
    + '2. 返事が来たら、その文をまるごとコピーします\n'
    + '3. ここに戻って「返事を貼り付け」を押します' + long,
    [{ label: 'ChatGPT を開く', value: 'chatgpt', primary: true },
     { label: 'claude.ai を開く', value: 'claude' },
     { label: '閉じる', value: null }]);
  if (CHAT_SITES[next]) window.open(CHAT_SITES[next], '_blank', 'noopener');
}

function pasteChapterReply() {
  const modal = $('modal');
  const area = h('textarea', { rows: 12, placeholder: '▼主なトピック\n…\n\n0:00 - 見出し\n2:51 - 見出し', style: 'width:100%' });
  const message = h('p', { class: 'bad-text small' });
  const close = () => { modal.hidden = true; modal.replaceChildren(); };
  const load = guard(async () => {
    message.textContent = '';
    try {
      const data = await api('/api/chapters/paste', { project: CH.project, text: area.value });
      close();
      setChapters(data);
      toast(`${data.chapters.length}個の見出しを読み込みました`, 'ok');
      offerChapterTitles();
    } catch (e) {
      message.textContent = e.message;
    }
  });
  modal.replaceChildren(h('div', { class: 'mbox wide', role: 'dialog', 'aria-modal': 'true' },
    h('h3', {}, 'AI の返事を貼り付け'),
    h('p', { class: 'muted small' }, 'ChatGPT や claude.ai から返ってきた文を、そのまま貼り付けてください。前置きの文が混ざっていても、時刻つきの行だけを読み取ります。'),
    area, message,
    h('div', { class: 'mbtns' },
      h('button', { class: 'outline', onclick: close }, 'やめる'),
      h('button', { class: 'primary', onclick: load }, '読み込む'))));
  modal.hidden = false;
  area.focus();
  // ChatGPT などでコピーしてきた返事が入っていれば、貼る手間を省く(読めないときは何もしない)
  navigator.clipboard?.readText?.().then((clip) => {
    if (!area.value && /\d{1,2}:\d{2}/.test(clip || '') && !clip.includes('--- 文字起こし ---')) {
      area.value = clip;
      message.textContent = '';
      area.after(h('p', { class: 'hint pasted' }, 'コピーしてあった返事を入れました。よければ「読み込む」を押してください。'));
    }
  }).catch(() => {});
}

function chapterKeyDialog() {
  const modal = $('modal');
  const has = !!CH.data?.has_key;
  const input = h('input', { type: 'password', autocomplete: 'off', placeholder: has ? '(登録済み。入れ直すときだけ入力)' : 'sk-ant-…', style: 'width:100%' });
  const message = h('p', { class: 'bad-text small' });
  const close = () => { modal.hidden = true; modal.replaceChildren(); };
  const save = guard(async (value) => {
    message.textContent = '';
    try {
      const r = await api('/api/apikey', { key: value });
      close();
      if (CH.data) CH.data.has_key = r.has_key;
      S.hasKey = r.has_key;
      renderChapters();
      toast(r.has_key ? 'API キーを保存しました' : 'API キーを消しました', 'ok');
    } catch (e) {
      message.textContent = e.message;
    }
  });
  modal.replaceChildren(h('div', { class: 'mbox', role: 'dialog', 'aria-modal': 'true' },
    h('h3', {}, 'API キーの設定'),
    h('p', {}, 'Claude の API キーを入れると、「AI で作る」ボタン1つで見出しを作れます。'),
    h('ul', { class: 'muted small keynotes' },
      h('li', {}, 'キーは Claude Console(console.anthropic.com)の「API Keys」で作れます。'),
      h('li', {}, '使った分だけ料金がかかります(35分の回で1本あたり約20〜30円)。'),
      h('li', {}, 'AI に送るのは文字起こしの文字だけです。動画や音声は送りません。'),
      h('li', {}, 'キーはこの PC の中(data フォルダ)にだけ保存し、画面には表示しません。')),
    input, message,
    h('div', { class: 'mbtns' },
      has && h('button', { class: 'danger', onclick: () => save('') }, 'キーを消す'),
      h('button', { class: 'outline', onclick: close }, 'やめる'),
      h('button', { class: 'primary', onclick: () => save(input.value) }, '保存'))));
  modal.hidden = false;
  input.focus();
}

const CH_KIND_NOTE = { intro: 'オープニングの動画', outro: 'エンディングの動画' };

function chapterRow(c, i) {
  const d = CH.data;
  const kind = c.kind || 'body';
  const bodyRows = d.chapters.filter((x) => (x.kind || 'body') === 'body');
  const firstBody = bodyRows[0] === c;
  // オープニング・エンディングの時刻は、つないだ動画の長さで決まる。本編の1つ目は本編の頭に固定
  const locked = kind !== 'body' || firstBody;
  const time = h('input', {
    class: 'mono ctime', value: hms(c.start), inputmode: 'numeric', 'aria-label': '時刻', disabled: locked,
    title: kind !== 'body' ? `${CH_KIND_NOTE[kind]}の時刻です(つないだ動画の長さで決まります)`
      : firstBody ? '本編の始まりです' : '例: 2:51 / 00:02:51',
    onchange: (e) => {
      const sec = parseHms(e.target.value);
      e.target.classList.toggle('bad', sec === null);
      if (sec === null) return;
      c.start = sec;
      sortChapters(d);
      chapterEdited();
      renderChapters();
    },
  });
  return h('div', { class: 'chapline' },
    h('button', { class: 'ghost seek', title: 'この箇所から再生', onclick: () => seekChapter(c.start) }, '▶'),
    time,
    h('input', {
      class: 'ctitle', value: c.title, placeholder: '見出し(概要欄)', 'aria-label': '見出し(概要欄)',
      oninput: (e) => { c.title = e.target.value; chapterEdited(); },
    }),
    kind === 'body'
      ? h('input', {
        class: 'clabel' + (labelTooLong(c.label) ? ' long' : ''), value: c.label || '',
        placeholder: '画面の見出し', 'aria-label': '画面の見出し',
        title: `動画に出す短い見出し(${LABEL_MAX}文字以内で1行に収まります)`,
        oninput: (e) => {
          c.label = e.target.value;
          e.target.classList.toggle('long', labelTooLong(c.label));
          chapterEdited();
        },
      })
      : h('span', { class: 'clabel cnone', title: '画面の見出しは本編だけに出します' }, CH_KIND_NOTE[kind]),
    h('button', {
      class: 'ghost del', title: kind === 'body' ? 'この見出しを消す' : `${CH_KIND_NOTE[kind]}をチャプターにしない`,
      disabled: kind === 'body' && bodyRows.length <= 1,
      onclick: () => { d.chapters.splice(i, 1); chapterEdited(); renderChapters(); },
    }, '×'));
}

/** 時刻の順に並べる(オープニングはいつも先頭、エンディングはいつも最後) */
function sortChapters(d) {
  const order = { intro: 0, body: 1, outro: 2 };
  d.chapters.sort((a, b) => (order[a.kind || 'body'] - order[b.kind || 'body']) || (a.start - b.start));
}

function addChapterRow() {
  const d = CH.data;
  const video = $('fixVideo');
  const body = d.chapters.filter((c) => (c.kind || 'body') === 'body');
  const outro = d.chapters.find((c) => c.kind === 'outro');
  const last = body[body.length - 1];
  // 再生中ならその位置、そうでなければ最後の見出しの1分後に足す(エンディングより前)
  let at = video && video.currentTime > 0 ? Math.floor(video.currentTime) : (last ? last.start + 60 : 0);
  if (outro && at >= outro.start - CH_MIN_GAP) at = Math.max((last?.start ?? 0) + CH_MIN_GAP, outro.start - CH_MIN_GAP * 3);
  while (d.chapters.some((c) => Math.abs(c.start - at) < CH_MIN_GAP)) at += CH_MIN_GAP;
  const added = { start: d.chapters.length ? at : 0, title: '', label: '', kind: 'body' };
  d.chapters.push(added);
  sortChapters(d);
  chapterEdited();
  renderChapters();
  const idx = d.chapters.indexOf(added);
  $('chapCard')?.querySelectorAll('.chapline')[idx]?.querySelector('.ctitle')?.focus();
}

/** 概要欄の見出しから、画面に焼き込む見出し(本編の時刻)を作る */
function titlesFromChapters(d, p) {
  const offset = d.offset || 0, bodyEnd = p.duration || (p.segments.at(-1)?.end ?? 0);
  const rows = d.chapters.filter((c) => (c.kind || 'body') === 'body' && c.title.trim());
  return rows.map((c, i) => ({
    start: Math.max(0, c.start - offset),
    end: Math.min(bodyEnd, rows[i + 1] ? rows[i + 1].start - offset : bodyEnd),
    text: (c.label || '').trim() || c.title.trim(),
  })).filter((t) => t.end - t.start >= 1);
}

const titlesSig = (titles) => JSON.stringify(titles.filter((t) => t.text.trim()).map((t) => [Math.round(t.start), t.text.trim()]));

/** 確認・修正で開いている結果の概要欄で、画面の見出しがまだ動画の見出しに入っていないか */
function chaptersNotApplied() {
  const p = S.project, d = CH.data;
  if (S.step !== 'review' || !p?.has_captioned || !d || CH.project !== p.name || d.source === 'draft') return null;
  const want = titlesFromChapters(d, p);
  if (!want.length || titlesSig(want) === titlesSig(S.edit.titles)) return null;
  return want;
}

function applyChapterTitles(want) {
  S.edit.titles = want;
  S.edit.showTitles = true;
  saveDraftSoon();
  renderReview();
}

/** 返事を貼った・AI で作った直後に、画面の見出しにも入れるか聞く */
async function offerChapterTitles() {
  const want = chaptersNotApplied();
  if (!want) return;
  const go = await ask('画面の見出しも、この内容にしますか?',
    `概要欄の「画面の見出し」(${want.length}個)を、動画に出す見出しにも入れます。`
    + '動画に反映するには、このあと「この内容で作り直す」を押してください。',
    [{ label: '入れる', value: true, primary: true }, { label: 'あとで', value: false }]);
  if (!go) return;
  applyChapterTitles(want);
  toast('画面の見出しに入れました。「この内容で作り直す」で動画に反映されます。', 'ok');
}

async function useChaptersForTitles() {
  const p = S.project, d = CH.data;
  const rows = d.chapters.filter((c) => (c.kind || 'body') === 'body' && c.title.trim());
  if (!rows.length) return toast('見出しがありません', 'warn');
  const long = longLabels(d.chapters);
  if (long.length) {
    const fix = await ask('画面の見出しが長すぎます',
      `${long.map((c) => `「${c.label}」(${labelWidth(c.label)}文字)`).join('、')} は、2行になることがあります。`
      + `${LABEL_MAX}文字以内に直すと1行に収まります。`,
      [{ label: '直す', value: true, primary: true }, { label: 'このまま進める', value: false }]);
    if (fix) {
      $('chapCard')?.querySelector('.clabel.long')?.focus();
      return;
    }
  }

  const go = await ask('画面の見出しを置き換えますか?',
    `今の「話題の見出し」を、ここの「画面の見出し」(${rows.length}個)に置き換えます。`
    + '動画に反映するには、このあと「この内容で作り直す」を押してください。',
    [{ label: '置き換える', value: true, primary: true }, { label: 'やめる', value: false }]);
  if (!go) return;
  applyChapterTitles(titlesFromChapters(d, p));
  toast('画面の見出しを置き換えました。「この内容で作り直す」で動画に反映されます。', 'ok');
}

function chaptersCard(p) {
  const card = h('div', { id: 'chapCard', style: 'display:grid;gap:12px' });
  if (CH.project !== p.name) loadChapters(p.name);
  setTimeout(renderChapters);
  return card;
}

function renderChapters() {
  const card = $('chapCard');
  if (!card) return;
  const d = CH.data;
  const head = h('div', { class: 'card-head' },
    h('div', {},
      h('p', { class: 'muted small' }, '「概要欄に貼る文」をコピーして、YouTube の概要欄に貼ってください。')),
    d && h('span', { class: 'chapsource' }));
  if (!d) {
    card.replaceChildren(head, CH.error ? h('div', { class: 'empty' }, CH.error) : h('div', { class: 'empty' }, '読み込んでいます…'));
    return;
  }
  const busy = !!CH.busy;
  const actions = h('div', { class: 'chapactions' },
    h('button', { class: 'primary', disabled: busy, onclick: guard(copyChapterPrompt) }, '1. 依頼文をコピー'),
    h('button', { class: 'primary', disabled: busy, onclick: pasteChapterReply }, '2. 返事を貼り付け'),
    d.has_key && h('button', { disabled: busy, onclick: guard(makeChaptersWithAi) }, 'AI で作る(API)'),
    h('span', { style: 'flex:1' }),
    d.source !== 'draft' && h('button', {
      class: 'ghost', disabled: busy,
      onclick: guard(async () => {
        const go = await ask('下書きに戻しますか?', '今の見出しを消して、話題の見出しから作った下書きに戻します。',
          [{ label: '戻す', value: true, danger: true }, { label: 'やめる', value: false }]);
        if (go) setChapters(await api('/api/chapters/reset', { project: CH.project }));
      }),
    }, '下書きに戻す'),
    h('button', { class: 'ghost small', disabled: busy, onclick: chapterKeyDialog }, d.has_key ? 'API キー' : 'API を使う'));
  const note = h('p', { class: 'hint' },
    '依頼文を ChatGPT などに貼って送り、返事を「返事を貼り付け」で読み込みます。'
    + (d.has_key ? '「AI で作る(API)」は文字起こしの文字だけを送ります(35分の回で約20〜30円)。' : ''));

  const topics = h('input', {
    value: d.topics.join(' / '), placeholder: 'トピック / トピック / トピック', 'aria-label': '主なトピック',
    oninput: (e) => { d.topics = e.target.value.split(/\s*[/／]\s*/).map((t) => t.trim()).filter(Boolean); chapterEdited(); },
  });
  const editor = h('div', { class: 'chapedit' },
    h('label', { class: 'chaplabel' }, '主なトピック(「 / 」で区切る)'), topics,
    h('div', { class: 'chaphead' },
      h('span', {}), h('span', {}, '時刻'), h('span', {}, `見出し(${d.chapters.length}個)`), h('span', {}, '画面の見出し'), h('span', {})),
    h('div', { class: 'chaplines' }, d.chapters.map(chapterRow)),
    h('button', { class: 'ghost addrow', onclick: addChapterRow }, '＋ 見出しを足す'),
    (d.offset > 0 || d.outro > 0) && h('p', { class: 'hint' },
      'オープニング・エンディング込みの時刻です。'),
    S.step === 'review' && S.project?.has_captioned && S.project.name === CH.project && h('div', { class: 'useforoverlay' },
      h('button', { disabled: busy, onclick: useChaptersForTitles }, '画面の見出しに使う'),
      h('span', { class: 'hint' }, '「画面の見出し」の列を、動画に出す見出しにします。')));
  const output = h('div', { class: 'chapout' },
    h('div', { class: 'spread' },
      h('label', { class: 'chaplabel' }, '概要欄に貼る文'),
      h('button', {
        class: 'primary', onclick: guard(async () => {
          const text = chapterText(d);
          if (await copyText(text)) toast('コピーしました。YouTube の概要欄に貼り付けてください。', 'ok');
          else showCopyBox('概要欄に貼る文', text);
        }),
      }, 'コピー')),
    h('textarea', { class: 'mono', rows: 16, readonly: true }),
    h('div', { class: 'chapprobs' }));

  card.replaceChildren(...[head, actions, note,
    busy && h('div', { class: 'notice' }, h('span', { class: 'spinner' }), CH.busy),
    h('div', { class: 'chapgrid' + (busy ? ' busy' : '') }, editor, output)].filter(Boolean));
  refreshChapterOutput();
  RP.refresh?.();
}

async function startReburn() {
  const p = S.project;
  // オープニング・エンディングを替えたときは、元動画を作り直さないと入らない
  if (bookendChanged(p) && !S.edit.main) {
    S.edit.main = true;
    toast('オープニング・エンディングを替えたので、元動画も作り直します');
  }
  const targets = [S.edit.main && 'main', S.edit.clip && p.clips.length && 'clip'].filter(Boolean);
  if (!targets.length) return toast('作り直す対象を1つ以上選んでください', 'warn');
  const segments = editedSegs();
  if (!segments.length) return toast('テロップの文字がすべて空です', 'warn');
  // 概要欄で作った画面の見出しが、まだ動画の見出しに入っていないまま作り直さないように
  const want = chaptersNotApplied();
  if (want && titlesSig(want) !== CH.keepSig) {
    const ans = await ask('画面の見出しが、概要欄と違います',
      '概要欄の「画面の見出し」が、まだ動画の見出しに入っていません。入れてから作り直しますか?',
      [{ label: '入れてから作り直す', value: 'apply', primary: true },
       { label: '今の見出しのまま', value: 'keep' },
       { label: 'やめる', value: null }]);
    if (!ans) return;
    if (ans === 'apply') applyChapterTitles(want);
    else CH.keepSig = titlesSig(want);      // 同じ内容では、もう聞かない
  }
  detachVideos();  // the files being rewritten must not be held open by the players
  await api('/api/reburn', {
    project: p.name, segments, style: S.edit.style, targets,
    titles: S.edit.titles.filter((t) => t.text.trim()), show_titles: S.edit.showTitles, logo: S.edit.logo, logo_clip: S.edit.logoClip,
    intro: S.edit.intro || '', outro: S.edit.outro || '',
  });
  S.job = { state: 'running', kind: 'reburn', project: p.name, steps: [], step: -1, percent: 0, message: '準備しています…' };
  setStep('run');
  poll();
}

/* ---------- テロップをまとめて直す:一括置換・AI の誤字の候補・直し方の辞書 ---------- */
//
// 確認・修正の画面と、焼き込む前に止まっている画面の両方で使う。
// ctx: { project: 結果の名前, segs(): 直すテロップの行({text}), titles(): 画面の見出し(無ければ null),
//        onChange(): 直したあとに画面を描き直す }

const RP = { find: '', repl: '', scope: { segs: true, titles: true, chapters: true }, remember: false, undo: null };
const FX = { entries: null };

const textField = (obj, k) => ({ get: () => String(obj[k] ?? ''), set: (v) => { obj[k] = v; } });

/** 置換できるもの(テロップ・画面の見出し・概要欄)ごとの、文字の入れ物 */
function replaceTargets(ctx) {
  const out = [{ key: 'segs', label: 'テロップ', unit: '行', items: ctx.segs().map((s) => textField(s, 'text')) }];
  const titles = ctx.titles?.();
  if (titles) out.push({ key: 'titles', label: '画面の見出し', unit: '個', items: titles.map((t) => textField(t, 'text')) });
  const d = CH.project === ctx.project ? CH.data : null;
  if (d) {
    out.push({
      key: 'chapters', label: '概要欄', unit: 'か所',
      items: [...d.topics.map((t, i) => textField(d.topics, i)), ...d.chapters.flatMap((c) => [textField(c, 'title'), textField(c, 'label')])],
    });
  }
  return out;
}

/** 文の中の「誤」を、消した形と直した形で見せる */
function showFix(text, wrong, right) {
  const at = text.indexOf(wrong);
  if (at < 0) return [h('del', {}, wrong), ' → ', h('ins', {}, right)];
  return [text.slice(0, at), h('del', {}, wrong), h('ins', {}, right), text.slice(at + wrong.length)];
}

function afterFix(ctx, touchedChapters) {
  if (touchedChapters && CH.data) {
    chapterEdited();
    renderChapters();
  }
  ctx.onChange();
}

function rememberFixes(pairs) {
  return api('/api/fixes/add', { entries: pairs })
    .then((r) => { FX.entries = r.entries; toast(`辞書に ${pairs.length} 個覚えました。次の文字起こしから自動で直します。`, 'ok'); })
    .catch((e) => toast(e.message, 'bad'));
}

function doReplace(ctx) {
  const find = RP.find;
  if (!find) return toast('探す言葉を入れてください', 'warn');
  if (find === RP.repl) return toast('探す言葉と置き換える言葉が同じです', 'warn');
  const undo = [];
  let touchedChapters = false;
  for (const t of replaceTargets(ctx).filter((x) => RP.scope[x.key])) {
    for (const f of t.items) {
      const v = f.get();
      if (!v.includes(find)) continue;
      undo.push([f, v]);
      f.set(v.split(find).join(RP.repl));
      if (t.key === 'chapters') touchedChapters = true;
    }
  }
  if (!undo.length) return toast('見つかりませんでした', 'warn');
  RP.undo = { items: undo, chapters: touchedChapters, note: `「${find}」→「${RP.repl}」` };
  if (RP.remember && RP.repl.trim()) rememberFixes([{ from: find, to: RP.repl }]);
  afterFix(ctx, touchedChapters);
  toast(`${undo.length} か所を置き換えました(「元に戻す」で戻せます)`, 'ok');
}

function undoReplace(ctx) {
  const u = RP.undo;
  if (!u) return;
  for (const [f, v] of u.items) f.set(v);
  RP.undo = null;
  afterFix(ctx, u.chapters);
  toast('置き換える前に戻しました');
}

function replaceBox(ctx) {
  const found = h('div', { class: 'rp-found' });
  const scopes = h('span', { class: 'row' });
  const refresh = () => {
    // 概要欄はあとから読み込まれるので、対象の選び方も毎回作り直す
    scopes.replaceChildren(...replaceTargets(ctx).map((t) => h('label', { class: 'check' },
      h('input', { type: 'checkbox', checked: RP.scope[t.key], onchange: (e) => { RP.scope[t.key] = e.target.checked; refresh(); } }), t.label)));
    if (!RP.find) {
      found.replaceChildren();
      return;
    }
    const parts = [];
    let samples = [];
    for (const t of replaceTargets(ctx)) {
      const hits = t.items.map((f) => f.get()).filter((v) => v.includes(RP.find));
      if (hits.length) parts.push(`${t.label} ${hits.length} ${t.unit}${RP.scope[t.key] ? '' : '(対象外)'}`);
      if (t.key === 'segs') samples = hits.slice(0, 4);
    }
    found.replaceChildren(
      parts.length ? h('b', { class: 'small' }, '見つかった数: ' + parts.join('・')) : h('span', { class: 'muted small' }, '見つかりません'),
      ...(samples.length ? [h('div', { class: 'rp-samples' }, samples.map((v) => h('div', {}, showFix(v, RP.find, RP.repl))))] : []));
  };
  const input = (k, placeholder) => h('input', {
    value: RP[k], placeholder, 'aria-label': placeholder,
    oninput: (e) => { RP[k] = e.target.value; refresh(); },
    onfocus: refresh,
    onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); doReplace(ctx); } },
  });
  refresh();
  RP.refresh = refresh;          // 概要欄を読み込んだ・直したときにも数え直す(renderChapters から呼ぶ)
  return h('div', { class: 'ft-block' },
    h('b', {}, '一括置換'),
    h('div', { class: 'rp-row' },
      input('find', '探す言葉(例: ヨシミ)'), h('span', { class: 'muted' }, '→'), input('repl', '置き換える言葉(例: 吉見)'),
      h('button', { class: 'primary', onclick: () => doReplace(ctx) }, 'すべて置き換える'),
      RP.undo && h('button', { class: 'ghost', title: RP.undo.note, onclick: () => undoReplace(ctx) }, '元に戻す')),
    h('div', { class: 'row rp-opts' },
      h('span', { class: 'muted small' }, '対象'), scopes,
      h('label', { class: 'check' },
        h('input', { type: 'checkbox', checked: RP.remember, onchange: (e) => { RP.remember = e.target.checked; } }),
        '辞書に覚える(次の文字起こしから自動で直す)')),
    found);
}

/* AI に誤字の候補を出してもらう。行番号は画面の行の順(1から) */

async function copyTypoPrompt(ctx) {
  const { text } = await api('/api/typos/prompt', { lines: ctx.segs().map((s) => s.text) });
  if (!(await copyText(text))) return showCopyBox('AI への依頼文(誤字の候補)', text);
  const long = text.length > LONG_PROMPT
    ? `\n\n※ 依頼文が長めです(約${Math.round(text.length / 1000)}千字)。無料版の ChatGPT などでは後半が読まれないことがあります。`
      + 'そのときは、有料版や Claude(claude.ai、または「AI で探す」)をお使いください。'
    : '';
  const next = await ask('依頼文をコピーしました',
    '1. ChatGPT や claude.ai を開いて、新しいチャットに貼り付けて送ります\n'
    + '2. 返事が来たら、その文をまるごとコピーします\n'
    + '3. ここに戻って「返事を貼り付け」を押すと、候補を1つずつ選べます' + long,
    [{ label: 'ChatGPT を開く', value: 'chatgpt', primary: true },
     { label: 'claude.ai を開く', value: 'claude' },
     { label: '閉じる', value: null }]);
  if (CHAT_SITES[next]) window.open(CHAT_SITES[next], '_blank', 'noopener');
}

function pasteTypoReply(ctx) {
  const modal = $('modal');
  const area = h('textarea', { rows: 12, placeholder: '12: 誤 → 正 ※理由\n48: 誤 → 正 ※理由', style: 'width:100%' });
  const message = h('p', { class: 'bad-text small' });
  const close = () => { modal.hidden = true; modal.replaceChildren(); };
  const load = guard(async () => {
    message.textContent = '';
    try {
      const r = await api('/api/typos/paste', { text: area.value, count: ctx.segs().length });
      close();
      showTypoCandidates(ctx, r.fixes);
    } catch (e) {
      message.textContent = e.message;
    }
  });
  modal.replaceChildren(h('div', { class: 'mbox wide', role: 'dialog', 'aria-modal': 'true' },
    h('h3', {}, 'AI の返事を貼り付け(誤字の候補)'),
    h('p', { class: 'muted small' }, 'ChatGPT や claude.ai から返ってきた文を、そのまま貼り付けてください。「12: 誤 → 正」の形の行だけを読み取ります。'),
    area, message,
    h('div', { class: 'mbtns' },
      h('button', { class: 'outline', onclick: close }, 'やめる'),
      h('button', { class: 'primary', onclick: load }, '読み込む'))));
  modal.hidden = false;
  area.focus();
  navigator.clipboard?.readText?.().then((clip) => {
    if (!area.value && /\d+\s*[:：]\s*.+(→|->)/.test(clip || '') && !clip.includes('--- テロップ ---')) {
      area.value = clip;
      area.after(h('p', { class: 'hint pasted' }, 'コピーしてあった返事を入れました。よければ「読み込む」を押してください。'));
    }
  }).catch(() => {});
}

async function findTyposWithAi(ctx, button) {
  button.disabled = true;
  const old = button.textContent;
  button.textContent = 'AI が探しています…(1分ほど)';
  try {
    const r = await api('/api/typos/ai', { lines: ctx.segs().map((s) => s.text) });
    showTypoCandidates(ctx, r.fixes);
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}

/** 候補の行を探す(依頼文を作ったあとに行を足したり消したりしても、近くの行なら見つける) */
function locateTypo(segs, f) {
  const i = f.line - 1;
  for (const d of [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5]) {
    if (segs[i + d]?.text.includes(f.wrong)) return i + d;
  }
  return -1;
}

function showTypoCandidates(ctx, found) {
  if (!found.length) {
    ask('誤字の候補はありませんでした', 'AI は直すところを見つけませんでした。', [{ label: '閉じる', value: null, primary: true }]);
    return;
  }
  const segs = ctx.segs();
  const rows = found.map((f) => {
    const at = locateTypo(segs, f);
    return { ...f, at, use: at >= 0 };
  });
  let remember = false;
  const modal = $('modal');
  const close = () => { modal.hidden = true; modal.replaceChildren(); };
  const applyBtn = h('button', { class: 'primary' });
  const count = () => { applyBtn.textContent = `チェックしたものを直す(${rows.filter((r) => r.use).length}個)`; };
  const boxes = [];
  const list = h('div', { class: 'typos' }, rows.map((r) => {
    const box = h('input', { type: 'checkbox', checked: r.use, disabled: r.at < 0, onchange: (e) => { r.use = e.target.checked; count(); } });
    boxes.push([box, r]);
    return h('label', { class: 'typo' + (r.at < 0 ? ' missing' : '') },
      box,
      h('span', { class: 'mono small muted' }, `${r.at >= 0 ? r.at + 1 : r.line}行`),
      h('span', { class: 'typo-text' },
        r.at >= 0 ? showFix(segs[r.at].text, r.wrong, r.right) : showFix('', r.wrong, r.right),
        r.at < 0 && h('span', { class: 'muted small' }, '(その行に見つかりません。もう直してあるかもしれません)'),
        r.reason && h('span', { class: 'hint typo-why' }, r.reason)));
  }));
  const setAll = (on) => { for (const [box, r] of boxes) if (r.at >= 0) { r.use = box.checked = on; } count(); };
  applyBtn.onclick = () => {
    let n = 0;
    const learned = [];
    for (const r of rows.filter((x) => x.use)) {
      const s = segs[r.at];
      if (!s || !s.text.includes(r.wrong)) continue;
      s.text = s.text.replace(r.wrong, r.right);
      n += 1;
      if (!learned.some((x) => x.from === r.wrong)) learned.push({ from: r.wrong, to: r.right });
    }
    close();
    if (!n) return toast('直したものはありません');
    if (remember && learned.length) rememberFixes(learned);
    ctx.onChange();
    toast(`${n} か所を直しました`, 'ok');
  };
  count();
  modal.replaceChildren(h('div', { class: 'mbox wide', role: 'dialog', 'aria-modal': 'true' },
    h('h3', {}, `誤字の候補(${rows.length}個)`),
    h('p', { class: 'muted small' }, 'AI が見つけた候補です。直すものにチェックを付けて、下のボタンを押してください。チェックを外したものは直しません。'
      + '赤い取り消し線が今の文字、緑が直したあとの文字です。'),
    h('div', { class: 'row' },
      h('button', { class: 'ghost small', onclick: () => setAll(true) }, 'すべて選ぶ'),
      h('button', { class: 'ghost small', onclick: () => setAll(false) }, 'すべて外す')),
    list,
    h('label', { class: 'check', style: 'margin-left:0' },
      h('input', { type: 'checkbox', onchange: (e) => { remember = e.target.checked; } }),
      '直したものを辞書にも覚える(次の文字起こしから自動で直す)'),
    h('div', { class: 'mbtns' }, h('button', { class: 'outline', onclick: close }, 'やめる'), applyBtn)));
  modal.hidden = false;
  applyBtn.focus();
}

function typoBox(ctx) {
  const aiBtn = S.hasKey && h('button', { onclick: guard((e) => findTyposWithAi(ctx, e.currentTarget)) }, 'AI で探す(API)');
  return h('div', { class: 'ft-block' },
    h('b', {}, 'AI に誤字を探してもらう'),
    h('div', { class: 'chapactions' },
      h('button', { onclick: guard(() => copyTypoPrompt(ctx)) }, '1. 依頼文をコピー'),
      h('button', { onclick: () => pasteTypoReply(ctx) }, '2. 返事を貼り付け'),
      aiBtn),
    h('p', { class: 'hint' }, '聞き間違いや変換の誤りを AI に挙げてもらい、1つずつ選んで直します。'));
}

/* 直し方の辞書。覚えた言い方は、次の文字起こしのあとで自動で直す */

function dictBox(ctx) {
  const body = h('div', { class: 'dict-body' });
  const from = h('input', { placeholder: '誤(例: ヨシミ)', 'aria-label': '直す前の言い方' });
  const to = h('input', { placeholder: '正(例: 吉見)', 'aria-label': '直したあとの言い方' });
  const draw = () => {
    const entries = FX.entries;
    body.replaceChildren(
      h('p', { class: 'hint' }, '覚えた言い方は、次の文字起こしから自動で直します。'),
      entries == null ? h('div', { class: 'muted small' }, '読み込んでいます…')
        : entries.length ? h('div', { class: 'dictlist' }, entries.map((e) => h('div', { class: 'dictrow' },
            h('span', {}, e.from), h('span', { class: 'muted' }, '→'), h('b', {}, e.to),
            h('button', {
              class: 'ghost del', title: 'この言い方を忘れる',
              onclick: guard(async () => { FX.entries = (await api('/api/fixes/remove', { from: e.from })).entries; draw(); }),
            }, '×'))))
          : h('div', { class: 'muted small' }, 'まだ何も覚えていません。'),
      h('div', { class: 'rp-row' }, from, h('span', { class: 'muted' }, '→'), to,
        h('button', {
          onclick: guard(async () => {
            FX.entries = (await api('/api/fixes/add', { entries: [{ from: from.value, to: to.value }] })).entries;
            from.value = to.value = '';
            draw();
          }),
        }, '覚える')),
      entries?.length > 0 && h('div', { class: 'row' },
        h('button', {
          class: 'ghost', onclick: () => {
            let n = 0;
            const sorted = [...entries].sort((a, b) => b.from.length - a.from.length);
            for (const s of ctx.segs()) {
              for (const e of sorted) {
                if (!s.text.includes(e.from)) continue;
                n += s.text.split(e.from).length - 1;
                s.text = s.text.split(e.from).join(e.to);
              }
            }
            if (!n) return toast('辞書の言い方は、いまのテロップにありませんでした');
            ctx.onChange();
            toast(`辞書のとおりに ${n} か所を直しました`, 'ok');
          },
        }, 'いまのテロップに辞書を使う')));
  };
  const box = h('details', { class: 'adv dict' }, h('summary', {}, FX.entries ? `直し方の辞書(${FX.entries.length}個)` : '直し方の辞書'), body);
  box.addEventListener('toggle', () => {
    if (!box.open) return;
    draw();
    if (FX.entries == null) {
      api('/api/fixes', {}).then((r) => { FX.entries = r.entries; draw(); }).catch((e) => toast(e.message, 'bad'));
    }
  });
  return box;
}

/** テロップをまとめて直す欄(開け閉めは次に開いたときも覚えている) */
function captionTools(ctx) {
  const el = h('details', { class: 'adv fixtools', open: sectionOpen('fixtools', false) },
    h('summary', {}, 'まとめて直す(一括置換・AI で誤字を探す・辞書)'),
    h('div', { class: 'ft-body' }, replaceBox(ctx), typoBox(ctx), dictBox(ctx)));
  el.addEventListener('toggle', () => rememberOpen('fixtools', el.open));
  return el;
}

/* ---------- 焼き込む前のテロップの確認(文字起こしのあと止まっているとき) ---------- */

const W = { project: '', segs: null, url: '', error: '', saveTimer: 0 };

async function loadWaitSegs(name) {
  W.project = name;
  W.segs = null;
  W.error = '';
  try {
    const p = await api('/api/project', { project: name });
    const saved = p.draft?.wait_segs;
    W.segs = (Array.isArray(saved) && saved.length ? saved : p.segments).map((s) => ({
      start: +s.start, end: +s.end, text: String(s.text ?? ''), orig: typeof s.orig === 'string' ? s.orig : String(s.text ?? ''),
    }));
    W.url = p.work_url || '';
  } catch (e) {
    W.error = e.message;
  }
  if (W.project === name) renderWaitCaptions();
}

function saveWaitSoon() {
  clearTimeout(W.saveTimer);
  const name = W.project, segs = W.segs;
  W.saveTimer = setTimeout(() => api('/api/draft', { project: name, draft: { wait_segs: segs } }).catch(() => {}), 1000);
}

/** 焼き込みに使うテロップ(直していなければ送らない) */
function waitSegments(job) {
  if (W.project !== job.project || !W.segs || !W.segs.some((s) => s.text.trim() !== s.orig)) return undefined;
  return W.segs.map(({ start, end, text }) => ({ start, end, text: text.trim() }));
}

function waitCaptionsCard(job) {
  const card = h('div', { id: 'waitCaps', style: 'display:grid;gap:12px' });
  if (W.project !== job.project) loadWaitSegs(job.project);
  setTimeout(renderWaitCaptions);
  return section('waitcaps', 'テロップの確認', {
    extra: h('span', { class: 'chip review', id: 'waitChanged', hidden: true }),
  }, card);
}

function refreshWaitChanged() {
  const chip = $('waitChanged');
  if (!chip || !W.segs) return;
  const n = W.segs.filter((s) => s.text.trim() !== s.orig).length;
  chip.hidden = !n;
  chip.textContent = `${n} 行を修正中`;
}

function renderWaitCaptions() {
  const card = $('waitCaps');
  if (!card) return;
  if (!W.segs) {
    card.replaceChildren(h('div', { class: 'empty' }, W.error || '読み込んでいます…'));
    return;
  }
  const video = W.url ? h('video', {
    id: 'waitVideo', controls: true, playsinline: true, disablePictureInPicture: true,
    controlsList: 'nofullscreen nodownload noremoteplayback', preload: 'metadata', src: W.url,
  }) : h('div', { class: 'empty' }, '動画を表示できません(文字は直せます)');
  if (W.url) {
    video.addEventListener('timeupdate', () => {
      if (stopAt !== null && video.currentTime >= stopAt) { video.pause(); stopAt = null; }
    });
  }
  const ctx = {
    project: W.project,
    segs: () => W.segs,
    titles: () => null,
    onChange: () => { saveWaitSoon(); renderWaitCaptions(); },
  };
  const rows = W.segs.map((s, i) => h('div', { class: 'line' },
    h('div', { class: 'linetop' },
      h('button', { class: 't', title: 'この箇所を再生', onclick: () => W.url && playIn(video, s.start, s.end) }, `▶ ${fmtTime(s.start)}〜${fmtTime(s.end)}`),
      h('input', {
        value: s.text, 'data-w': i, class: s.text.trim() !== s.orig ? 'changed' : '',
        placeholder: '(空のままだとこのテロップは出ません)',
        oninput: (e) => {
          s.text = e.target.value;
          e.target.classList.toggle('changed', s.text.trim() !== s.orig);
          refreshWaitChanged();
          saveWaitSoon();
        },
        onkeydown: (e) => {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          const next = card.querySelector(`input[data-w="${i + 1}"]`);
          if (next) { next.focus(); next.scrollIntoView({ block: 'nearest' }); }
        },
      }))));
  card.replaceChildren(
    h('p', { class: 'muted small' }, '時刻のボタンで再生します。空にした行は出ません。直した内容は自動で保存します。'),
    captionTools(ctx),
    h('div', { class: 'fix' }, video, h('div', { class: 'lines' }, rows)));
  refreshWaitChanged();
}

/* ---------- polling ---------- */

let pollTimer = 0;

async function poll() {
  clearTimeout(pollTimer);
  let st;
  try {
    st = await api('/api/state');
  } catch {
    pollTimer = setTimeout(poll, 3000);
    return;
  }
  const before = S.job?.state;
  const beforeWait = S.job?.wait || '';
  S.job = st.job;
  S.recent = st.recent;
  S.results = st.results;
  if (!S.fonts.length) S.fonts = st.fonts;
  if (!S.settings) S.settings = st.settings;
  S.hasKey = !!st.has_key;

  if (st.pending) {
    useVideoPath(st.pending, `「${basename(st.pending)}」を受け取りました。内容を確認して「次へ」に進んでください。`).catch((e) => toast(e.message, 'bad'));
  }

  const now = st.job.state;
  if (now === 'running') {
    const waitChanged = (st.job.wait || '') !== beforeWait;
    if (waitChanged && st.job.wait === 'titles' && S.step !== 'run') toast('文字起こしが終わりました。「3 処理」で見出しを決めてください。');
    if (waitChanged && st.job.wait === 'captions' && S.step !== 'run') toast('文字起こしが終わりました。「3 処理」でテロップを確かめてください。');
    if (S.step === 'run' && before === 'running' && !waitChanged) updateProgress();
    else if (S.step === 'run') renderRun();
    else { renderHeader(); renderSteps(); }
  } else if (before === 'running') {
    if (now === 'done') {
      toast(st.job.kind === 'reburn' ? '作り直しが終わりました' : '処理が終わりました');
      await openProject(st.job.project).catch((e) => toast(e.message, 'bad'));
    } else {
      if (now === 'cancelled') toast('中止しました');
      if (S.step === 'run') renderRun();
      else renderAll();
    }
  } else if (S.step === 'video') {
    const card = $('recentCard');
    if (card) card.replaceChildren(...recentList());
    renderSteps();
  }
  pollTimer = setTimeout(poll, now === 'running' ? 1000 : 3000);
}

/* ---------- boot ---------- */

/** 「場所を表示」「結果フォルダを開く」。エクスプローラーを最前面に出し、この画面に戻ってきたら閉じる */
let explorerOpen = false;

async function showPlace(path) {
  await api('/api/open', { path });
  explorerOpen = true;
}

window.addEventListener('focus', () => {
  if (!explorerOpen) return;
  explorerOpen = false;
  api('/api/explorer-done', {}).catch(() => {});
});

function wire() {
  for (const b of $('steps').querySelectorAll('button')) b.onclick = () => setStep(b.dataset.step);
  $('btnPhone').onclick = guard(openPhoneDoor);
  window.addEventListener('resize', () => {
    if (isNarrow() !== S.narrow) {
      S.narrow = isNarrow();
      renderAll();
    }
  });
  $('btnNew').onclick = () => {
    S.video = null;
    S.thumb = '';
    setStep('video');
  };
  $('btnResults').onclick = guard(() => showPlace(S.results));
  window.addEventListener('resize', () => { settingsEditor?.update(); reviewEditor?.update(); settingsLogo?.update(); });
  window.addEventListener('pagehide', () => navigator.sendBeacon(`/api/bye?t=${encodeURIComponent(TOKEN)}`));
}

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
    try { sessionStorage.setItem('clipper-token', TOKEN); } catch {}
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


/** スマホの中の動画を送って、そのまま処理に進む */
async function uploadVideo(file) {
  S.upload = { name: file.name, ratio: 0 };
  renderAll();
  try {
    const item = await sendOneFile(file, (r) => {
      S.upload.ratio = r;
      const bar = $('uploadBar');
      const label = $('uploadLabel');
      if (bar) bar.style.width = `${Math.round(r * 100)}%`;
      if (label) label.textContent = `${file.name} … ${Math.round(r * 100)}%`;
    });
    S.upload = null;
    await useVideoPath(item.path, `「${item.name}」を受け取りました。`);
  } catch (e) {
    S.upload = null;
    toast(e.message, 'bad');
    renderAll();
  }
}

async function boot() {
  if (!TOKEN && PIN_IN_URL) {           // QR から開いたときは、そのままつなぐ
    try {
      TOKEN = (await api('/api/pair', { pin: PIN_IN_URL })).token;
      try { sessionStorage.setItem('clipper-token', TOKEN); } catch { /* 使えなくても動く */ }
    } catch (e) {
      return askPin(e.message);
    }
  }
  if (!TOKEN) return askPin();          // スマホから開いたときは合言葉を聞く
  wire();
  try {
    const st = await api('/api/state');
    S.settings = st.settings;
    S.hasKey = !!st.has_key;
    S.fonts = st.fonts;
    S.recent = st.recent;
    S.results = st.results;
    S.job = st.job;
    if (st.pending) await useVideoPath(st.pending, `「${basename(st.pending)}」を受け取りました。内容を確認して「次へ」に進んでください。`);
    if (st.job.state === 'running') S.step = 'run';
  } catch (e) {
    showError(e.message);
  }
  renderAll();
  pollTimer = setTimeout(poll, 1000);
}

boot();
