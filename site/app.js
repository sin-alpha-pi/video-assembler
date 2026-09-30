// Dialogue Assembler — browser version of dialogue_assemble.py

const $ = (id) => document.getElementById(id);
const VIDEO_EXT = ['.mp4', '.mov', '.m4v', '.mkv', '.avi', '.webm'];
const WHOOSH_NAMES = ['transition.mp3', 'whoosh.mp3'];
const SEARCH_BEFORE = 0.5;
const SEARCH_AFTER = 0.8;
const FRAME = 0.01; // 10 ms loudness frames

const state = { files: [], plan: null, ffmpeg: null, mounted: false, logBuf: [], busy: false };

// ------------------------------------------------------------------ UI utils
function log(msg) {
  const el = $('log');
  el.textContent += msg + '\n';
  if (el.textContent.length > 200000) el.textContent = el.textContent.slice(-150000);
  el.scrollTop = el.scrollHeight;
}
function status(msg, cls = '') { $('status').className = cls; $('status').textContent = msg; }
function progress(r) { $('bar').style.width = `${Math.max(0, Math.min(1, r)) * 100}%`; }
function setting(id) { const v = $(id).value; return isNaN(Number(v)) || v === '' ? v : Number(v); }
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function ext(name) { const i = name.lastIndexOf('.'); return i < 0 ? '' : name.slice(i).toLowerCase(); }
function stem(name) { const i = name.lastIndexOf('.'); return i < 0 ? name : name.slice(0, i); }
function setBusy(b) {
  state.busy = b;
  $('btnAnalyze').disabled = b || !state.files.length;
  $('btnRender').disabled = b || !(state.plan && state.plan.ok);
  $('btnCutlist').disabled = b || !state.plan;
}

// ------------------------------------------------------------------ files
async function filesFromDrop(items) {
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) {
      out.push(await new Promise((res, rej) => entry.file(res, rej)));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const e of batch) await walk(e);
      } while (batch.length);
    }
  };
  const entries = [...items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  for (const e of entries) await walk(e);
  return out;
}

function setFiles(list) {
  // keep top-level-ish files only, ignore hidden files and earlier outputs
  const seen = new Map();
  for (const f of list) {
    if (f.name.startsWith('.') || f.name.endsWith('_assembled.mp4')) continue;
    if (!seen.has(f.name)) seen.set(f.name, f);
  }
  state.files = [...seen.values()];
  state.plan = null;
  state.mounted = false;
  $('cutlist').innerHTML = '';
  $('result').classList.add('hidden');
  summarize();
  setBusy(false);
}

function summarize() {
  const f = state.files;
  const scripts = f.filter((x) => ext(x.name) === '.txt');
  const vids = f.filter((x) => VIDEO_EXT.includes(ext(x.name)));
  const mp3s = f.filter((x) => ext(x.name) === '.mp3');
  const whoosh = mp3s.find((x) => WHOOSH_NAMES.includes(x.name.toLowerCase()));
  const lines = [];
  lines.push(`<div>Script: ${scripts.length === 1 ? esc(scripts[0].name) : `<span class="bad">${scripts.length ? 'more than one .txt' : 'no .txt found'}</span>`}</div>`);
  lines.push(`<div>Videos: ${vids.length ? vids.map((v) => esc(v.name)).join(', ') : '<span class="bad">none</span>'}</div>`);
  lines.push(`<div>Audio: ${mp3s.length ? mp3s.map((v) => esc(v.name)).join(', ') : 'none'}</div>`);
  if (!whoosh) lines.push('<div class="warn">No transition.mp3 found. Slides will be silent.</div>');
  $('fileSummary').innerHTML = lines.join('');
  status(f.length ? 'Ready. Click Analyze.' : 'Add files to start.');
}

// ------------------------------------------------------------------ script parsing
function parseScript(text, mp3Offset) {
  const items = [];
  let pendingLeft = false;
  for (const raw of text.split(/\r?\n/)) {
    let line = raw.trim();
    for (;;) {
      const m = line.match(/^\[([^\]]+)\]\s*(.*)$/);
      if (!m) break;
      let tag = m[1].trim();
      line = m[2].trim();
      if (tag.toLowerCase() === 'left') { pendingLeft = true; continue; }
      let offset = mp3Offset;
      const off = tag.match(/@\s*([\d.]+)\s*$/);
      if (off) { offset = Number(off[1]); tag = tag.slice(0, off.index).trim(); }
      const parts = tag.split('+').map((p) => p.trim());
      items.push({ type: 'clip', file: parts[0], mp3: parts[1] || null, offset, left: pendingLeft });
      pendingLeft = false;
    }
    const m = line.match(/^([A-Za-z][\w .'-]{0,30}):\s*(.+)$/);
    if (m) {
      items.push({ type: 'line', speaker: m[1].trim(), text: m[2].trim(), left: pendingLeft });
      pendingLeft = false;
    }
  }
  return items;
}

// ------------------------------------------------------------------ matching
function norm(w) { return w.toLowerCase().replace(/’/g, "'").replace(/[^a-z0-9']/g, ''); }
function normWords(t) { return t.split(/[\s\-—–]+/).map(norm).filter(Boolean); }

const simCache = new Map();
function wordSim(a, b) {
  if (a === b) return 1;
  const k = a < b ? a + '|' + b : b + '|' + a;
  let v = simCache.get(k);
  if (v !== undefined) return v;
  const n = a.length, m = b.length;
  let prev = Array.from({ length: m + 1 }, (_, j) => j);
  for (let i = 1; i <= n; i++) {
    const cur = [i];
    for (let j = 1; j <= m; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  v = 1 - prev[m] / Math.max(n, m);
  simCache.set(k, v);
  return v;
}

// similarity of two word sequences, 0..1 (fuzzy word-level edit distance)
function seqScore(A, B, bFrom, bTo) {
  const n = A.length, m = bTo - bFrom;
  if (!n || !m) return 0;
  let prev = new Float64Array(m + 1);
  for (let j = 0; j <= m; j++) prev[j] = j;
  for (let i = 1; i <= n; i++) {
    const cur = new Float64Array(m + 1);
    cur[0] = i;
    for (let j = 1; j <= m; j++) {
      const s = wordSim(A[i - 1], B[bFrom + j - 1]);
      const sub = s >= 0.6 ? 1 - s : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + sub);
    }
    prev = cur;
  }
  return 1 - prev[m] / Math.max(n, m);
}

function findLine(text, clips, used) {
  const target = normWords(text);
  const n = target.length;
  const rough = [];
  clips.forEach((c, ci) => {
    const last = Math.max(1, c.toks.length - n + 1);
    for (let i = 0; i < last; i++) rough.push([seqScore(target, c.toks, i, Math.min(i + n, c.toks.length)), ci, i]);
  });
  rough.sort((a, b) => b[0] - a[0]);
  const best = [];
  const seen = new Set();
  for (const [, ci, i0] of rough.slice(0, 15)) {
    const toks = clips[ci].toks;
    for (let i = Math.max(0, i0 - 3); i < i0 + 4 && i < toks.length; i++) {
      for (let size = Math.max(1, Math.floor(n * 0.6)); size < n + 5; size++) {
        const j = Math.min(i + size, toks.length);
        const key = `${ci}:${i}:${j}`;
        if (seen.has(key)) continue;
        seen.add(key);
        if (used.some(([uc, ui, uj]) => uc === ci && i < uj && j > ui)) continue;
        best.push([seqScore(target, toks, i, j), ci, i, j]);
      }
    }
  }
  if (!best.length) return [null, null];
  best.sort((a, b) => b[0] - a[0] || (a[3] - a[2]) - (b[3] - b[2]));
  const top = best[0];
  const second = best.slice(1).find((b) => b[1] !== top[1] || b[3] <= top[2] || b[2] >= top[3]) || null;
  return [top, second];
}

// ------------------------------------------------------------------ audio levels
function levelsFromPCM(pcm, rate = 16000) {
  const size = Math.round(rate * FRAME);
  const out = new Float32Array(Math.ceil(pcm.length / size));
  for (let f = 0; f < out.length; f++) {
    let sum = 0;
    const s = f * size, e = Math.min(pcm.length, s + size);
    for (let i = s; i < e; i++) sum += pcm[i] * pcm[i];
    const rms = Math.sqrt(sum / Math.max(1, e - s));
    out[f] = rms > 0 ? 20 * Math.log10(rms) : -120;
  }
  return out;
}

function refineEdges(levels, wStart, wEnd, lo, hi, pad, thresh) {
  const a = Math.max(lo, wStart - SEARCH_BEFORE);
  const b = Math.min(hi, wEnd + SEARCH_AFTER);
  const fa = Math.floor(a / FRAME), fb = Math.min(levels.length, Math.ceil(b / FRAME));
  let first = -1, last = -1;
  for (let f = fa; f < fb; f++) if (levels[f] > thresh) { first = f; break; }
  for (let f = fb - 1; f >= fa; f--) if (levels[f] > thresh) { last = f; break; }
  let spStart = first < 0 ? a : first * FRAME;
  let spEnd = last < 0 ? b : (last + 1) * FRAME;
  spStart = Math.min(spStart, wStart + 0.3);
  spEnd = Math.max(spEnd, wEnd - 0.3);
  return [Math.max(lo, spStart - pad), Math.min(hi, spEnd + pad)];
}

// ------------------------------------------------------------------ transcript cache (IndexedDB)
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open('dialogue-assembler', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('transcripts');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function cacheGet(key) {
  try {
    const db = await idb();
    return await new Promise((res) => {
      const q = db.transaction('transcripts').objectStore('transcripts').get(key);
      q.onsuccess = () => res(q.result || null);
      q.onerror = () => res(null);
    });
  } catch { return null; }
}
async function cachePut(key, val) {
  try {
    const db = await idb();
    db.transaction('transcripts', 'readwrite').objectStore('transcripts').put(val, key);
  } catch { /* cache is optional */ }
}

// ------------------------------------------------------------------ whisper worker
let worker = null, jobId = 0;
const pending = new Map();
function getWorker() {
  if (worker) return worker;
  worker = new Worker(new URL('./whisper-worker.js', import.meta.url), { type: 'module' });
  const files = {};
  worker.onmessage = (e) => {
    const d = e.data;
    if (d.type === 'progress') {
      const p = d.p;
      if (p.status === 'progress' && p.file) {
        files[p.file] = p.progress || 0;
        const vals = Object.values(files);
        status(`Downloading Whisper model... ${Math.round(vals.reduce((a, b) => a + b, 0) / vals.length)}% (first time only)`);
      }
    } else if (d.type === 'ready') {
      log(`Whisper ready (${d.device === 'webgpu' ? 'graphics card' : 'CPU'})`);
    } else if (d.type === 'log') {
      log(d.msg);
    } else if (pending.has(d.id)) {
      const { res, rej } = pending.get(d.id);
      pending.delete(d.id);
      d.type === 'result' ? res(d.words) : rej(new Error(d.message));
    }
  };
  worker.onerror = (e) => log('Whisper worker error: ' + (e.message || e));
  return worker;
}
function transcribe(model, audio) {
  const id = ++jobId;
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    getWorker().postMessage({ id, model, audio }, [audio.buffer]);
  });
}

// ------------------------------------------------------------------ ffmpeg
async function getFFmpeg() {
  if (state.ffmpeg) return state.ffmpeg;
  status('Loading FFmpeg...');
  let FFmpeg;
  try {
    ({ FFmpeg } = await import('./vendor/ffmpeg/index.js'));
  } catch {
    throw new Error('FFmpeg files are missing. In the GitHub repo, set Settings > Pages > Source to "GitHub Actions" and re-run the deploy.');
  }
  const ff = new FFmpeg();
  ff.on('log', ({ message }) => { state.logBuf.push(message); });
  const base = new URL('./vendor/', location.href).href;
  const mt = window.crossOriginIsolated;
  try {
    if (mt) {
      await ff.load({
        coreURL: base + 'core-mt/ffmpeg-core.js',
        wasmURL: base + 'core-mt/ffmpeg-core.wasm',
        workerURL: base + 'core-mt/ffmpeg-core.worker.js',
      });
    } else {
      await ff.load({ coreURL: base + 'core/ffmpeg-core.js', wasmURL: base + 'core/ffmpeg-core.wasm' });
    }
  } catch (err) {
    if (!mt) throw err;
    log('Multi-threaded FFmpeg failed, using single-threaded: ' + err);
    await ff.load({ coreURL: base + 'core/ffmpeg-core.js', wasmURL: base + 'core/ffmpeg-core.wasm' });
  }
  log(`FFmpeg loaded (${mt ? 'multi-threaded' : 'single-threaded'})`);
  state.ffmpeg = ff;
  return ff;
}

async function mountFiles(ff) {
  if (state.mounted) return;
  try { await ff.unmount('/in'); } catch {}
  try { await ff.createDir('/in'); } catch {}
  await ff.mount('WORKERFS', { files: state.files }, '/in');
  state.mounted = true;
}

async function ffrun(args, onLine) {
  const ff = state.ffmpeg;
  state.logBuf = [];
  const handler = onLine ? ({ message }) => onLine(message) : null;
  if (handler) ff.on('log', handler);
  const code = await ff.exec(args);
  if (handler) ff.off('log', handler);
  return { code, log: state.logBuf.join('\n') };
}

async function probe(file) {
  const { log: out } = await ffrun(['-hide_banner', '-i', '/in/' + file.name]);
  const d = out.match(/Duration: (\d+):(\d+):([\d.]+)/);
  const v = out.match(/Stream #[^\n]*Video:[^\n]*?(\d{2,5})x(\d{2,5})/);
  const fps = out.match(/Video:[^\n]*?([\d.]+) fps/);
  const rot = out.match(/rotation of (-?[\d.]+)/);
  let w = v ? +v[1] : null, h = v ? +v[2] : null;
  if (rot && Math.abs(Math.round(+rot[1])) % 180 === 90) [w, h] = [h, w];
  return {
    duration: d ? +d[1] * 3600 + +d[2] * 60 + +d[3] : 0,
    width: w, height: h, fps: fps ? +fps[1] : null,
    hasAudio: /Stream #[^\n]*Audio:/.test(out),
  };
}

async function decodePCM(file) {
  await ffrun(['-hide_banner', '-i', '/in/' + file.name, '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', '/pcm.raw']);
  const data = await state.ffmpeg.readFile('/pcm.raw');
  await state.ffmpeg.deleteFile('/pcm.raw');
  return new Float32Array(data.buffer, data.byteOffset, data.byteLength / 4);
}

// ------------------------------------------------------------------ analyze
async function analyze() {
  setBusy(true);
  progress(0);
  $('cutlist').innerHTML = '';
  $('result').classList.add('hidden');
  try {
    const files = state.files;
    const byName = new Map(files.map((f) => [f.name.toLowerCase(), f]));
    const scripts = files.filter((f) => ext(f.name) === '.txt');
    if (scripts.length !== 1) throw new Error('Add exactly one .txt script.');
    const S = {
      model: setting('set-model'), trans: setting('set-trans'), whoosh: setting('set-whoosh'),
      mp3off: setting('set-mp3off'), mp3vol: setting('set-mp3vol'), pad: setting('set-pad'),
      silence: setting('set-silence'), minMatch: setting('set-minmatch') / 100,
      crf: setting('set-crf'), preset: setting('set-preset'),
    };
    const items = parseScript(await scripts[0].text(), S.mp3off);
    if (!items.length) throw new Error('No dialogue lines or [clip] tags found in the script.');

    const tagged = new Set(items.filter((i) => i.type === 'clip').map((i) => i.file.toLowerCase()));
    const videos = files.filter((f) => VIDEO_EXT.includes(ext(f.name)) && !tagged.has(f.name.toLowerCase()))
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    const speakers = [...new Set(items.filter((i) => i.type === 'line').map((i) => i.speaker))];
    const bySpeaker = Object.fromEntries(speakers.map((s) => [s, []]));
    for (const f of videos) {
      const hits = speakers.filter((s) => new RegExp(`(?<![a-z])${s.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![a-z])`).test(stem(f.name).toLowerCase()));
      if (hits.length === 1) bySpeaker[hits[0]].push(f);
      else log(`! ${f.name}: ${hits.length ? 'several' : 'no'} speaker names in filename, ignored`);
    }
    for (const s of speakers) if (!bySpeaker[s].length) throw new Error(`No clips found for speaker "${s}" (the name must be in the filename).`);

    const ff = await getFFmpeg();
    await mountFiles(ff);

    // transcribe
    const allClips = speakers.flatMap((s) => bySpeaker[s]);
    const clipData = {};
    let done = 0;
    for (const s of speakers) {
      clipData[s] = [];
      for (const f of bySpeaker[s]) {
        const key = `${S.model}|${f.name}|${f.size}|${f.lastModified}`;
        let entry = await cacheGet(key);
        const info = await probe(f);
        if (!entry) {
          status(`Transcribing ${f.name} (${done + 1}/${allClips.length})...`);
          const pcm = await decodePCM(f);
          const levels = levelsFromPCM(pcm);
          const words = await transcribe(S.model, pcm);
          entry = { words, levels };
          await cachePut(key, entry);
          log(`Transcribed ${f.name}: ${words.length} words`);
        } else {
          log(`Using saved transcript for ${f.name}`);
        }
        const words = entry.words.filter((w) => norm(w.w));
        clipData[s].push({ file: f, info, words, toks: words.map((w) => norm(w.w)), levels: entry.levels });
        progress(++done / allClips.length);
      }
    }

    // build the cut list
    status('Matching script lines...');
    const segments = [], rows = [], used = {};
    let problems = 0;
    let n = 0;
    for (const it of items) {
      n++;
      if (it.type === 'clip') {
        const f = byName.get(it.file.toLowerCase());
        if (!f) throw new Error(`Clip named in the script not found: ${it.file}`);
        const info = await probe(f);
        const seg = { file: f, start: 0, end: info.duration, mute: true, hasAudio: info.hasAudio, left: it.left, mp3: null, offset: it.offset };
        const row = { n, left: it.left, label: `[${f.name}] muted`, src: `0.00–${info.duration.toFixed(2)}`, cls: 'ok', notes: [] };
        if (it.mp3) {
          const m = byName.get(it.mp3.toLowerCase());
          if (!m) throw new Error(`mp3 named in the script not found: ${it.mp3}`);
          seg.mp3 = m;
          row.label += ` + ${m.name} at ${it.offset}s`;
          const over = it.offset + (await probe(m)).duration - info.duration;
          if (over > 0.01) row.notes.push(['warn', `mp3 runs ${over.toFixed(2)}s past the clip end and will be cut off`]);
        }
        segments.push(seg);
        rows.push(row);
        continue;
      }
      const clips = clipData[it.speaker];
      used[it.speaker] = used[it.speaker] || [];
      const [top, second] = findLine(it.text, clips, used[it.speaker]);
      const row = { n, left: it.left, label: `${it.speaker}: ${it.text}`, src: '', cls: 'ok', notes: [] };
      if (!top || top[0] < S.minMatch) {
        problems++;
        row.cls = 'bad';
        row.src = `not found (best ${top ? Math.round(top[0] * 100) : 0}%)`;
        if (top) row.notes.push(['bad', `closest in ${clips[top[1]].file.name}: "${clips[top[1]].words.slice(top[2], top[3]).map((w) => w.w).join(' ')}"`]);
        rows.push(row);
        continue;
      }
      const [score, ci, i, j] = top;
      const c = clips[ci];
      used[it.speaker].push([ci, i, j]);
      const w = c.words;
      const lo = i > 0 ? w[i - 1].e + 0.02 : 0;
      const hi = j < w.length ? w[j].s - 0.02 : c.info.duration;
      const [start, end] = refineEdges(c.levels, w[i].s, w[j - 1].e, lo, hi, S.pad, S.silence);
      segments.push({ file: c.file, start, end, mute: false, hasAudio: c.info.hasAudio, left: it.left, mp3: null });
      row.src = `${c.file.name} ${start.toFixed(2)}–${end.toFixed(2)} · ${Math.round(score * 100)}%`;
      if (score < 0.85) { row.cls = 'warn'; row.notes.push(['warn', `weak match, heard: "${w.slice(i, j).map((x) => x.w).join(' ')}"`]); }
      if (second && second[0] >= 0.85) row.notes.push(['warn', 'line also appears elsewhere, used the best match']);
      rows.push(row);
    }

    const whoosh = files.find((f) => WHOOSH_NAMES.includes(f.name.toLowerCase())) || null;
    state.plan = { segments, rows, whoosh, S, ok: problems === 0 && segments.length > 0 };
    showCutList(rows);
    progress(1);
    if (problems) status(`${problems} line(s) not found. Check the cut list below.`, 'bad');
    else status(`All ${rows.length} items found. Check the cut list, then click Render video.`, 'ok');
  } catch (err) {
    console.error(err);
    status('Error: ' + err.message, 'bad');
    log('ERROR: ' + (err.stack || err));
  } finally {
    setBusy(false);
  }
}

function showCutList(rows) {
  const html = ['<table><tr><th>#</th><th>Item</th><th>Source</th></tr>'];
  for (const r of rows) {
    const notes = r.notes.map(([c, t]) => `<div class="note ${c}">${esc(t)}</div>`).join('');
    html.push(`<tr><td class="num">${r.n}</td><td>${r.left ? '<span class="slide">⟵ slide</span> ' : ''}${esc(r.label)}${notes}</td>` +
      `<td class="src ${r.cls === 'bad' ? 'bad' : ''}">${esc(r.src)}</td></tr>`);
  }
  html.push('</table>');
  $('cutlist').innerHTML = html.join('');
}

function cutListText() {
  return state.plan.rows.map((r) =>
    `${String(r.n).padStart(3)} ${r.left ? '>' : ' '} ${r.label}\n      ${r.src}` +
    r.notes.map(([, t]) => `\n      ${t}`).join('')).join('\n') + '\n';
}

// ------------------------------------------------------------------ render
async function render() {
  const plan = state.plan;
  if (!plan || !plan.ok) return;
  setBusy(true);
  progress(0);
  $('result').classList.add('hidden');
  try {
    const ff = await getFFmpeg();
    await mountFiles(ff);
    const { segments, whoosh, S } = plan;
    const first = await probe(segments[0].file);
    let W = first.width || 1080, H = first.height || 1920;
    W -= W % 2; H -= H % 2;
    const FPS = first.fps || 30;
    const AF = 'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo';
    const args = ['-hide_banner'];
    const parts = [];
    let idx = 0;
    segments.forEach((s, k) => {
      s.dur = Math.max(2 / FPS, Math.round((s.end - s.start) * FPS) / FPS);
      const d = s.dur.toFixed(4);
      args.push('-ss', s.start.toFixed(3), '-t', (s.dur + 0.2).toFixed(3), '-i', '/in/' + s.file.name);
      const vi = idx++;
      parts.push(`[${vi}:v]scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2,` +
        `setsar=1,fps=${FPS},format=yuv420p,tpad=stop_mode=clone:stop_duration=0.3,trim=duration=${d},` +
        `setpts=PTS-STARTPTS,settb=AVTB[v${k}]`);
      if (s.mp3) {
        args.push('-i', '/in/' + s.mp3.name);
        const mi = idx++;
        parts.push(`[${mi}:a]${AF},volume=${S.mp3vol},adelay=${Math.round(s.offset * 1000)}:all=1,apad,atrim=0:${d},asetpts=PTS-STARTPTS[a${k}]`);
      } else if (s.mute || !s.hasAudio) {
        parts.push(`aevalsrc=0|0:s=48000:d=${d},${AF}[a${k}]`);
      } else {
        parts.push(`[${vi}:a]${AF},apad,atrim=0:${d},asetpts=PTS-STARTPTS[a${k}]`);
      }
    });

    let v = '[v0]', a = '[a0]';
    let total = segments[0].dur;
    const centers = [];
    for (let k = 1; k < segments.length; k++) {
      const s = segments[k];
      const vo = `[vx${k}]`, ao = `[ax${k}]`;
      if (s.left) {
        const dd = Math.min(S.trans, total, s.dur) - 0.01;
        parts.push(`${v}[v${k}]xfade=transition=slideleft:duration=${dd.toFixed(3)}:offset=${(total - dd).toFixed(3)}${vo}`);
        parts.push(`${a}[a${k}]acrossfade=d=${dd.toFixed(3)}${ao}`);
        centers.push(total - dd / 2);
        total += s.dur - dd;
      } else {
        parts.push(`${v}${a}[v${k}][a${k}]concat=n=2:v=1:a=1${vo}${ao}`);
        total += s.dur;
      }
      v = vo; a = ao;
    }
    if (whoosh && centers.length) {
      const wlen = (await probe(whoosh)).duration;
      args.push('-i', '/in/' + whoosh.name);
      const wi = idx++;
      parts.push(`[${wi}:a]${AF},volume=${S.whoosh},asplit=${centers.length}` + centers.map((_, k) => `[w${k}]`).join(''));
      centers.forEach((c, k) => parts.push(`[w${k}]adelay=${Math.round(Math.max(0, c - wlen / 2) * 1000)}:all=1[wd${k}]`));
      parts.push(`${a}${centers.map((_, k) => `[wd${k}]`).join('')}amix=inputs=${centers.length + 1}:duration=first:normalize=0[aout]`);
      a = '[aout]';
    }
    args.push('-filter_complex', parts.join(';'), '-map', v, '-map', a,
      '-c:v', 'libx264', '-preset', S.preset, '-crf', String(S.crf), '-pix_fmt', 'yuv420p',
      '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-y', '/out.mp4');
    log('ffmpeg ' + args.join(' '));

    const t0 = performance.now();
    status(`Rendering ${segments.length} segments, ${total.toFixed(1)}s of video at ${W}x${H}... this can take a while.`);
    const { code, log: out } = await ffrun(args, (line) => {
      const m = line.match(/time=(\d+):(\d+):([\d.]+)/);
      if (m) {
        const t = +m[1] * 3600 + +m[2] * 60 + +m[3];
        const r = t / total;
        progress(r);
        const el = (performance.now() - t0) / 1000;
        const left = r > 0.02 ? Math.round(el / r - el) : null;
        status(`Rendering... ${Math.round(r * 100)}%${left != null ? ` · about ${left > 90 ? Math.round(left / 60) + ' min' : left + ' s'} left` : ''}`);
      }
    });
    if (code !== 0) { log(out.split('\n').slice(-40).join('\n')); throw new Error('FFmpeg failed while rendering (see technical log).'); }
    const data = await ff.readFile('/out.mp4');
    await ff.deleteFile('/out.mp4');
    const blob = new Blob([data.buffer], { type: 'video/mp4' });
    const url = URL.createObjectURL(blob);
    $('preview').src = url;
    const scriptName = stem(state.files.find((f) => ext(f.name) === '.txt').name);
    $('download').href = url;
    $('download').download = `${scriptName}_assembled.mp4`;
    $('result').classList.remove('hidden');
    progress(1);
    status(`Done in ${Math.round((performance.now() - t0) / 1000)}s: ${total.toFixed(1)}s video, ${centers.length} transitions.`, 'ok');
  } catch (err) {
    console.error(err);
    status('Error: ' + err.message, 'bad');
    log('ERROR: ' + (err.stack || err));
  } finally {
    setBusy(false);
  }
}

// ------------------------------------------------------------------ wiring
const drop = $('drop');
drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', async (e) => {
  e.preventDefault();
  drop.classList.remove('over');
  if (state.busy) return;
  setFiles(await filesFromDrop(e.dataTransfer.items));
});
$('pickDir').addEventListener('change', (e) => setFiles([...e.target.files]));
$('pickFiles').addEventListener('change', (e) => setFiles([...e.target.files]));
$('btnAnalyze').addEventListener('click', analyze);
$('btnRender').addEventListener('click', render);
$('btnCutlist').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([cutListText()], { type: 'text/plain' }));
  a.download = 'cut_list.txt';
  a.click();
});
for (const id of ['set-trans', 'set-whoosh', 'set-mp3vol', 'set-crf', 'set-preset']) {
  // render-only settings apply without re-analyzing
  $(id).addEventListener('change', () => { if (state.plan) state.plan.S[id.replace('set-', '')] = setting(id); });
}
log(`Cross-origin isolated: ${window.crossOriginIsolated}`);
