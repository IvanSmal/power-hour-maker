'use strict';
/* Power Hour Maker UI - plain JS talking to the local API (app/server.py). */

// ============================================================================================
// helpers
// ============================================================================================
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith('--')) el.style.setProperty(sk, sv);
        else el.style[sk] = sv;
      }
    }
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function fmtTime(s, dec = false) {
  if (s == null || isNaN(s)) return '–';
  s = Math.max(0, +s);
  const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const ss = dec ? sec.toFixed(1).padStart(4, '0') : String(Math.floor(sec)).padStart(2, '0');
  return hh ? `${hh}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
function parseTime(str) {
  const parts = String(str).trim().split(':').map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}
function fmtBytes(b) {
  if (!b) return '0 MB';
  return b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
}
function fmtViews(n) {
  if (!n) return '';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B views';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M views';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K views';
  return n + ' views';
}
const getPath = (obj, path) => path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
function setPath(obj, path, val) {
  const ks = path.split('.');
  let o = obj;
  for (const k of ks.slice(0, -1)) o = o[k] ??= {};
  o[ks[ks.length - 1]] = val;
}
function deepMerge(a, b) {
  for (const [k, v] of Object.entries(b)) {
    if (v && typeof v === 'object' && !Array.isArray(v) && a[k] && typeof a[k] === 'object') deepMerge(a[k], v);
    else a[k] = v;
  }
  return a;
}

const api = {
  async req(method, url, data) {
    const opt = { method, headers: {} };
    if (data instanceof FormData) opt.body = data;
    else if (data !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(data); }
    const r = await fetch(url, opt);
    let j = {};
    try { j = await r.json(); } catch { /* not json */ }
    if (!r.ok || j.error) throw new Error(j.error || `${r.status} ${r.statusText}`);
    return j;
  },
  get: url => api.req('GET', url),
  post: (url, data = {}) => api.req('POST', url, data),
  patch: (url, data) => api.req('PATCH', url, data),
};

function toast(msg, kind = '') {
  const t = h('div', { class: 'toast ' + kind }, msg);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 8000 : 3500);
}
async function withBusy(btn, fn) {
  if (btn) { btn.classList.add('busy'); btn.disabled = true; }
  try { return await fn(); }
  catch (e) { toast(e.message, 'err'); }
  finally { if (btn) { btn.classList.remove('busy'); btn.disabled = false; } }
}
const desktop = () => !!(window.pywebview && window.pywebview.api);
const ACCEPT = {
  video: 'video/*,.mkv,.flv,.wmv,.ts',
  image: 'image/*',
  audio: 'audio/*,.mp3,.wav,.m4a,.ogg,.flac,.opus',
};
/** Ask for files: desktop mode gives paths, browser mode gives File objects. */
function pickFiles(kind, multiple = false) {
  if (desktop()) return window.pywebview.api.pick_files(kind, multiple).then(paths => ({ paths: paths || [] }));
  return new Promise(resolve => {
    const inp = h('input', { type: 'file', accept: ACCEPT[kind], multiple });
    inp.addEventListener('change', () => resolve({ files: [...inp.files] }));
    inp.click();
  });
}

// ============================================================================================
// state + polling
// ============================================================================================
const S = {
  rev: null, project: null, media: {}, summary: null, render: null, meta: null,
  analysis: {}, tab: localStorage.getItem('tab') || 'songs', search: { mode: 'add', clipId: null },
};
const pending = {};   // path -> value, local edits not yet saved
const inflight = {};  // path -> value, edits being saved right now

let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  try {
    const st = await api.get('/api/state' + (S.rev ? `?since=${encodeURIComponent(S.rev)}` : ''));
    $('#conn').classList.add('hidden');
    S.render = st.render;
    if (!st.unchanged) {
      const switched = S.project && S.project.id !== st.project.id;
      S.rev = st.rev; S.project = st.project; S.media = st.media; S.summary = st.summary;
      for (const [p, v] of Object.entries({ ...inflight, ...pending })) setPath(S.project, p, v);
      if (switched) onProjectSwitched();
      refresh();
    }
    renderRenderStatus();
  } catch {
    $('#conn').classList.remove('hidden');
  }
  const active = S.render?.status === 'running'
    || Object.values(S.media).some(m => !['ready', 'error'].includes(m.status))
    || (S.project?.clips || []).some(c => c.resolve === 'searching');
  pollTimer = setTimeout(poll, active ? 700 : 2000);
}

function refresh() {
  syncBindings();
  renderStats();
  renderClips();
  renderPickers();
  renderChecklist();
  previews.changed();
  trimmer.sync();
}

function onProjectSwitched() {
  rows.forEach(r => r.el.remove());
  rows.clear();
  trimmer.close();
  $('#results').classList.add('hidden');
  previews.reset();
}

// ============================================================================================
// two-way form binding: data-bind="intro.title" etc.
// ============================================================================================
let patchTimer = null;
function setValue(path, v, delay = 0) {
  setPath(S.project, path, v);
  pending[path] = v;
  syncBindings();
  clearTimeout(patchTimer);
  patchTimer = setTimeout(flushPatch, delay);
  previews.changed();
}
async function flushPatch() {
  const paths = Object.keys(pending);
  if (!paths.length) return;
  const changes = {};
  for (const p of paths) {
    const part = {};
    setPath(part, p, pending[p]);
    deepMerge(changes, part);
    inflight[p] = pending[p];
    delete pending[p];
  }
  try { await api.patch('/api/project', changes); }
  catch (e) { toast(e.message, 'err'); }
  finally { for (const p of paths) if (inflight[p] === getPath(changes, p)) delete inflight[p]; }
  poll();
}

function convert(el, v) {
  if (el.dataset.type === 'number' || el.type === 'number' || el.type === 'range') {
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  return v;
}

function bindAll(root = document) {
  for (const el of $$('[data-bind]', root)) {
    if (el.dataset.bound) continue;
    el.dataset.bound = '1';
    const path = el.dataset.bind;
    if (el.matches('.seg, .effect-grid')) {
      el.addEventListener('click', e => {
        const b = e.target.closest('[data-value]');
        if (b) setValue(path, convert(el, b.dataset.value));
      });
      continue;
    }
    const live = el.type === 'range' || el.type === 'color' || el.tagName === 'TEXTAREA'
      || (el.tagName === 'INPUT' && !['checkbox'].includes(el.type));
    el.addEventListener(live ? 'input' : 'change', () => {
      if (el.tagName === 'SELECT' && el.value === '__upload__') return uploadSound(el, path);
      const v = el.type === 'checkbox' ? el.checked : convert(el, el.value);
      if (v === null) return;
      setValue(path, v, live ? 400 : 0);
    });
  }
  for (const b of $$('[data-play-sound]', root)) {
    if (b.dataset.bound) continue;
    b.dataset.bound = '1';
    b.addEventListener('click', () => playSound(getPath(S.project, b.dataset.playSound)));
  }
}

function evalShow(expr) {
  const m = expr.match(/^([\w.]+)\s*(==|!=)\s*(.+)$/);
  if (!m) return !!getPath(S.project, expr.trim());
  const v = String(getPath(S.project, m[1]) ?? '');
  const opts = m[3].split('|');
  return m[2] === '==' ? opts.includes(v) : !opts.includes(v);
}

function syncBindings() {
  if (!S.project) return;
  for (const el of $$('[data-bind]')) {
    const v = getPath(S.project, el.dataset.bind);
    if (el.matches('.seg, .effect-grid')) {
      $$('[data-value]', el).forEach(b => b.classList.toggle('active', String(v) === b.dataset.value));
      continue;
    }
    if (document.activeElement === el && el.type !== 'checkbox' && el.type !== 'range') continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.tagName === 'SELECT') {
      if (el.dataset.options === 'sounds' && v && ![...el.options].some(o => o.value === v)) fillSoundOptions(el, v);
      el.value = v ?? '';
    } else if (el.value !== String(v ?? '')) el.value = v ?? '';
  }
  for (const o of $$('output[data-for]')) {
    const v = getPath(S.project, o.dataset.for);
    let text = v;
    if (o.dataset.percent !== undefined) text = Math.round((v || 0) * 100) + '%';
    else if (typeof v === 'number') text = (Number.isInteger(v) ? v : v.toFixed(1)) + (o.dataset.suffix || '');
    if (o.dataset.zero && !v) text = o.dataset.zero;
    o.textContent = text;
  }
  for (const el of $$('[data-show]')) el.classList.toggle('hidden', !evalShow(el.dataset.show));
  $('#effect-grid')?.classList.toggle('random', !!S.project.transition.random);
  $$('#effect-grid .effect').forEach(t => t.classList.toggle('dim', !!S.project.transition.random));
  const res = S.project.settings.resolution || '1920x1080';
  const [w, hgt] = res.split('x').map(Number);
  $$('.preview-box').forEach(b => {
    b.classList.toggle('vertical', hgt > w);
    b.classList.toggle('square', hgt === w);
  });
  renderTriviaBar();
}

// Same maths as PlanBuilder.trivia_phases: [end of sound-only part, answer reveal] for a clip.
function triviaPhases(dur) {
  const tv = S.project.overlays.trivia || {};
  if (!tv.enabled) return null;
  let a = Math.max(0, +tv.audio_seconds || 0), v = Math.max(0, +tv.video_seconds || 0);
  const room = Math.max(0, dur - 3);
  if (a + v > room && a + v > 0) [a, v] = [a * room / (a + v), v * room / (a + v)];
  if (a < 1) a = 0;
  return [a, a + v];
}

function renderTriviaBar() {
  const bar = $('#trivia-bar');
  const hint = $('#look-hint');
  const dur = +S.project.settings.clip_seconds || 60;
  const ph = triviaPhases(dur);
  if (hint) hint.textContent = ph
    ? `Trivia: sound only until ${fmtTime(ph[0])}, video until ${fmtTime(ph[1])}, then the answer — scrub to check each part.`
    : 'Title shows at the start, the countdown at the end — scrub to check both.';
  if (!bar || !ph) return;
  const parts = [['sound', '🎧 Sound', 0, ph[0]], ['video', '🎬 Video', ph[0], ph[1]], ['answer', '✅ Answer', ph[1], dur]];
  const sig = JSON.stringify([ph, dur]);
  if (bar.dataset.sig === sig) return;
  bar.dataset.sig = sig;
  bar.replaceChildren(...parts.filter(([, , a, b]) => b - a > 0.01).map(([cls, label, a, b]) =>
    h('div', { class: 'tb-' + cls, style: `flex:${b - a}`, title: `${label.slice(3)}: ${fmtTime(a)} – ${fmtTime(b)}` },
      h('b', {}, label), h('span', {}, `${fmtTime(a)}–${fmtTime(b)}`))));
}

function fillOptions() {
  for (const sel of $$('select[data-options]')) {
    const kind = sel.dataset.options;
    if (kind === 'sounds') { fillSoundOptions(sel, getPath(S.project || {}, sel.dataset.bind || '')); continue; }
    sel.innerHTML = '';
    const list = kind === 'fonts' ? S.meta.fonts.map(f => ({ id: f, label: f }))
      : kind === 'effects' ? S.meta.effects : [];
    for (const it of list) sel.append(new Option(it.label, it.id));
  }
}

function fillSoundOptions(sel, current) {
  sel.innerHTML = '';
  sel.append(new Option('— None —', 'none'));
  for (const s of S.meta.sounds) sel.append(new Option(s.label, s.id));
  if (current && current.startsWith('file:')) {
    sel.append(new Option('Custom: ' + current.slice(5).replace(/-[0-9a-f]{6}(\.\w+)$/, '$1'), current));
  }
  sel.append(new Option('Upload your own sound…', '__upload__'));
  if (current) sel.value = current;
}

function playSound(v) {
  if (!v || v === 'none') return;
  new Audio('/api/sound/' + encodeURIComponent(v)).play().catch(() => toast('Could not play that sound', 'err'));
}

async function importAsset(kind) {
  const pick = await pickFiles(kind);
  if (pick.paths?.length) return (await api.post('/api/assets/import', { path: pick.paths[0] })).name;
  if (pick.files?.length) {
    const fd = new FormData();
    fd.append('file', pick.files[0]);
    return (await api.req('POST', '/api/assets/upload', fd)).name;
  }
  return null;
}

async function uploadSound(sel, path) {
  const prev = getPath(S.project, path);
  sel.value = prev;
  try {
    const name = await importAsset('audio');
    if (name) { setValue(path, 'file:' + name); fillSoundOptions(sel, 'file:' + name); }
  } catch (e) { toast(e.message, 'err'); }
}

// --- asset + media pickers ----------------------------------------------------------------
function renderPickers() {
  for (const el of $$('[data-asset]')) {
    const path = el.dataset.asset;
    const v = getPath(S.project, path) || '';
    if (el.dataset.cur === v && el.childElementCount) continue;
    el.dataset.cur = v;
    el.innerHTML = '';
    const kind = el.dataset.kind;
    el.append(
      h('button', { class: 'btn small', onclick: () => withBusy(null, async () => {
        const name = await importAsset(kind);
        if (name) setValue(path, name);
      }) }, kind === 'image' ? '🖼 Choose image…' : '🎵 Choose music…'),
      h('span', { class: 'name', title: v }, v ? v.replace(/-[0-9a-f]{6}(\.\w+)$/, '$1') : 'nothing chosen'),
      v && kind === 'audio' ? h('button', { class: 'btn small', onclick: () => new Audio('/assets/' + encodeURIComponent(v)).play() }, '▶') : null,
      v ? h('button', { class: 'btn small ghost', title: 'Clear', onclick: () => setValue(path, '') }, '✕') : null,
    );
  }
  for (const el of $$('[data-mediapick]')) {
    const path = el.dataset.mediapick;
    const key = getPath(S.project, path);
    if (!el.childElementCount) {
      const q = h('input', { class: 'q', placeholder: 'Paste a link or type a search…' });
      const fetchBtn = h('button', { class: 'btn small' }, 'Fetch');
      fetchBtn.addEventListener('click', () => withBusy(fetchBtn, async () => {
        if (!q.value.trim()) return;
        const r = await api.post('/api/media/add', { text: q.value.trim(), source: $('#search-source').value });
        q.value = '';
        setValue(path, r.key);
        toast(`Downloading “${r.title}”`);
      }));
      q.addEventListener('keydown', e => { if (e.key === 'Enter') fetchBtn.click(); });
      const fileBtn = h('button', { class: 'btn small', onclick: () => withBusy(null, async () => {
        const pick = await pickFiles('video');
        let r = null;
        if (pick.paths?.length) r = await api.post('/api/media/add', { path: pick.paths[0] });
        else if (pick.files?.length) {
          const fd = new FormData();
          fd.append('file', pick.files[0]);
          r = await api.req('POST', '/api/media/upload', fd);
        }
        if (r) setValue(path, r.key);
      }) }, '📁 File');
      const startPath = el.dataset.start;
      el.append(
        h('div', { class: 'row' }, q, fetchBtn, fileBtn),
        h('div', { class: 'row' }, h('span', { class: 'cur' }),
          startPath ? h('span', { class: 'muted small' }, 'start at') : null,
          startPath ? h('input', { type: 'number', class: 'num', min: 0, step: 1, 'data-bind': startPath, 'data-type': 'number' }) : null,
          startPath ? h('span', { class: 'unit' }, 's') : null,
          h('button', { class: 'btn small ghost clear', title: 'Remove video', onclick: () => setValue(path, null) }, '✕')),
      );
      bindAll(el);
      syncBindings();
    }
    const cur = $('.cur', el);
    const m = key && S.media[key];
    cur.textContent = !key ? 'No video chosen' : !m ? 'Queued…'
      : m.status === 'ready' ? `✓ ${m.title} (${fmtTime(m.duration)})`
      : m.status === 'error' ? `✗ ${m.error}` : `${m.title || ''} — ${m.stage || m.status}`;
    $('.clear', el).classList.toggle('hidden', !key);
  }
}

// ============================================================================================
// header + stats
// ============================================================================================
function renderStats() {
  const sm = S.summary;
  if (!sm) return;
  const pills = [
    [`${sm.ready}/${sm.target}`, 'songs', sm.ready >= sm.target ? 'good' : ''],
    [fmtTime(sm.duration), 'runtime', ''],
  ];
  if (sm.bonus) pills.push([`+${sm.bonus}`, 'bonus', '']);
  if (sm.shots) pills.push([sm.shots, sm.shots > 1 ? 'shots' : 'shot', '']);
  if (sm.pending) pills.push([sm.pending, 'downloading', 'busy']);
  if (sm.error) pills.push([sm.error, 'failed', 'bad']);
  $('#stats').replaceChildren(...pills.map(([b, l, c]) => h('div', { class: 'stat-pill ' + c }, h('b', {}, b), h('span', { class: 'muted small' }, l))));
  $('#fill-bar').style.width = Math.min(100, (sm.ready / sm.target) * 100) + '%';
  const others = sm.total - sm.ready - (sm.bonus_ready || 0);
  const extra = (others > 0 ? ` · ${others} more in the list` : '') + (sm.bonus ? ` · ${sm.bonus} bonus` : '')
    + (sm.shots ? ` · ${sm.shots} shot song${sm.shots > 1 ? 's' : ''}` : '');
  $('#fill-text').textContent = `${sm.ready} of ${sm.target} songs ready${extra} · video runtime ${fmtTime(sm.duration)}`;
  $('#shot-count').textContent = sm.shots ? `${sm.shots} shot song${sm.shots > 1 ? 's' : ''} in the playlist`
    : 'no shot songs yet - mark them with 🥃';
}

// ============================================================================================
// playlist
// ============================================================================================
const rows = new Map();
let dragId = null;

function clipById(id) { return S.project?.clips.find(c => c.id === id); }

// "12" for main songs, "B1" for bonus songs (they're numbered separately, after the outro)
function clipNumber(c) {
  const group = S.project.clips.filter(x => !!x.bonus === !!c.bonus);
  return (c.bonus ? 'B' : '') + (group.indexOf(c) + 1);
}

// Mark songs as shot / bonus songs. Songs that change group go to the end of their new group,
// main songs always come first - the same rules the backend applies.
function setFlags(ids, fields) {
  const moved = [];
  for (const c of S.project.clips) {
    if (!ids.includes(c.id)) continue;
    if ('bonus' in fields && !!c.bonus !== fields.bonus) moved.push(c);
    Object.assign(c, fields);
  }
  const all = [...S.project.clips.filter(c => !moved.includes(c)), ...moved];
  S.project.clips = [...all.filter(c => !c.bonus), ...all.filter(c => c.bonus)];
  renderClips();
  return api.post('/api/clips/flags', { ids, ...fields }).catch(e => toast(e.message, 'err')).then(poll);
}

const bonusDivider = h('div', { class: 'bonus-divider' },
  h('b', {}, '🎁 Bonus songs'),
  h('span', { class: 'muted small' }, 'secret extras after the end card, after'),
  h('input', { type: 'number', class: 'num', min: 0, max: 30, step: 1, 'data-bind': 'bonus.gap', 'data-type': 'number', title: 'Seconds of black between the end card and the first bonus song' }),
  h('span', { class: 'muted small' }, 's of black · counter says'),
  h('input', { class: 'bonus-label', 'data-bind': 'bonus.label', placeholder: 'BONUS', maxlength: 16, title: 'Shown on the drink counter: BONUS 1, BONUS 2, ...' }),
);
bonusDivider.addEventListener('dragover', e => { if (dragId) e.preventDefault(); });
bonusDivider.addEventListener('drop', e => {          // dropped on the header: becomes the first bonus song
  e.preventDefault();
  const first = S.project.clips.find(c => c.bonus);
  if (dragId && first && first.id !== dragId) moveClip(dragId, first.id, false);
});
function clipStatus(c) {
  if (c.resolve === 'searching') return { kind: 'busy', text: 'Searching…' };
  if (c.resolve === 'error') return { kind: 'err', text: 'Not found', err: c.error };
  const m = c.media_key && S.media[c.media_key];
  if (!m) return { kind: 'busy', text: 'Queued' };
  if (m.status === 'ready') {
    const short = (m.duration || 0) < S.project.settings.clip_seconds;
    return { kind: short ? 'short' : 'ready', text: short ? `Short (${Math.round(m.duration)}s)` : 'Ready' };
  }
  if (m.status === 'long') return { kind: 'long', text: 'Pick a section', err: `${fmtTime(m.duration)} long - choose which part to use` };
  if (m.status === 'error') return { kind: 'err', text: 'Failed', err: m.error };
  return { kind: 'busy', text: m.stage || m.status, progress: m.progress };
}
const SRC_LABEL = {
  heatmap: '🔥 most replayed', loudness: '🔊 loudest part', default: '⏱ default', manual: '✋ picked by you',
  full: 'whole video', loop: '⟳ looped',
};

function renderClips() {
  const list = $('#clip-list');
  const clips = S.project.clips;
  $('#empty-list').classList.toggle('hidden', clips.length > 0);
  const target = S.summary?.target || 60;
  const seen = new Set();
  const els = [];
  const place = (c, i, bonus) => {
    seen.add(c.id);
    let row = rows.get(c.id);
    if (!row) { row = makeRow(c.id); rows.set(c.id, row); }
    updateRow(row, c, i, target, bonus);
    els.push(row.el);
  };
  const main = clips.filter(c => !c.bonus), extra = clips.filter(c => c.bonus);
  main.forEach((c, i) => place(c, i, false));
  if (extra.length) {
    if (!bonusDivider.dataset.ready) { bonusDivider.dataset.ready = '1'; bindAll(bonusDivider); }
    els.push(bonusDivider);
  }
  extra.forEach((c, i) => place(c, i, true));
  els.forEach((el, k) => { if (list.children[k] !== el) list.insertBefore(el, list.children[k] || null); });
  if (!extra.length) bonusDivider.remove();
  for (const [id, row] of rows) if (!seen.has(id)) { row.el.remove(); rows.delete(id); }
  $('#btn-auto-sections').classList.toggle('hidden', !clips.some(c => clipStatus(c).kind === 'long'));
}

function makeRow(id) {
  const el = h('div', { class: 'clip' });
  const r = {
    el, id,
    handle: h('div', { class: 'handle', title: 'Drag to reorder' }, '⋮⋮'),
    num: h('div', { class: 'num' }),
    thumb: h('div', { class: 'thumb' }, h('span', { class: 'dur' })),
    info: h('div', { class: 'info' }, h('div', { class: 't' }), h('div', { class: 'c' })),
    mini: h('canvas', { class: 'mini', width: 500, height: 68 }),
    win: h('div', { class: 'window' }),
    status: h('div', { class: 'status' }),
    acts: h('div', { class: 'acts' }),
    sig: '',
  };
  const retry = h('button', { class: 'btn small ghost retry', title: 'Retry', onclick: e => { e.stopPropagation(); api.post('/api/clips/retry', { ids: [id] }).then(poll); } }, '⟳');
  const del = h('button', { class: 'btn small ghost', title: 'Remove', onclick: e => { e.stopPropagation(); removeClips([id]); } }, '✕');
  const flag = (cls, key, icon, tip) => h('button', {
    class: 'btn small ghost flag ' + cls, title: tip,
    onclick: e => { e.stopPropagation(); const c = clipById(id); if (c) setFlags([id], { [key]: !c[key] }); },
  }, icon);
  r.acts.append(
    flag('t-shot', 'shot', '🥃', 'Shot song: a "take a shot" card plays before it'),
    flag('t-bonus', 'bonus', '🎁', 'Bonus song: plays after the end card'),
    retry, del);
  el.append(r.handle, r.num, r.thumb, r.info, r.mini, r.win, r.status, r.acts);
  el.addEventListener('click', () => {
    const c = clipById(id);
    if (c && clipStatus(c).kind === 'long') sectioner.open(id); else trimmer.open(id);
  });
  r.handle.addEventListener('mousedown', () => { el.draggable = true; });
  r.handle.addEventListener('click', e => e.stopPropagation());
  el.addEventListener('dragstart', e => { dragId = id; el.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', id); });
  el.addEventListener('dragend', () => { el.draggable = false; el.classList.remove('dragging'); clearDropMarks(); dragId = null; });
  el.addEventListener('dragover', e => {
    if (!dragId || dragId === id) return;
    e.preventDefault();
    const b = el.getBoundingClientRect();
    clearDropMarks();
    el.classList.add(e.clientY > b.top + b.height / 2 ? 'drop-after' : 'drop-before');
  });
  el.addEventListener('drop', e => {
    e.preventDefault();
    const after = el.classList.contains('drop-after');
    clearDropMarks();
    if (dragId && dragId !== id) moveClip(dragId, id, after);
  });
  return r;
}
function clearDropMarks() { $$('.drop-before, .drop-after').forEach(x => x.classList.remove('drop-before', 'drop-after')); }

async function moveClip(srcId, targetId, after) {
  const src = clipById(srcId), tgt = clipById(targetId);
  if (src && tgt && !!src.bonus !== !!tgt.bonus) await setFlags([srcId], { bonus: !!tgt.bonus });   // dragged across the bonus header
  const ids = S.project.clips.map(c => c.id).filter(x => x !== srcId);
  const idx = ids.indexOf(targetId) + (after ? 1 : 0);
  ids.splice(idx, 0, srcId);
  const byId = Object.fromEntries(S.project.clips.map(c => [c.id, c]));
  S.project.clips = ids.map(x => byId[x]);
  renderClips();
  api.post('/api/clips/reorder', { ids }).then(poll).catch(e => toast(e.message, 'err'));
}

async function removeClips(ids) {
  S.project.clips = S.project.clips.filter(c => !ids.includes(c.id));
  renderClips();
  await api.post('/api/clips/remove', { ids }).catch(e => toast(e.message, 'err'));
  poll();
}

function updateRow(r, c, i, target, bonus = false) {
  const m = (c.media_key && S.media[c.media_key]) || {};
  const st = clipStatus(c);
  const w = S.summary?.windows?.[c.id];
  r.num.textContent = bonus ? `B${i + 1}` : i + 1;
  r.num.classList.toggle('over', !bonus && i >= target);
  r.num.classList.toggle('bonus', bonus);
  r.num.title = bonus ? 'Bonus song - plays after the end card'
    : i >= target ? 'Past your target length - still included in the video' : '';
  r.el.classList.toggle('is-shot', !!c.shot);
  r.el.classList.toggle('is-bonus', bonus);
  $('.t-shot', r.acts).classList.toggle('on', !!c.shot);
  $('.t-bonus', r.acts).classList.toggle('on', bonus);
  const thumb = c.thumbnail || m.thumbnail || (['ready', 'long'].includes(m.status)
    ? `/api/thumb/${encodeURIComponent(c.media_key)}?t=${Math.round((m.duration || 60) * 0.3)}` : null);
  const bg = thumb ? `url("${thumb}")` : '';
  if (r.thumb.style.backgroundImage !== bg) r.thumb.style.backgroundImage = bg;
  r.thumb.firstChild.textContent = fmtTime(m.duration || c.duration);
  const title = w?.title ? (w.artist ? `${w.title} — ${w.artist}` : w.title) : (m.title || c.title || c.query || c.url);
  r.info.firstChild.textContent = title;
  r.info.firstChild.title = m.title || c.title || '';
  const at = S.summary?.times?.[c.id];
  r.info.lastChild.textContent = [m.channel || c.channel, at != null ? `starts at ${fmtTime(at)} in the video` : ''].filter(Boolean).join(' · ');

  r.win.replaceChildren(...(w ? [`${fmtTime(w.ss)} → ${fmtTime(w.ss + w.dur)}`, h('span', { class: 'src' }, SRC_LABEL[w.src] || w.src)] : []));
  const pill = h('span', { class: 'pill ' + ({ err: 'err', busy: 'busy', short: 'short', long: 'long' }[st.kind] || 'ready') }, st.text);
  const kids = [pill];
  if (st.kind === 'busy' && st.progress != null) {
    const bar = h('div', { class: 'mini-progress' }, h('div', { style: { width: (st.progress * 100).toFixed(0) + '%' } }));
    kids.push(bar);
  }
  if (st.err) kids.push(h('div', { class: 'err-text', title: st.err }, st.err));
  r.status.replaceChildren(...kids);
  $('.retry', r.acts).classList.toggle('hidden', st.kind !== 'err');

  if (m.status === 'ready') needAnalysis(c.media_key);
  const a = S.analysis[c.media_key];
  const sig = [c.media_key, w?.ss, w?.dur, !!a, m.duration].join('|');
  if (sig !== r.sig) { r.sig = sig; drawMini(r.mini, a, w, m.duration); }
}

function drawMini(cv, a, w, total) {
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  if (!total) return;
  const X = t => (t / total) * W;
  const loud = a?.loudness || [];
  if (loud.length) {
    ctx.beginPath();
    ctx.moveTo(0, H);
    loud.forEach((db, i) => ctx.lineTo(X(i + 0.5), H - clamp((db + 50) / 50, 0, 1) * (H - 6)));
    ctx.lineTo(X(loud.length), H);
    ctx.fillStyle = 'rgba(56,214,255,.18)';
    ctx.fill();
  }
  const hm = a?.heatmap || [];
  if (hm.length) {
    ctx.beginPath();
    ctx.moveTo(0, H);
    hm.forEach(([s, e, v]) => ctx.lineTo(X((s + e) / 2), H - v * (H - 6)));
    ctx.lineTo(W, H);
    ctx.fillStyle = 'rgba(255,138,61,.55)';
    ctx.fill();
  }
  if (w) {
    const x0 = X(w.ss), x1 = Math.max(X(w.ss + w.dur), x0 + 3);
    ctx.fillStyle = 'rgba(255,196,0,.25)';
    ctx.fillRect(x0, 0, x1 - x0, H);
    ctx.strokeStyle = '#ffc400';
    ctx.lineWidth = 3;
    ctx.strokeRect(x0 + 1.5, 1.5, x1 - x0 - 3, H - 3);
  }
}

// lazily fetch heatmap/loudness for each ready video (a few at a time)
const analysisWanted = new Set();
const analysisLoading = new Set();
function needAnalysis(key) {
  if (!key || S.analysis[key] || analysisLoading.has(key)) return;
  analysisWanted.add(key);
  pumpAnalysis();
}
function pumpAnalysis() {
  while (analysisLoading.size < 4 && analysisWanted.size) {
    const key = analysisWanted.values().next().value;
    analysisWanted.delete(key);
    analysisLoading.add(key);
    api.get('/api/media/' + encodeURIComponent(key))
      .then(m => { S.analysis[key] = { heatmap: m.heatmap, loudness: m.loudness }; renderClips(); })
      .catch(() => {})
      .finally(() => { analysisLoading.delete(key); pumpAnalysis(); });
  }
}

// ============================================================================================
// search / adding songs
// ============================================================================================
async function doSearch(q, mode = 'add', clipId = null) {
  S.search = { mode, clipId };
  showTab('songs');
  const box = $('#results');
  box.classList.remove('hidden');
  $('#results-title').textContent = `Searching for “${q}”…`;
  $('#results-grid').replaceChildren(...Array.from({ length: 6 }, () => h('div', { class: 'result' }, h('div', { class: 'thumb' }), h('div', { class: 'meta' }, h('div', { class: 'c' }, '…')))));
  try {
    const r = await api.get(`/api/search?q=${encodeURIComponent(q)}&source=${encodeURIComponent($('#search-source').value)}`);
    renderResults(r.results, q);
  } catch (e) {
    $('#results-title').textContent = 'Search failed: ' + e.message;
    $('#results-grid').replaceChildren();
  }
}

function renderResults(results, q) {
  const { mode, clipId } = S.search;
  const head = $('#results-title');
  head.replaceChildren();
  if (mode === 'replace') {
    const idx = S.project.clips.findIndex(c => c.id === clipId);
    head.append(`Pick a replacement for song #${idx + 1} `, h('button', { class: 'btn ghost small', onclick: () => { S.search = { mode: 'add' }; renderResults(results, q); } }, 'cancel replace'));
  } else {
    head.append(`${results.length} result${results.length === 1 ? '' : 's'} for “${q}” `);
    const addable = results.filter(r => !r.live);
    if (addable.length > 1 && /^(https?:|www\.)/i.test(q)) {
      head.append(h('button', { class: 'btn gold small', onclick: e => addEntries(addable, e.target) }, `+ Add all ${addable.length}`));
    }
  }
  if (!results.length) { $('#results-grid').replaceChildren(h('div', { class: 'muted' }, 'Nothing found.')); return; }
  $('#results-grid').replaceChildren(...results.map(r => {
    const btn = mode === 'replace'
      ? h('button', { class: 'btn gold small', onclick: async () => {
          await api.post('/api/clips/replace', { id: clipId, entry: r });
          toast('Replaced');
          S.search = { mode: 'add' };
          $('#results').classList.add('hidden');
          poll();
        } }, 'Use this one')
      : h('button', { class: 'btn small' + (r.in_project ? '' : ' gold'), onclick: e => addEntries([r], e.currentTarget) }, r.in_project ? '+ Add again' : '+ Add');
    if (r.live) btn.disabled = true;
    return h('div', { class: 'result' },
      h('div', { class: 'thumb', style: { backgroundImage: r.thumbnail ? `url("${r.thumbnail}")` : '' } },
        r.duration ? h('span', { class: 'dur' }, fmtTime(r.duration)) : r.live ? h('span', { class: 'dur' }, 'LIVE') : null,
        r.in_project ? h('span', { class: 'badge-in' }, 'IN PLAYLIST') : null),
      h('div', { class: 'meta' }, h('div', { class: 't', title: r.title }, r.title), h('div', { class: 'c' }, [r.channel, fmtViews(r.views)].filter(Boolean).join(' · '))),
      h('div', { class: 'acts' }, btn,
        h('a', { class: 'btn small ghost', href: r.url, target: '_blank', title: 'Open the original page' }, '↗')));
  }));
}

async function addEntries(entries, btn) {
  await withBusy(btn, async () => {
    await api.post('/api/clips/add', { entries });
    if (btn) { btn.textContent = '✓ Added'; btn.classList.remove('gold'); }
    toast(entries.length === 1 ? `Added “${entries[0].title}”` : `Added ${entries.length} songs`, 'ok');
    poll();
  });
  if (btn) { btn.textContent = '✓ Added'; btn.classList.remove('gold'); }
}

function openBulk() {
  const ta = h('textarea', { placeholder: 'One per line, e.g.\n\nThe Killers - Mr. Brightside\nToto - Africa\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ\nhttps://www.youtube.com/playlist?list=...   (adds every video)\nhttps://vimeo.com/...' });
  const suffix = h('input', { placeholder: 'e.g. official video', style: { width: '220px' } });
  const count = h('span', { class: 'muted small' });
  const upd = () => { const n = ta.value.split('\n').filter(x => x.trim() && !x.trim().startsWith('#')).length; count.textContent = n ? `${n} line${n === 1 ? '' : 's'}` : ''; };
  ta.addEventListener('input', upd);
  const go = h('button', { class: 'btn gold' }, 'Add songs');
  go.addEventListener('click', () => withBusy(go, async () => {
    if (!ta.value.trim()) return;
    const r = await api.post('/api/clips/bulk', { text: ta.value, suffix: suffix.value, source: $('#search-source').value });
    closeModal();
    toast(`Looking up ${r.added} songs - they download in the background`, 'ok');
    poll();
  }));
  openModal(
    h('div', { class: 'modal-head' }, h('h3', {}, '📋 Bulk add songs'), h('button', { class: 'btn ghost icon', onclick: closeModal }, '✕')),
    h('div', { class: 'modal-body', style: { width: '720px' } },
      h('p', { class: 'muted small' }, 'Paste song names (the top search result is used - you can swap any of them later) or links from YouTube, Vimeo, Dailymotion, TikTok, and ~1800 other sites. Playlist links add every video in them.'),
      ta,
      h('div', { class: 'field-row' }, h('span', { class: 'lbl' }, 'Add to every search:'), suffix, h('span', { class: 'spacer' }), count)),
    h('div', { class: 'modal-foot' }, h('span', { class: 'spacer' }), h('button', { class: 'btn', onclick: closeModal }, 'Cancel'), go),
  );
  ta.focus();
}

async function addLocalFiles() {
  const pick = await pickFiles('video', true);
  if (pick.paths?.length) {
    const r = await api.post('/api/clips/local', { paths: pick.paths });
    toast(`Added ${r.added} file${r.added === 1 ? '' : 's'}`, 'ok');
  } else if (pick.files?.length) {
    const fd = new FormData();
    pick.files.forEach(f => fd.append('files', f));
    toast(`Uploading ${pick.files.length} file(s)…`);
    const r = await api.req('POST', '/api/clips/upload', fd);
    toast(`Added ${r.added} file${r.added === 1 ? '' : 's'}`, 'ok');
  }
  poll();
}

// ============================================================================================
// trimmer: pick the minute for one song
// ============================================================================================
const trimmer = {
  id: null, key: null, a: null, vdur: 0, L: 60, start: 0, dur: 60, short: false,
  drag: null, hoverT: null, playingClip: false, raf: 0, saveTimer: null, unsaved: undefined,

  el: () => $('#trimmer'),
  video: () => $('#trim-video'),

  async open(id) {
    const c = clipById(id);
    if (!c) return;
    this.id = id;
    this.el().classList.remove('hidden');
    this.stopClip();
    await this.load();
  },

  async load() {
    const c = clipById(this.id);
    if (!c) return this.close();
    const idx = S.project.clips.indexOf(c);
    const st = clipStatus(c);
    const m = (c.media_key && S.media[c.media_key]) || {};
    const w = S.summary?.windows?.[c.id];
    $('#trim-num').textContent = clipNumber(c);
    $('#trim-title').textContent = m.title || c.title || c.query || '';
    $('#trim-sub').textContent = [m.channel || c.channel, m.duration ? fmtTime(m.duration) : '', m.views ? fmtViews(m.views) : '', st.text].filter(Boolean).join(' · ');
    const dt = $('#trim-display-title'), da = $('#trim-display-artist');
    dt.value = c.display_title || '';
    da.value = c.display_artist || '';
    dt.placeholder = 'Song title' + (w?.title && !c.display_title ? ` (auto: ${w.title})` : '');
    da.placeholder = 'Artist' + (w?.artist && !c.display_artist ? ` (auto: ${w.artist})` : '');
    $('#trim-prev').disabled = idx <= 0;
    $('#trim-next').disabled = idx >= S.project.clips.length - 1;
    this.L = S.project.settings.clip_seconds;
    const ready = st.kind === 'ready' || st.kind === 'short';
    const msg = $('#trim-video-msg');
    const video = this.video();
    $$('#trimmer .trim-controls button, #trimmer .snap').forEach(b => { b.disabled = !ready; });
    if (!ready) {
      this.key = null; this.a = null; this.vdur = 0;
      video.removeAttribute('src'); video.load();
      msg.textContent = st.kind === 'err' ? `Download failed: ${st.err || ''}` : `Not ready yet — ${st.text}`;
      msg.classList.remove('hidden');
      this.draw();
      return;
    }
    msg.classList.add('hidden');
    if (this.key !== c.media_key || !this.a) {
      this.key = c.media_key;
      this.a = await api.get('/api/media/' + encodeURIComponent(c.media_key));
      video.src = '/media/' + encodeURIComponent(c.media_key);
      video.onerror = () => { msg.textContent = 'This video format can’t be previewed here, but it will still render fine. Use the timeline to pick the minute.'; msg.classList.remove('hidden'); };
    } else {
      this.a = await api.get('/api/media/' + encodeURIComponent(c.media_key));
    }
    this.vdur = this.a.duration || m.duration || 0;
    this.short = this.vdur <= this.L + 0.05;
    this.section = this.a.section || null;
    $('#trim-excerpt').classList.toggle('hidden', !this.section);
    if (this.section) $('#trim-excerpt-text').textContent = `This is ${fmtTime(this.section[0])} – ${fmtTime(this.section[1])} of a longer video.`;
    this.dur = this.short ? this.vdur : this.L;
    this.start = this.short ? 0 : (c.start ?? this.a.auto[0]);
    this.clampStart();
    const sg = this.a.suggestions || {};
    $('#snap-heatmap').textContent = sg.heatmap != null ? fmtTime(sg.heatmap) : 'no data';
    $('#snap-loudness').textContent = sg.loudness != null ? fmtTime(sg.loudness) : '–';
    $('#snap-auto').textContent = this.short ? 'whole video' : fmtTime(this.a.auto[0]);
    $('[data-snap=heatmap]').disabled = sg.heatmap == null || this.short;
    $('[data-snap=loudness]').disabled = sg.loudness == null || this.short;
    this.updateReadout();
    try { video.currentTime = this.start; } catch { /* not loaded yet */ }
    video.addEventListener('loadedmetadata', () => { if (!this.playingClip) video.currentTime = this.start; }, { once: true });
    this.draw();
  },

  sync() {
    if (!this.id) return;
    const c = clipById(this.id);
    if (!c) return this.close();
    const st = clipStatus(c);
    if (!this.a && (st.kind === 'ready' || st.kind === 'short')) this.load();
    else if (this.a && S.project.settings.clip_seconds !== this.L) this.load();
  },

  close() {
    this.stopClip();
    this.video().pause();
    this.flush();
    this.id = null;
    this.el().classList.add('hidden');
  },

  clampStart() {
    this.start = this.short ? 0 : clamp(this.start, 0, Math.max(0, this.vdur - this.dur - 0.05));
  },

  source() {
    const c = clipById(this.id);
    if (this.short) return S.project.settings.short_mode === 'loop' ? 'loop' : 'full';
    return c?.start == null ? (this.a?.auto?.[1] || 'default') : 'manual';
  },

  updateReadout() {
    const inp = $('#trim-start');
    if (document.activeElement !== inp) inp.value = fmtTime(this.start, true);
    $('#trim-end').textContent = fmtTime(this.start + this.dur, true);
    const src = this.source();
    const inOriginal = this.section && !this.short ? ` · ${fmtTime(this.section[0] + this.start)} in the original` : '';
    $('#trim-source').textContent = this.short
      ? `This video is only ${fmtTime(this.vdur)} long — it ${src === 'loop' ? 'loops to fill the minute' : 'plays in full'} (change under Songs → Short videos).`
      : `Chosen by: ${SRC_LABEL[src] || src}${inOriginal}`;
    $$('#trimmer .snap').forEach(b => b.classList.toggle('active',
      (b.dataset.snap === 'auto' && src !== 'manual') || (src === 'manual' && Math.abs((this.a?.suggestions?.[b.dataset.snap] ?? -99) - this.start) < 0.05)));
  },

  setStart(t, { save = true, seek = true } = {}) {
    if (this.short || !this.vdur) return;
    this.start = t;
    this.clampStart();
    if (save) {
      const c = clipById(this.id);
      if (c) c.start = this.start;
      this.unsaved = this.start;
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => this.flush(), 300);
    }
    this.updateReadout();
    this.draw();
    if (seek) { this.video().currentTime = this.start; }
  },

  flush() {
    clearTimeout(this.saveTimer);
    this.saveTimer = null;
    if (this.unsaved === undefined || !this.id) return;
    const start = this.unsaved;
    this.unsaved = undefined;
    api.post('/api/clips/update', { id: this.id, start }).then(poll).catch(e => toast(e.message, 'err'));
  },

  snap(kind) {
    if (!this.a) return;
    if (kind === 'auto') {
      const c = clipById(this.id);
      if (c) c.start = null;
      this.start = this.a.auto[0];
      this.clampStart();
      clearTimeout(this.saveTimer);
      this.unsaved = undefined;
      api.post('/api/clips/update', { id: this.id, start: null }).then(poll);
      this.updateReadout();
      this.draw();
      this.video().currentTime = this.start;
      return;
    }
    const v = this.a.suggestions?.[kind];
    if (v != null) this.setStart(v);
  },

  playClip() {
    const v = this.video();
    if (this.playingClip) return this.stopClip(true);
    v.currentTime = this.start;
    v.play().catch(() => {});
    this.playingClip = true;
    $('#trim-play').textContent = '⏸ Pause';
    this.tick();
  },

  stopClip(pause = true) {
    this.playingClip = false;
    $('#trim-play').textContent = '▶ Play the minute';
    if (pause) this.video().pause();
  },

  tick() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      const v = this.video();
      if (this.playingClip && v.currentTime >= this.start + this.dur) {
        this.stopClip();
        v.currentTime = this.start;
      }
      this.draw();
      if (!v.paused || this.playingClip) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  },

  geom() {
    const cv = $('#trim-canvas');
    const W = cv.clientWidth, H = cv.clientHeight;
    return { cv, W, H, top: 8, bottom: H - 20 };
  },
  tAt(clientX) {
    const { cv, W } = this.geom();
    const x = clientX - cv.getBoundingClientRect().left;
    return clamp((x / W) * this.vdur, 0, this.vdur);
  },

  draw() {
    const { cv, W, H, top, bottom } = this.geom();
    if (!W) return;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const vd = this.vdur;
    if (!vd) {
      ctx.fillStyle = '#6f6488';
      ctx.font = '13px Segoe UI';
      ctx.fillText('Waiting for the download…', 12, H / 2);
      return;
    }
    const ph = bottom - top;
    const X = t => (t / vd) * W;
    // time grid
    const stepS = vd > 900 ? 120 : vd > 360 ? 60 : vd > 120 ? 30 : vd > 40 ? 10 : 5;
    ctx.font = '11px Segoe UI';
    ctx.textBaseline = 'alphabetic';
    for (let t = 0; t <= vd; t += stepS) {
      const x = Math.round(X(t)) + 0.5;
      ctx.strokeStyle = 'rgba(255,255,255,.06)';
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
      ctx.fillStyle = '#7d7398';
      ctx.fillText(fmtTime(t), Math.min(x + 3, W - 30), H - 5);
    }
    const loud = this.a?.loudness || [];
    if (loud.length) {
      ctx.beginPath();
      ctx.moveTo(0, bottom);
      loud.forEach((db, i) => ctx.lineTo(X(i + 0.5), bottom - clamp((db + 50) / 50, 0, 1) * ph));
      ctx.lineTo(X(loud.length), bottom);
      ctx.closePath();
      ctx.fillStyle = 'rgba(56,214,255,.13)';
      ctx.fill();
      ctx.strokeStyle = 'rgba(56,214,255,.55)';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    const hm = this.a?.heatmap || [];
    if (hm.length) {
      const g = ctx.createLinearGradient(0, top, 0, bottom);
      g.addColorStop(0, 'rgba(255,90,60,.85)');
      g.addColorStop(1, 'rgba(255,170,60,.25)');
      ctx.beginPath();
      ctx.moveTo(0, bottom);
      hm.forEach(([s, e, v]) => ctx.lineTo(X((s + e) / 2), bottom - v * ph * 0.95));
      ctx.lineTo(W, bottom);
      ctx.closePath();
      ctx.fillStyle = g;
      ctx.fill();
    }
    // suggestion markers
    const sg = this.a?.suggestions || {};
    for (const [k, color] of [['heatmap', '#ff7a3d'], ['loudness', '#38d6ff']]) {
      if (sg[k] == null || this.short) continue;
      const x = X(sg[k] + this.dur / 2);
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.moveTo(x - 5, 0); ctx.lineTo(x + 5, 0); ctx.lineTo(x, 7); ctx.fill();
    }
    // the minute
    const x0 = X(this.start), x1 = Math.max(X(this.start + this.dur), x0 + 4);
    ctx.fillStyle = 'rgba(255,196,0,.20)';
    ctx.fillRect(x0, top - 6, x1 - x0, ph + 12);
    ctx.strokeStyle = '#ffc400';
    ctx.lineWidth = 2;
    ctx.strokeRect(x0 + 1, top - 5, x1 - x0 - 2, ph + 10);
    if (!this.short) {
      ctx.fillStyle = '#ffc400';
      for (const gx of [x0 + 1, x1 - 5]) ctx.fillRect(gx, top + ph / 2 - 10, 4, 20);
    }
    ctx.fillStyle = '#ffdb5c';
    ctx.font = 'bold 12px Segoe UI';
    const label = `${Math.round(this.dur)}s`;
    const lw = ctx.measureText(label).width;
    if (x1 - x0 > lw + 10) ctx.fillText(label, (x0 + x1) / 2 - lw / 2, top + 10);
    // playhead
    const v = this.video();
    if (v.readyState > 0) {
      const px = Math.round(X(v.currentTime)) + 0.5;
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, bottom + 2); ctx.stroke();
    }
    if (this.hoverT != null) {
      const hx = X(this.hoverT);
      const txt = fmtTime(this.hoverT);
      ctx.font = '11px Segoe UI';
      const tw = ctx.measureText(txt).width + 8;
      const bx = clamp(hx - tw / 2, 0, W - tw);
      ctx.fillStyle = 'rgba(0,0,0,.75)';
      ctx.fillRect(bx, bottom - 16, tw, 16);
      ctx.fillStyle = '#fff';
      ctx.fillText(txt, bx + 4, bottom - 4);
    }
  },

  initEvents() {
    const cv = $('#trim-canvas');
    const v = this.video();
    cv.addEventListener('pointerdown', e => {
      if (!this.vdur) return;
      const t = this.tAt(e.clientX);
      cv.setPointerCapture(e.pointerId);
      if (!this.short && t >= this.start && t <= this.start + this.dur) {
        this.drag = { offset: t - this.start };
        cv.style.cursor = 'grabbing';
      } else {
        this.drag = { scrub: true };
        v.currentTime = t;
        this.draw();
      }
    });
    cv.addEventListener('pointermove', e => {
      if (!this.vdur) return;
      const t = this.tAt(e.clientX);
      this.hoverT = t;
      if (this.drag?.offset != null) this.setStart(t - this.drag.offset, { save: false, seek: false });
      else if (this.drag?.scrub) { v.currentTime = t; }
      else cv.style.cursor = (!this.short && t >= this.start && t <= this.start + this.dur) ? 'grab' : 'pointer';
      this.draw();
    });
    cv.addEventListener('pointerup', () => {
      if (this.drag?.offset != null) { this.setStart(this.start); }
      this.drag = null;
      cv.style.cursor = '';
    });
    cv.addEventListener('pointerleave', () => { this.hoverT = null; this.draw(); });
    cv.addEventListener('dblclick', e => { if (this.vdur) this.setStart(this.tAt(e.clientX) - this.dur / 2); });
    v.addEventListener('play', () => this.tick());
    v.addEventListener('seeked', () => this.draw());
    v.addEventListener('pause', () => { if (this.playingClip && v.currentTime < this.start + this.dur - 0.1) this.stopClip(false); this.draw(); });
    v.addEventListener('click', () => (v.paused ? v.play() : v.pause()));
    window.addEventListener('resize', () => this.id && this.draw());

    $('#trim-play').addEventListener('click', () => this.playClip());
    $$('[data-nudge]').forEach(b => b.addEventListener('click', () => this.setStart(this.start + Number(b.dataset.nudge))));
    $$('#trimmer .snap').forEach(b => b.addEventListener('click', () => this.snap(b.dataset.snap)));
    $('#trim-at-playhead').addEventListener('click', () => this.setStart(v.currentTime));
    $('#trim-start').addEventListener('change', e => {
      const t = parseTime(e.target.value);
      if (t != null) this.setStart(t); else this.updateReadout();
    });
    $('#trim-close').addEventListener('click', () => this.close());
    $('#trim-done').addEventListener('click', () => this.close());
    $('#trimmer').addEventListener('mousedown', e => { if (e.target.id === 'trimmer') this.close(); });
    $('#trim-prev').addEventListener('click', () => this.step(-1));
    $('#trim-next').addEventListener('click', () => this.step(1));
    const saveText = debounce((field, value) => api.post('/api/clips/update', { id: this.id, [field]: value }).then(poll), 400);
    $('#trim-display-title').addEventListener('input', e => saveText('display_title', e.target.value));
    $('#trim-display-artist').addEventListener('input', e => saveText('display_artist', e.target.value));
    $('#trim-replace').addEventListener('click', () => {
      const c = clipById(this.id);
      const m = (c.media_key && S.media[c.media_key]) || {};
      const id = this.id;
      this.close();
      const q = m.title || c.title || c.query || '';
      $('#search-input').value = q;
      doSearch(q, 'replace', id);
    });
    $('#trim-remove').addEventListener('click', () => {
      const id = this.id;
      const idx = S.project.clips.findIndex(c => c.id === id);
      const next = S.project.clips[idx + 1] || S.project.clips[idx - 1];
      removeClips([id]);
      if (next) this.open(next.id); else this.close();
    });
    document.addEventListener('keydown', e => {
      if (!this.id) return;
      if (e.key === 'Escape') { e.preventDefault(); return this.close(); }
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
      if (e.key === ' ') { e.preventDefault(); this.playClip(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); this.setStart(this.start - (e.shiftKey ? 5 : 1)); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); this.setStart(this.start + (e.shiftKey ? 5 : 1)); }
      else if (e.key === 'Enter') { e.preventDefault(); this.step(1); }
    });
  },

  step(d) {
    const idx = S.project.clips.findIndex(c => c.id === this.id);
    const next = S.project.clips[idx + d];
    if (!next) return;
    this.flush();
    this.open(next.id);
  },
};

// ============================================================================================
// previews (still frames + short renders)
// ============================================================================================
const previews = {
  sigs: {},
  timer: null,
  reset() { this.sigs = {}; $$('.preview-video').forEach(v => { v.pause(); v.removeAttribute('src'); v.classList.add('hidden'); }); },
  changed() { clearTimeout(this.timer); this.timer = setTimeout(() => this.update(), 350); },

  update() {
    if (!S.project) return;
    const p = S.project;
    if (S.tab === 'cards') for (const which of ['intro', 'outro']) {
      const bgKey = p[which].bg.media_key;
      const sig = JSON.stringify([p[which], p.settings.resolution, S.summary?.ready, $(`#${which}-t`)?.value,
        bgKey && S.media[bgKey]?.status, p.name]);
      if (this.sigs[which] !== sig) {
        this.sigs[which] = sig;
        const t = $(`#${which}-t`);
        t.max = p[which].duration;
        this.frame($(`#${which}-frame`), `kind=${which}&t=${t.value}`, $(`#${which}-video`));
      }
    }
    if (S.tab === 'transitions') {
      const st = p.transition.stinger;
      const sig = JSON.stringify([st, p.overlays.font, p.settings.resolution, st.media_key && S.media[st.media_key]?.status]);
      if (st.enabled && this.sigs.stinger !== sig) {
        this.sigs.stinger = sig;
        if ($('#trans-video').classList.contains('hidden')) this.frame($('#stinger-frame'), 'kind=stinger&t=1', null, $('#trans-empty'));
      }
      const sh = p.transition.shot || {};
      const shotSig = JSON.stringify([sh, p.overlays.font, p.settings.resolution, sh.media_key && S.media[sh.media_key]?.status]);
      if (this.sigs.shot !== shotSig) {
        this.sigs.shot = shotSig;
        this.frame($('#shot-frame'), 'kind=shot&t=1.5', $('#shot-video'));
      }
    }
    if (S.tab === 'look') this.updateLook();
  },

  updateLook() {
    const sel = $('#look-clip');
    const ready = S.project.clips.filter(c => S.summary?.windows?.[c.id]);
    const cur = sel.value;
    const opts = ready.map(c => `${c.id}`).join(',');
    if (sel.dataset.opts !== opts) {
      sel.dataset.opts = opts;
      sel.replaceChildren(...ready.map(c => new Option(`${clipNumber(c)}. ${S.summary.windows[c.id].title}`, c.id)));
      if (ready.some(c => c.id === cur)) sel.value = cur;
    }
    $('#look-empty').classList.toggle('hidden', ready.length > 0);
    $('#look-frame').classList.toggle('hidden', !ready.length);
    if (!ready.length) return;
    const w = S.summary.windows[sel.value];
    const range = $('#look-time');
    range.max = w ? w.dur : 60;
    $('#look-time-out').textContent = fmtTime(+range.value);
    const p = S.project;
    const tk = p.overlays.trivia?.bg?.media_key;
    const sig = JSON.stringify([p.overlays, p.settings, p.transition.duration, p.transition.effect, sel.value, range.value, w,
      tk && S.media[tk]?.status]);
    if (this.sigs.look === sig) return;
    this.sigs.look = sig;
    this.frame($('#look-frame'), `kind=clip&id=${sel.value}&t=${range.value}`);
  },

  frame(img, query, video = null, empty = null) {
    const box = img.closest('.preview-box');
    let ld = $('.loading', box);
    if (!ld) { ld = h('div', { class: 'loading' }, 'Rendering preview…'); box.append(ld); }
    ld.classList.remove('hidden');
    const url = `/api/frame?${query}&w=960&_=${Date.now()}`;
    const done = () => ld.classList.add('hidden');
    img.onload = () => { done(); img.classList.remove('hidden'); if (empty) empty.classList.add('hidden'); };
    img.onerror = () => done();
    img.src = url;
    if (video) { video.pause(); video.classList.add('hidden'); }
  },

  async render(kind, video, btn, hideEls = []) {
    await withBusy(btn, async () => {
      const r = await api.post('/api/preview', { kind });
      hideEls.forEach(e => e?.classList.add('hidden'));
      video.src = r.url;
      video.classList.remove('hidden');
      video.play().catch(() => {});
    });
  },
};

// ============================================================================================
// intro / outro editors
// ============================================================================================
function cardEditor(P) {
  const intro = P === 'intro';
  return `
  <div class="panel card-editor" id="editor-${P}">
    <div class="panel-head"><h3>${intro ? '🎬 Intro' : '🏁 Outro'}</h3><label class="toggle"><input type="checkbox" data-bind="${P}.enabled"><span></span>Include</label></div>
    <div class="preview-box"><img class="preview-img" id="${P}-frame" alt=""><video class="preview-video hidden" id="${P}-video" controls></video></div>
    <div class="field-row"><button class="btn small gold" id="btn-preview-${P}">▶ Play preview</button><span class="muted small">Scrub:</span><input type="range" id="${P}-t" min="0" max="10" step="0.25" value="${intro ? 2 : 3}"></div>
    <div class="form-grid mt">
      <label>Length</label><div class="inline"><input type="range" min="3" max="60" step="1" data-bind="${P}.duration" data-type="number"><output data-for="${P}.duration" data-suffix=" s"></output></div>
      <label>Title</label><input data-bind="${P}.title">
      <label>Subtitle</label><input data-bind="${P}.subtitle">
      <label></label><span class="muted small">Placeholders: <code>{count}</code> songs · <code>{minutes}</code> · <code>{name}</code> project name</span>
      <label>Font</label><select data-bind="${P}.font" data-options="fonts"></select>
      <label>Text colors</label><div class="inline"><input type="color" data-bind="${P}.color" title="Title"><input type="color" data-bind="${P}.sub_color" title="Subtitle"></div>
      <label>Background</label><div class="seg" data-bind="${P}.bg.type"><button data-value="gradient">Gradient</button><button data-value="color">Color</button><button data-value="image">Image</button><button data-value="media">Video</button></div>
      <label data-show="${P}.bg.type==gradient|color">Colors</label><div class="inline" data-show="${P}.bg.type==gradient|color"><input type="color" data-bind="${P}.bg.c1"><input type="color" data-bind="${P}.bg.c2" data-show="${P}.bg.type==gradient"></div>
      <label data-show="${P}.bg.type==gradient">Style</label><div class="inline" data-show="${P}.bg.type==gradient"><select data-bind="${P}.bg.style"><option value="linear">Linear</option><option value="radial">Radial</option><option value="circular">Circular</option><option value="spiral">Spiral</option><option value="square">Square</option></select><label class="toggle"><input type="checkbox" data-bind="${P}.bg.animated"><span></span>Animated</label></div>
      <label data-show="${P}.bg.type==image">Image</label><div class="assetpick" data-asset="${P}.bg.image" data-kind="image" data-show="${P}.bg.type==image"></div>
      <label data-show="${P}.bg.type==media">Video</label><div class="mediapick" data-mediapick="${P}.bg.media_key" data-start="${P}.bg.start" data-show="${P}.bg.type==media"></div>
      <label data-show="${P}.bg.type==image|media">Darken</label><div class="inline" data-show="${P}.bg.type==image|media"><input type="range" min="0" max="0.8" step="0.05" data-bind="${P}.bg.dim" data-type="number"><output data-for="${P}.bg.dim" data-percent></output></div>
      <label>Audio</label><div class="seg" data-bind="${P}.audio.type"><button data-value="none">Silent</button><button data-value="sfx">Sound effect</button><button data-value="file">Music file</button><button data-value="media" data-show="${P}.bg.type==media">Video's sound</button></div>
      <label data-show="${P}.audio.type==sfx">Sound</label><div class="inline" data-show="${P}.audio.type==sfx"><select data-bind="${P}.audio.sfx" data-options="sounds"></select><button class="btn small" data-play-sound="${P}.audio.sfx">▶</button></div>
      <label data-show="${P}.audio.type==file">Music</label><div class="assetpick" data-asset="${P}.audio.file" data-kind="audio" data-show="${P}.audio.type==file"></div>
      <label data-show="${P}.audio.type==file">Start at</label><div class="inline" data-show="${P}.audio.type==file"><input type="number" class="num" min="0" step="1" data-bind="${P}.audio.start" data-type="number"><span class="unit">sec into the song</span></div>
      <label data-show="${P}.audio.type!=none">Volume</label><div class="inline" data-show="${P}.audio.type!=none"><input type="range" min="0" max="2" step="0.05" data-bind="${P}.audio.volume" data-type="number"><output data-for="${P}.audio.volume" data-percent></output></div>
      <label>Countdown</label><div class="inline"><input type="range" min="0" max="10" step="1" data-bind="${P}.countdown" data-type="number"><output data-for="${P}.countdown" data-suffix=" s" data-zero="off"></output><label class="toggle"><input type="checkbox" data-bind="${P}.countdown_beeps"><span></span>Beeps</label></div>
      <label>${intro ? 'Into song 1' : 'After last song'}</label><div class="inline"><select data-bind="${P}.transition.effect" data-options="effects"></select><input type="range" min="0" max="3" step="0.1" data-bind="${P}.transition.duration" data-type="number"><output data-for="${P}.transition.duration" data-suffix=" s"></output></div>
    </div>
  </div>`;
}

// ============================================================================================
// transitions tab
// ============================================================================================
const FX_ANIM = {
  cut: 'fxcut', fade: 'fxfade', dissolve: 'fxpix', fadeblack: 'fxblack', fadewhite: 'fxwhite', fadegrays: 'fxfade',
  pixelize: 'fxpix', radial: 'fxcircle', circleopen: 'fxcircle', circleclose: 'fxcircle', zoomin: 'fxzoom',
  hblur: 'fxpix', distance: 'fxzoom', wipeleft: 'fxwipeL', wiperight: 'fxwipeR', wipeup: 'fxwipeU',
  wipedown: 'fxwipeD', slideleft: 'fxslideL', slideright: 'fxslideR', slideup: 'fxslideU', slidedown: 'fxslideD',
  smoothleft: 'fxwipeL', smoothright: 'fxwipeR', coverleft: 'fxslideL', revealright: 'fxwipeR', squeezeh: 'fxbarn',
  diagtl: 'fxdiag', hlslice: 'fxwipeL', vertopen: 'fxbarn', horzclose: 'fxshutter', hlwind: 'fxwipeL',
};
function buildEffectGrid() {
  const grid = $('#effect-grid');
  grid.setAttribute('data-bind', 'transition.effect');
  grid.replaceChildren(...S.meta.effects.map(e => h('div', { class: 'effect', 'data-value': e.id, title: e.label },
    h('div', { class: 'fx', style: { '--anim': FX_ANIM[e.id] || 'fxfade' } }), e.label)));
}

// ============================================================================================
// render tab
// ============================================================================================
function renderChecklist() {
  const sm = S.summary;
  if (!sm) return;
  const st = S.project.settings;
  const items = [];
  if (sm.ready >= sm.target) items.push(['ok', '✓', `${sm.ready} songs ready (target ${sm.target})`]);
  else if (sm.ready) items.push(['warn', '!', `Only ${sm.ready} of ${sm.target} songs ready — you can still render a shorter video.`]);
  else items.push(['bad', '✗', 'No songs downloaded yet — add some on the Songs tab.']);
  if (sm.pending) items.push(['warn', '⏳', `${sm.pending} still downloading — they’ll be left out if you render now.`]);
  if (sm.error) items.push(['bad', '✗', `${sm.error} failed — retry or remove them on the Songs tab.`]);
  if (sm.short) items.push(['warn', 'ℹ', `${sm.short} video${sm.short === 1 ? ' is' : 's are'} shorter than ${st.clip_seconds}s — ${st.short_mode === 'loop' ? 'they loop to fill the time' : 'they play in full'}.`]);
  items.push(['ok', '⏱', `Total runtime ${fmtTime(sm.duration)} at ${st.resolution} ${st.fps}fps`]);
  $('#checklist').replaceChildren(...items.map(([k, ic, t]) => h('div', { class: 'check ' + k }, h('span', { class: 'ic' }, ic), h('span', {}, t))));
  const enc = S.meta?.encoders.find(e => e.id === sm.encoder);
  $('#encoder-label').textContent = enc ? enc.label + (sm.encoder !== 'libx264' ? ' (GPU — fast)' : '') : sm.encoder || '';
  $('#btn-render').disabled = !sm.ready || S.render?.status === 'running';
  if (S.tab === 'render') loadTracklist();
}

const loadTracklist = debounce(async () => {
  try {
    const t = await fetch('/api/tracklist').then(r => r.text());
    $('#tracklist').textContent = t;
  } catch { /* ignore */ }
}, 500);

let lastRenderStatus = null;
function renderRenderStatus() {
  const r = S.render || {};
  const panel = $('#render-progress');
  const running = r.status === 'running';
  const mine = !r.project || r.project === S.project?.id;
  panel.classList.toggle('hidden', !r.status || r.status === 'idle' || (!mine && !running));
  $('#btn-render').disabled = running || !S.summary?.ready;
  $('#btn-export').disabled = running || !S.summary?.ready;
  $('#btn-quick').disabled = running;
  $('#btn-cancel').classList.toggle('hidden', !running);
  if (r.status && r.status !== 'idle') {
    const pct = Math.round((r.progress || 0) * 100);
    $('#render-bar').style.width = pct + '%';
    $('#render-pct').textContent = pct + '%';
    const what = r.kind === 'export' ? 'Export' : 'Render';
    $('#render-stage').textContent = running ? `${what}: ${r.stage}` : r.status === 'done' ? `✅ ${what} done!` : r.status === 'cancelled' ? 'Cancelled' : `❌ ${what} failed`;
    const bits = [`elapsed ${fmtTime(r.elapsed)}`];
    if (running && r.eta != null) bits.push(`about ${fmtTime(r.eta)} left`);
    if (r.cached) bits.push(`${r.cached} pieces reused from last time`);
    $('#render-times').textContent = bits.join(' · ');
    const err = $('#render-error');
    err.classList.toggle('hidden', !r.error);
    err.textContent = r.error || '';
    $('#btn-goto-render').textContent = running ? `⏳ Rendering ${pct}%` : '▶ Render';
  }
  if (lastRenderStatus === 'running' && r.status === 'done' && r.kind === 'export') {
    toast('🎞 Export ready - opening the folder', 'ok');
    api.post('/api/open_export', { name: r.output.split(/[\\/]/).pop() });
    loadExports();
  } else if (lastRenderStatus === 'running' && r.status === 'done') {
    toast('🎉 Your power hour is ready!', 'ok');
    showResult(r.output.split(/[\\/]/).pop());
    loadOutputs();
  } else if (lastRenderStatus === 'running' && r.status === 'error') {
    toast('Render failed — see the Render tab', 'err');
  }
  lastRenderStatus = r.status;
}

function showResult(name, url = null) {
  const v = $('#result-video');
  v.src = url || '/output/' + encodeURIComponent(name);
  v.classList.remove('hidden');
  $('#result-empty').classList.add('hidden');
  $('#result-title').textContent = name || 'Quick preview';
  $('#result-actions').classList.toggle('hidden', !name);
  $('#result-actions').dataset.name = name || '';
}

async function loadOutputs() {
  const r = await api.get('/api/outputs');
  $('#outputs').replaceChildren(...(r.files.length ? r.files.map(f => h('div', { class: 'output' },
    h('span', { class: 'n', title: f.name }, f.name),
    h('span', { class: 'muted small' }, fmtBytes(f.size)),
    h('button', { class: 'btn small', title: 'Watch here', onclick: () => showResult(f.name) }, '▶'),
    h('button', { class: 'btn small', title: 'Show in folder', onclick: () => api.post('/api/open', { target: 'output-file', name: f.name }) }, '📂'))) : [h('div', { class: 'muted small' }, 'Nothing rendered yet.')]));
}

// ============================================================================================
// modals: projects + settings
// ============================================================================================
function openModal(...kids) {
  $('#modal-box').replaceChildren(...kids);
  $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); }

async function openProjects() {
  const r = await api.get('/api/projects');
  const name = h('input', { placeholder: 'New project name', style: { flex: 1 } });
  const create = h('button', { class: 'btn gold' }, '+ New project');
  create.addEventListener('click', () => withBusy(create, async () => {
    await api.post('/api/projects/new', { name: name.value });
    closeModal(); S.rev = null; poll();
  }));
  name.addEventListener('keydown', e => { if (e.key === 'Enter') create.click(); });
  const list = h('div', { class: 'proj-list' }, ...r.projects.map(p => h('div', {
    class: 'proj' + (p.id === r.current ? ' current' : ''),
    onclick: async () => { if (p.id !== r.current) { await api.post('/api/projects/open', { id: p.id }); S.rev = null; poll(); } closeModal(); },
  },
  h('span', { class: 'n' }, p.name),
  h('span', { class: 'muted small' }, `${p.clips} songs · ${new Date(p.updated * 1000).toLocaleDateString()}`),
  h('button', { class: 'btn small ghost', title: 'Delete project', onclick: async e => {
    e.stopPropagation();
    if (!confirm(`Delete “${p.name}”? Downloaded videos stay in the cache.`)) return;
    await api.post('/api/projects/delete', { id: p.id }).catch(err => toast(err.message, 'err'));
    closeModal(); S.rev = null; poll();
  } }, '🗑'))));
  openModal(
    h('div', { class: 'modal-head' }, h('h3', {}, '📁 Projects'), h('button', { class: 'btn ghost icon', onclick: closeModal }, '✕')),
    h('div', { class: 'modal-body' }, list),
    h('div', { class: 'modal-foot' }, name, create,
      h('button', { class: 'btn', onclick: async () => { await api.post('/api/projects/duplicate'); closeModal(); S.rev = null; poll(); toast('Project duplicated'); } }, 'Duplicate current')),
  );
}

async function openSettings() {
  const meta = await api.get('/api/meta');
  S.meta = { ...S.meta, ...meta };
  const st = meta.settings;
  const save = (k, v) => api.post('/api/settings', { [k]: v }).then(() => toast('Saved', 'ok')).catch(e => toast(e.message, 'err'));
  const sel = (k, options, cast = String) => {
    const s = h('select', { onchange: e => save(k, cast(e.target.value)) }, ...options.map(([v, l]) => new Option(l, v)));
    s.value = String(st[k] ?? '');
    return s;
  };
  const storage = h('div', { class: 'muted small' }, 'Measuring…');
  api.get('/api/storage').then(s => { storage.textContent = `Downloads ${fmtBytes(s.cache)} · Render cache ${fmtBytes(s.work)} · Finished videos ${fmtBytes(s.output)}`; });
  const ytBtn = h('button', { class: 'btn small' }, 'Update yt-dlp');
  const ytLog = h('pre', { class: 'tracklist hidden' });
  ytBtn.addEventListener('click', () => withBusy(ytBtn, async () => {
    await api.post('/api/ytdlp/update');
    for (;;) {
      await new Promise(r => setTimeout(r, 1000));
      const s = await api.get('/api/ytdlp/update');
      if (s.status !== 'running') {
        ytLog.textContent = s.log;
        ytLog.classList.remove('hidden');
        toast(s.status === 'done' ? 'yt-dlp updated — restart the app to use it' : 'Update failed', s.status === 'done' ? 'ok' : 'err');
        break;
      }
    }
  }));
  const clearBtn = (label, what, confirmText) => {
    const b = h('button', { class: 'btn small' }, label);
    b.addEventListener('click', () => withBusy(b, async () => {
      if (confirmText && !confirm(confirmText)) return;
      const r = await api.post('/api/storage/clear', { what });
      toast(r.removed != null ? `Removed ${r.removed} videos` : 'Cleared', 'ok');
      const s = await api.get('/api/storage');
      storage.textContent = `Downloads ${fmtBytes(s.cache)} · Render cache ${fmtBytes(s.work)} · Finished videos ${fmtBytes(s.output)}`;
    }));
    return b;
  };
  openModal(
    h('div', { class: 'modal-head' }, h('h3', {}, '⚙ Settings'), h('button', { class: 'btn ghost icon', onclick: closeModal }, '✕')),
    h('div', { class: 'modal-body', style: { width: '640px' } },
      h('div', { class: 'form-grid' },
        h('label', {}, 'Download quality'), sel('max_height', [[480, 'Up to 480p'], [720, 'Up to 720p'], [1080, 'Up to 1080p'], [1440, 'Up to 1440p'], [2160, 'Up to 4K']], Number),
        h('label', {}, 'Parallel downloads'), sel('concurrent_downloads', [1, 2, 3, 4, 5, 6].map(n => [n, n]), Number),
        h('label', { title: 'A short random wait before each download. Without it YouTube may start asking to "confirm you're not a bot" after about a hundred videos.' }, 'Pause between downloads'),
        sel('download_pause', [[0, 'None'], [3, '3-6 s (recommended)'], [6, '6-12 s'], [10, '10-20 s']], Number),
        h('label', {}, 'Video encoder'), sel('encoder', [['auto', 'Automatic (GPU if available)'], ...meta.encoders.map(e => [e.id, e.label])]),
        h('label', {}, 'Render jobs at once'), sel('render_workers', [1, 2, 3, 4, 5, 6].map(n => [n, n]), Number),
        h('label', {}, 'Browser cookies'), sel('cookies_browser', [['', 'None'], ['firefox', 'Firefox'], ['chrome', 'Chrome'], ['edge', 'Edge'], ['brave', 'Brave'], ['opera', 'Opera'], ['vivaldi', 'Vivaldi']]),
        h('label', {}), h('span', { class: 'muted small' }, 'Only needed if YouTube asks to “confirm you’re not a bot” or for age-restricted videos. Firefox works best on Windows.'),
      ),
      h('h3', { class: 'mt' }, 'Storage'),
      storage,
      h('div', { class: 'field-row' },
        clearBtn('Clear render cache', 'work', null),
        clearBtn('Delete downloads no project uses', 'unused', 'Delete downloaded videos that no project uses?'),
        h('button', { class: 'btn small', onclick: () => api.post('/api/open', { target: 'data' }) }, '📂 Open data folder')),
      h('h3', { class: 'mt' }, 'Long videos'),
      h('div', { class: 'form-grid' },
        h('label', {}, 'Long if over'), sel('long_minutes', [[10, '10 minutes'], [15, '15 minutes'], [20, '20 minutes'], [30, '30 minutes'], [60, '1 hour']], Number),
        h('label', {}, 'Section length'), sel('section_minutes', [[5, '5 minutes'], [10, '10 minutes'], [15, '15 minutes']], Number),
        h('label', {}), h('span', { class: 'muted small' }, 'Movies, concerts and compilations are used through a section, so the minute is picked from a short excerpt instead of the whole thing.')),
      h('h3', { class: 'mt' }, 'Claude integration'),
      h('p', { class: 'muted small' }, 'Lets Claude drive this app - e.g. “make me a Halloween power hour”. Run install-claude.ps1 in the app folder once, or add it yourself:'),
      h('pre', { class: 'tracklist' }, meta.mcp_command || ''),
      h('div', { class: 'field-row' }, h('button', { class: 'btn small', onclick: async () => { await navigator.clipboard.writeText(meta.mcp_command || ''); toast('Copied', 'ok'); } }, 'Copy command')),
      h('h3', { class: 'mt' }, 'Downloader'),
      h('div', { class: 'field-row' }, h('span', { class: 'muted' }, `yt-dlp ${meta.ytdlp}`), ytBtn,
        h('span', { class: 'muted small' }, 'Update when downloads start failing — sites change often.')),
      ytLog),
    h('div', { class: 'modal-foot' }, h('span', { class: 'spacer' }), h('button', { class: 'btn gold', onclick: closeModal }, 'Close')),
  );
}

// ============================================================================================
// tabs + wiring
// ============================================================================================
function showTab(t) {
  S.tab = t;
  localStorage.setItem('tab', t);
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  $$('.tab-page').forEach(p => p.classList.toggle('active', p.id === 'tab-' + t));
  if (t === 'render') { loadOutputs(); loadTracklist(); loadExports(); }
  previews.sigs = { ...previews.sigs, look: null, intro: null, outro: null, stinger: null };
  previews.changed();
  if (t === 'transitions' && !S.project?.transition.stinger.enabled) $('#stinger-frame').classList.add('hidden');
}

function wire() {
  $$('#tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  $('#btn-goto-render').addEventListener('click', () => showTab('render'));
  $('#btn-settings').addEventListener('click', openSettings);
  $('#btn-projects').addEventListener('click', openProjects);
  $('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });

  $('#search-form').addEventListener('submit', e => {
    e.preventDefault();
    const q = $('#search-input').value.trim();
    if (q) doSearch(q, S.search.mode === 'replace' ? 'replace' : 'add', S.search.clipId);
  });
  $('#results-close').addEventListener('click', () => { $('#results').classList.add('hidden'); S.search = { mode: 'add' }; });
  $('#btn-bulk').addEventListener('click', openBulk);
  $('#btn-local').addEventListener('click', () => addLocalFiles().catch(e => toast(e.message, 'err')));
  $('#btn-folder').addEventListener('click', () => openFolder().catch(e => toast(e.message, 'err')));
  $('#btn-auto-sections').addEventListener('click', e => withBusy(e.currentTarget, async () => {
    const r = await api.post('/api/clips/auto_sections');
    toast(`Using the suggested part of ${r.sections} long video${r.sections === 1 ? '' : 's'}`, 'ok');
    poll();
  }));
  $('#trim-change-section').addEventListener('click', () => sectioner.open(trimmer.id, true));
  $('#btn-apply-theme').addEventListener('click', e => withBusy(e.currentTarget, async () => {
    const id = $('#theme-select').value;
    if (!confirm('Apply this theme? It replaces the colours, fonts, intro/outro texts and sounds.')) return;
    await api.post('/api/themes/apply', { id });
    toast('Theme applied', 'ok');
    S.rev = null;
    poll();
  }));
  $('#btn-export').addEventListener('click', e => withBusy(e.currentTarget, async () => {
    const formats = $$('#export-formats input:checked').map(x => x.value);
    if (!formats.length) return toast('Pick at least one format', 'err');
    await api.post('/api/export', { formats, handles: +$('#export-handles').value });
    lastRenderStatus = 'running';
    poll();
  }));
  sectioner.initEvents();
  $('#btn-shuffle').addEventListener('click', () => api.post('/api/clips/shuffle').then(poll));
  $('#btn-reset-starts').addEventListener('click', () => {
    if (confirm('Reset every song to its automatic start point (Most replayed / loudest)?')) api.post('/api/clips/reset_starts').then(poll);
  });
  $('#btn-retry-all').addEventListener('click', () => api.post('/api/clips/retry', { ids: [] }).then(poll));
  $('#btn-remove-failed').addEventListener('click', () => {
    const ids = S.project.clips.filter(c => clipStatus(c).kind === 'err').map(c => c.id);
    if (ids.length) removeClips(ids);
  });
  $('#search-source').addEventListener('change', e => api.post('/api/settings', { search_source: e.target.value }));

  $('#btn-preview-transition').addEventListener('click', e => previews.render('transition', $('#trans-video'), e.currentTarget, [$('#trans-empty'), $('#stinger-frame')]));
  $('#btn-preview-stinger').addEventListener('click', e => previews.render('stinger', $('#trans-video'), e.currentTarget, [$('#trans-empty'), $('#stinger-frame')]));
  $('#btn-preview-shot').addEventListener('click', e => previews.render('shot', $('#shot-video'), e.currentTarget, [$('#shot-frame')]));
  $('#btn-shots').addEventListener('click', openShots);
  for (const which of ['intro', 'outro']) {
    $(`#btn-preview-${which}`).addEventListener('click', e => previews.render(which, $(`#${which}-video`), e.currentTarget));
    $(`#${which}-t`).addEventListener('input', () => previews.changed());
  }
  $('#look-clip').addEventListener('change', () => previews.updateLook());
  $('#look-time').addEventListener('input', () => { $('#look-time-out').textContent = fmtTime(+$('#look-time').value); previews.changed(); });

  $('#btn-render').addEventListener('click', e => withBusy(e.currentTarget, async () => {
    await api.post('/api/render', { filename: $('#out-name').value });
    lastRenderStatus = 'running';
    poll();
  }));
  $('#btn-quick').addEventListener('click', e => withBusy(e.currentTarget, async () => {
    const r = await api.post('/api/preview', { kind: 'quick' });
    showResult(null, r.url);
    $('#result-video').play().catch(() => {});
  }));
  $('#btn-cancel').addEventListener('click', () => api.post('/api/render/cancel').then(poll));
  $('#btn-play-external').addEventListener('click', () => api.post('/api/open', { target: 'play', name: $('#result-actions').dataset.name }));
  $('#btn-reveal').addEventListener('click', () => api.post('/api/open', { target: 'output-file', name: $('#result-actions').dataset.name }));
  $('#btn-copy-tracklist').addEventListener('click', async () => {
    await navigator.clipboard.writeText($('#tracklist').textContent);
    toast('Tracklist copied — paste it into a YouTube description for chapters', 'ok');
  });

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f' && !trimmer.id) {
      e.preventDefault();
      showTab('songs');
      $('#search-input').focus();
      $('#search-input').select();
    }
    if (e.key === 'Escape' && !$('#modal').classList.contains('hidden')) closeModal();
  });
  trimmer.initEvents();
}

// ============================================================================================
// section picker: which part of a long video (movie, concert, compilation) to use
// ============================================================================================
const sectioner = {
  id: null, info: null, key: null, start: 0, len: 600, drag: null, hoverT: null, bigTimer: null,

  async open(id, backToTrimmer = false) {
    const c = clipById(id);
    if (!c || !c.media_key) return;
    trimmer.close();
    this.id = id;
    this.back = backToTrimmer;
    this.info = null;
    $('#sectioner').classList.remove('hidden');
    $('#sec-title').textContent = 'Loading…';
    $('#sec-sub').textContent = '';
    $('#sec-thumbs').replaceChildren();
    let info;
    try { info = await api.get('/api/longvideo/' + encodeURIComponent(c.media_key)); }
    catch (e) { toast(e.message, 'err'); return this.close(); }
    this.info = info;
    this.key = info.key;
    $('#sec-title').textContent = info.title || 'Long video';
    $('#sec-sub').textContent = `${fmtTime(info.duration)} long` + (info.local ? ' · file on this computer' : '');
    const sel = $('#sec-length');
    const want = Math.round(info.current ? info.current[1] - info.current[0] : info.section_seconds);
    if (![...sel.options].some(o => +o.value === want)) sel.append(new Option(`${Math.round(want / 60)} min`, want));
    sel.value = String(want);
    this.len = Math.min(+sel.value, info.duration);
    this.start = info.current ? info.current[0] : (info.suggestion ?? info.duration * 0.25);
    this.clamp();
    $('#sec-suggest').disabled = info.suggestion == null;
    $('#sec-note').textContent = info.local ? 'Your file stays untouched - the section is copied out of it.'
      : info.storyboard ? 'Pictures come from YouTube - nothing is downloaded until you confirm.'
        : 'No preview pictures for this site - use the time box.';
    const N = 30;
    $('#sec-thumbs').replaceChildren(...Array.from({ length: N }, (_, i) => {
      const t = (i + 0.5) * info.duration / N;
      const img = h('img', { src: `/api/thumb/${encodeURIComponent(this.key)}?t=${t.toFixed(1)}`, alt: '' });
      img.onerror = () => { img.style.visibility = 'hidden'; };
      return img;
    }));
    this.update();
    this.showBig(this.start + 5);
  },

  close() {
    const id = this.id;
    this.id = null;
    $('#sectioner').classList.add('hidden');
    if (this.back && id) { this.back = false; trimmer.open(id); }    // came from the trimmer: go back to it
  },

  clamp() {
    this.start = clamp(this.start, 0, Math.max(0, (this.info?.duration || 0) - this.len));
  },

  update() {
    const inp = $('#sec-start');
    if (document.activeElement !== inp) inp.value = fmtTime(this.start);
    $('#sec-range').textContent = `→ ${fmtTime(this.start + this.len)}`;
    this.draw();
  },

  showBig(t) {
    clearTimeout(this.bigTimer);
    this.bigTimer = setTimeout(() => {
      if (!this.key) return;
      const img = $('#sec-big');
      img.onerror = () => { img.removeAttribute('src'); };
      img.src = `/api/thumb/${encodeURIComponent(this.key)}?t=${Math.round(t)}&big=1`;
      $('#sec-big-time').textContent = fmtTime(t);
    }, 120);
  },

  geom() {
    const cv = $('#sec-canvas');
    return { cv, W: cv.clientWidth, H: cv.clientHeight };
  },
  tAt(x) {
    const { cv, W } = this.geom();
    return clamp(((x - cv.getBoundingClientRect().left) / W) * this.info.duration, 0, this.info.duration);
  },

  draw() {
    const { cv, W, H } = this.geom();
    if (!W || !this.info) return;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const D = this.info.duration, X = t => (t / D) * W, bar = 18, top = H - bar;
    ctx.fillStyle = 'rgba(10,8,16,.85)';
    ctx.fillRect(0, top, W, bar);
    const step = D > 5400 ? 900 : D > 2400 ? 600 : D > 1200 ? 300 : 120;
    ctx.font = '11px Segoe UI';
    for (let t = 0; t < D; t += step) {
      ctx.fillStyle = '#8e84a8';
      ctx.fillRect(Math.round(X(t)), top, 1, 5);
      ctx.fillText(fmtTime(t), X(t) + 3, H - 4);
    }
    const hm = this.info.heatmap || [];
    if (hm.length) {
      ctx.beginPath();
      ctx.moveTo(0, top);
      hm.forEach(([a, b, v]) => ctx.lineTo(X((a + b) / 2), top - v * top * 0.6));
      ctx.lineTo(W, top);
      ctx.fillStyle = 'rgba(255,110,40,.45)';
      ctx.fill();
    }
    const x0 = X(this.start), x1 = Math.max(X(this.start + this.len), x0 + 4);
    ctx.fillStyle = 'rgba(0,0,0,.45)';
    ctx.fillRect(0, 0, x0, top);
    ctx.fillRect(x1, 0, W - x1, top);
    ctx.strokeStyle = '#ffc400';
    ctx.lineWidth = 3;
    ctx.strokeRect(x0 + 1.5, 1.5, x1 - x0 - 3, H - 3);
    if (this.hoverT != null) {
      const hx = Math.round(X(this.hoverT)) + 0.5;
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, top); ctx.stroke();
    }
  },

  initEvents() {
    const cv = $('#sec-canvas');
    cv.addEventListener('pointerdown', e => {
      if (!this.info) return;
      const t = this.tAt(e.clientX);
      cv.setPointerCapture(e.pointerId);
      if (!(t >= this.start && t <= this.start + this.len)) { this.start = t - this.len / 2; this.clamp(); }
      this.drag = { offset: t - this.start };
      this.update();
      this.showBig(this.start + 5);
    });
    cv.addEventListener('pointermove', e => {
      if (!this.info) return;
      const t = this.tAt(e.clientX);
      this.hoverT = t;
      if (this.drag) { this.start = t - this.drag.offset; this.clamp(); this.update(); this.showBig(this.start + 5); }
      else { this.draw(); this.showBig(t); }
    });
    cv.addEventListener('pointerup', () => { this.drag = null; });
    cv.addEventListener('pointerleave', () => { this.hoverT = null; this.draw(); });
    $('#sec-start').addEventListener('change', e => {
      const t = parseTime(e.target.value);
      if (t != null) { this.start = t; this.clamp(); this.showBig(this.start + 5); }
      this.update();
    });
    $('#sec-length').addEventListener('change', e => { this.len = Math.min(+e.target.value, this.info.duration); this.clamp(); this.update(); });
    $('#sec-suggest').addEventListener('click', () => {
      if (this.info?.suggestion == null) return;
      this.start = this.info.suggestion; this.clamp(); this.update(); this.showBig(this.start + 5);
    });
    $('#sec-close').addEventListener('click', () => this.close());
    $('#sec-cancel').addEventListener('click', () => this.close());
    $('#sectioner').addEventListener('mousedown', e => { if (e.target.id === 'sectioner') this.close(); });
    $('#sec-use').addEventListener('click', e => withBusy(e.currentTarget, async () => {
      await api.post('/api/clips/section', { id: this.id, start: this.start, length: this.len });
      toast(`Getting ${fmtTime(this.start)} – ${fmtTime(this.start + this.len)}…`, 'ok');
      await poll();       // so the trimmer reopens on the new section
      this.close();
    }));
    window.addEventListener('resize', () => this.id && this.draw());
    document.addEventListener('keydown', e => { if (this.id && e.key === 'Escape') this.close(); });
  },
};

// Pick shot songs in one go: every Nth main song, N at random, or none.
function openShots() {
  const main = S.project.clips.filter(c => !c.bonus);
  const every = h('input', { type: 'number', class: 'num', min: 2, max: 60, value: 10 });
  const count = h('input', { type: 'number', class: 'num', min: 1, max: 60, value: 5 });
  const apply = (ids, btn) => withBusy(btn, async () => {
    const all = S.project.clips.map(c => c.id);
    await api.post('/api/clips/flags', { ids: all, shot: false });
    if (ids.length) await api.post('/api/clips/flags', { ids, shot: true });
    closeModal();
    await poll();
    toast(ids.length ? `${ids.length} shot song${ids.length > 1 ? 's' : ''} - each gets a "take a shot" card` : 'No shot songs', 'ok');
  });
  const everyBtn = h('button', { class: 'btn gold' }, 'Apply');
  everyBtn.addEventListener('click', () => {
    const n = Math.max(2, Math.round(+every.value || 10));
    apply(main.filter((_c, i) => (i + 1) % n === 0).map(c => c.id), everyBtn);
  });
  const randomBtn = h('button', { class: 'btn gold' }, 'Apply');
  randomBtn.addEventListener('click', () => {
    const ids = main.map(c => c.id);
    for (let i = ids.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [ids[i], ids[j]] = [ids[j], ids[i]]; }
    apply(ids.slice(0, Math.max(1, Math.round(+count.value || 5))), randomBtn);
  });
  const clearBtn = h('button', { class: 'btn danger' }, 'Clear all shot songs');
  clearBtn.addEventListener('click', () => apply([], clearBtn));
  openModal(
    h('div', { class: 'modal-head' }, h('h3', {}, '🥃 Shot songs'), h('button', { class: 'btn ghost icon', onclick: closeModal }, '✕')),
    h('div', { class: 'modal-body' },
      h('p', { class: 'muted small' }, 'A "take a shot" card plays before every shot song (style it in the Transitions tab). You can also click 🥃 on any song in the playlist. This replaces the current shot songs.'),
      h('div', { class: 'field-row' }, h('span', {}, 'Every'), every, h('span', {}, 'th song'), h('span', { class: 'spacer' }), everyBtn),
      h('div', { class: 'field-row' }, count, h('span', {}, 'songs at random'), h('span', { class: 'spacer' }), randomBtn),
      h('div', { class: 'field-row' }, clearBtn)),
    h('div', { class: 'modal-foot' }, h('span', { class: 'spacer' }), h('button', { class: 'btn', onclick: closeModal }, 'Close')),
  );
}

async function openFolder() {
  let path = '';
  if (desktop()) {
    path = await window.pywebview.api.pick_folder();
    if (!path) return;
  }
  const inp = h('input', { value: path, placeholder: 'C:\\Users\\you\\Videos\\Party', style: { flex: 1 } });
  const sub = h('input', { type: 'checkbox' });
  const go = h('button', { class: 'btn gold' }, 'Add videos');
  go.addEventListener('click', () => withBusy(go, async () => {
    const r = await api.post('/api/clips/folder', { path: inp.value.trim(), recursive: sub.checked });
    closeModal();
    toast(`Added ${r.added} video${r.added === 1 ? '' : 's'} - long ones (movies) will ask which part to use`, 'ok');
    poll();
  }));
  openModal(
    h('div', { class: 'modal-head' }, h('h3', {}, '📁 Add a folder of videos'), h('button', { class: 'btn ghost icon', onclick: closeModal }, '✕')),
    h('div', { class: 'modal-body', style: { width: '640px' } },
      h('p', { class: 'muted small' }, 'Every video file in the folder is added, sorted by name. Files stay where they are. Videos longer than the long-video limit (Settings) - movies, concerts - ask you to pick a section instead of being analysed whole.'),
      h('div', { class: 'field-row' }, inp,
        desktop() ? h('button', { class: 'btn', onclick: async () => { const p = await window.pywebview.api.pick_folder(); if (p) inp.value = p; } }, 'Browse…') : null),
      h('label', { class: 'toggle mt' }, sub, h('span'), 'Include subfolders')),
    h('div', { class: 'modal-foot' }, h('span', { class: 'spacer' }), h('button', { class: 'btn', onclick: closeModal }, 'Cancel'), go),
  );
  inp.focus();
}

async function loadExports() {
  const r = await api.get('/api/exports');
  $('#exports').replaceChildren(...r.exports.slice(0, 5).map(x => h('div', { class: 'output' },
    h('span', { class: 'n', title: x.path }, '🎞 ' + x.name),
    h('button', { class: 'btn small', title: 'Open folder', onclick: () => api.post('/api/open_export', { name: x.name }) }, '📂'))));
}

async function boot() {
  $('#cards-grid').innerHTML = cardEditor('intro') + cardEditor('outro');
  wire();
  S.meta = await api.get('/api/meta');
  $('#search-source').replaceChildren(...S.meta.sources.map(s => new Option(s.label, s.id)));
  $('#search-source').value = S.meta.sources.some(s => s.id === S.meta.settings.search_source) ? S.meta.settings.search_source : 'youtube';
  $('#search-source').classList.toggle('hidden', S.meta.sources.length < 2);
  buildEffectGrid();
  $('#theme-select').replaceChildren(...S.meta.themes.map(t => new Option(t.label, t.id)));
  $('#export-formats').replaceChildren(...S.meta.export_formats.map(f => h('label', {},
    h('input', { type: 'checkbox', value: f.id, checked: true }), f.label)));
  fillOptions();
  bindAll();
  showTab(S.tab);
  await poll();
  fillOptions();
  syncBindings();
}

boot().catch(e => { console.error(e); toast('Failed to start: ' + e.message, 'err'); });
