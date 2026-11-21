"use strict";

// ---------------------------------------------------------------------------
// STPG Pattern Viewer front end (no external dependencies).
// Times from the server are integer picoseconds.
// ---------------------------------------------------------------------------

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const LANE_H = 26;
const RULER_H = 46;
const NAME_W = 200;
const ROW_H = 20;
const ROW_BLOCK = 200;
const CHAR_W = 15;

const $ = (id) => document.getElementById(id);
const canvas = $("wave");
const ctx = canvas.getContext("2d");

const S = {
  traces: [],
  activeId: null,
  meta: null,
  views: new Map(),          // trace id -> {t0, scale, y}
  view: { t0: 0, scale: 1000, y: 0 },
  sel: -1,                   // selected vector (range anchor)
  selEnd: -1,                // other end of a shift-selected range
  edit: null,                // edit info of the active STIL (or its edited copy)
  editForm: null,
  hover: null,               // {x, y, t}
  wave: null,                // last /api/wave response, prepared
  waveKey: "",
  fetching: false,
  refetch: false,
  rows: new Map(),           // block start -> rows
  rowsPending: new Set(),
  colors: {},
  dpr: 1,
  width: 0,
  height: 0,
  selectedSignal: -1,
  marks: null,               // a run's fails placed on the active trace (/api/marks), sorted by time
  markAt: -1,                // the fail shown in the inspector
  markVectors: null,         // vectors with a fail (table rows)
};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
// A button whose click asks the server for something shows that from the click until the answer: requests
// started while its click is handled mark it busy (class "busy", aria-busy) and the browser repaints it at once.
let clickedButton = null;
document.addEventListener("click", (e) => {
  const button = e.target instanceof Element ? e.target.closest("button") : null;
  if (!button) return;
  clickedButton = button;
  setTimeout(() => { if (clickedButton === button) clickedButton = null; }, 0);
}, true);

function markBusy(button, on) {
  button._busy = Math.max(0, (button._busy || 0) + (on ? 1 : -1));
  button.classList.toggle("busy", button._busy > 0);
  if (button._busy > 0) button.setAttribute("aria-busy", "true"); else button.removeAttribute("aria-busy");
}

// A button whose action starts after an await (a picker or a confirm first) is marked for that promise.
async function busyWhile(button, promise) {
  if (button) markBusy(button, true);
  try { return await promise; } finally { if (button) markBusy(button, false); }
}

// ---------------------------------------------------------------------------
// activity: what the server is working on, in the top bar, for every request that takes a while (also
// the ones no button started: a double-clicked file, a dropped file, a tab switch) and every running job.
// ---------------------------------------------------------------------------
const ACTIVITY = [  // [path pattern, label (null: never shown), delay ms]
  [/^\/api\/open\b/, "Opening the pattern", 0],
  [/^\/api\/upload\b/, "Uploading", 0],
  [/^\/api\/edits\b/, (o) => (o.method === "POST" ? "Applying the edit" : "Reading the edit state"), 0],
  [/^\/api\/wave\b/, "Loading the waveform", 700],
  [/^\/api\/rows\b/, "Loading table rows", 700],
  [/^\/api\/vector\b/, "Loading the vector", 700],
  [/^\/api\/find\b/, "Searching", 400],
  [/^\/api\/export\//, "Exporting", 0],
  [/^\/api\/external\b/, "Writing the VCD for the external viewer", 0],
  [/^\/api\/browse\b/, "Listing the folder", 700],
  [/^\/api\/runner\/project\/open\b/, "Opening the project", 0],
  [/^\/api\/runner\/project\/init\b/, "Creating the project", 0],
  [/^\/api\/runner\/project\/edit\b/, "Saving the project settings", 0],
  [/^\/api\/runner\/bundle\/open\b/, "Opening and checking the bundle", 0],
  [/^\/api\/runner\/run\/prepare\b/, "Preparing the run", 0],
  [/^\/api\/runner\/control\/prepare\b/, "Preparing the GPIO operation", 0],
  [/^\/api\/runner\/ports\b/, "Scanning serial ports", 0],
  [/^\/api\/runner\/stil_info\b/, "Reading the STIL for Loop statements", 700],
  [/^\/api\/runner\/external\/inspect\b/, "Checking the accessory config", 400],
  [/^\/api\/runner\/results\b/, "Reading the results", 400],
  [/^\/api\/runner\/(job|state|bindings|capabilities)\b/, null, 0],
  [/^\/api\/(state|viewers)\b/, null, 0],
];
const Activity = { items: new Map(), seq: 0, timer: null };

function activityBegin(path, options) {
  const rule = ACTIVITY.find(([re]) => re.test(path));
  let label = rule ? rule[1] : "Working";
  if (typeof label === "function") label = label(options || {});
  if (label === null) return () => {};
  const id = ++Activity.seq;
  Activity.items.set(id, { label, since: performance.now(), delay: rule ? rule[2] : 400 });
  renderActivity();
  return () => { Activity.items.delete(id); renderActivity(); };
}

function renderActivity() {
  const now = performance.now();
  const shown = [...Activity.items.values()].filter((a) => now - a.since >= a.delay);
  if (typeof runnerActivities === "function") shown.push(...runnerActivities());
  const box = $("activity");
  if (!shown.length) {
    box.hidden = true;
    document.body.classList.remove("working");
  } else {
    shown.sort((a, b) => a.since - b.since);
    const first = shown[0];
    const seconds = Math.floor((now - first.since) / 1000);
    $("activityText").textContent = `${first.label}… ${seconds} s` + (shown.length > 1 ? ` (+${shown.length - 1})` : "");
    box.title = shown.map((a) => `${a.label} · ${Math.floor((now - a.since) / 1000)} s`).join("\n");
    box.hidden = false;
    document.body.classList.add("working");
  }
  const pending = Activity.items.size > 0 || shown.length > 0;
  if (pending && !Activity.timer) Activity.timer = setInterval(renderActivity, 250);
  else if (!pending && Activity.timer) { clearInterval(Activity.timer); Activity.timer = null; }
}

async function api(path, options = {}) {
  const button = clickedButton;
  if (button) markBusy(button, true);
  const done = activityBegin(path, options);
  try {
    const headers = Object.assign({ "X-STPG-Token": TOKEN }, options.headers || {});
    const response = await fetch(path, Object.assign({}, options, { headers }));
    let body = null;
    try { body = await response.json(); } catch (e) { body = null; }
    if (!response.ok) throw new Error((body && body.error) || response.statusText);
    return body;
  } finally { done(); if (button) markBusy(button, false); }
}

function q(params) {
  return Object.entries(params).filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

function fmtTime(ps, digits = 3) {
  const a = Math.abs(ps);
  if (a === 0) return "0";
  if (a < 1e3) return `${+ps.toFixed(0)} ps`;
  if (a < 1e6) return `${+(ps / 1e3).toFixed(digits)} ns`;
  if (a < 1e9) return `${+(ps / 1e6).toFixed(digits)} µs`;
  if (a < 1e12) return `${+(ps / 1e9).toFixed(digits)} ms`;
  return `${+(ps / 1e12).toFixed(digits)} s`;
}

function parseTime(text) {
  const m = /^\s*([0-9]*\.?[0-9]+)\s*(ps|ns|us|µs|ms|s)\s*$/i.exec(text);
  if (!m) return null;
  const unit = { ps: 1, ns: 1e3, us: 1e6, "µs": 1e6, ms: 1e9, s: 1e12 }[m[2].toLowerCase()];
  return Math.round(parseFloat(m[1]) * unit);
}

function charClass(c) {
  if (c === "H" || c === "L" || c === "h" || c === "l") return "compare";
  if (c === "X") return "masked";
  if (c === "Z") return "release";
  if ("01PNUDRFudpn".includes(c)) return "drive";
  return "none";
}
const isHigh = (c) => c === "H" || c === "h";

function dirClass(declared) {
  const d = (declared || "").toLowerCase();
  if (d === "in") return "in";
  if (d === "out") return "out";
  if (d === "inout") return "inout";
  return "";
}

function setStatus(text, isError = false) {
  const el = $("statusMsg");
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
}

function readColors() {
  const cs = getComputedStyle(document.documentElement);
  const names = ["bg", "panel", "panel-2", "line", "line-strong", "text", "muted", "accent", "drive", "drive-fill",
    "cmp-h", "cmp-l", "masked", "release", "wait", "wait-fill", "dir-in", "dir-out", "dir-inout", "sel", "cursor",
    "accent-soft", "fail"];
  for (const n of names) S.colors[n] = cs.getPropertyValue(`--${n}`).trim();
  S.colors.mono = cs.getPropertyValue("--mono").trim();
  S.colors.sans = cs.getPropertyValue("--sans").trim();
}

// ---------------------------------------------------------------------------
// colours chosen for rates (by WaveformTable name) and signals (by name), kept in this browser
// ---------------------------------------------------------------------------
const PALETTE = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#65a30d", "#dc2626", "#4f46e5",
  "#ca8a04", "#0d9488", "#c026d3"];
let Colors = { rates: {}, signals: {} };
try {
  const saved = JSON.parse(localStorage.getItem("stpg-view-colors") || "null");
  if (saved && typeof saved === "object") Colors = { rates: saved.rates || {}, signals: saved.signals || {} };
} catch (e) { /* storage unavailable */ }

function rateColor(name) { return Colors.rates[name] || null; }
function signalColor(name) { return Colors.signals[name] || null; }

function tint(hex, alpha) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  if (!m) return hex;
  return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${alpha})`;
}

function saveColors() {
  try { localStorage.setItem("stpg-view-colors", JSON.stringify(Colors)); } catch (e) { /* storage unavailable */ }
}

function colorsChanged() {
  saveColors();
  renderCharHead();
  renderTable();
  draw();
}

function colorRow(kind, name, caption) {
  const store = Colors[kind];
  const picker = document.createElement("input");
  picker.type = "color";
  picker.value = store[name] || "#888888";
  picker.dataset.name = name;
  const label = document.createElement("span");
  label.className = "n";
  label.textContent = name;
  const note = document.createElement("span");
  note.className = "muted";
  note.textContent = caption || "";
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "btn small";
  clear.textContent = "×";
  clear.title = "No colour";
  const li = document.createElement("li");
  li.classList.toggle("unset", !store[name]);
  li.dataset.name = name;
  li.append(picker, label, note, clear);
  picker.addEventListener("input", () => { store[name] = picker.value; li.classList.remove("unset"); colorsChanged(); });
  clear.addEventListener("click", () => { delete store[name]; li.classList.add("unset"); colorsChanged(); });
  return li;
}

function openColors(focus) {
  if (!S.meta) return;
  const rates = $("colorRates"), signals = $("colorSignals");
  rates.innerHTML = "";
  signals.innerHTML = "";
  const seen = new Set();
  for (const r of S.meta.rates) {
    if (seen.has(r.name)) continue;
    seen.add(r.name);
    rates.append(colorRow("rates", r.name, fmtTime(r.period_ps)));
  }
  for (const s of S.meta.signals) signals.append(colorRow("signals", s.name, [s.pin, s.declared].filter(Boolean).join(" ")));
  $("colorDialog").showModal();
  if (focus) {
    const row = [...document.querySelectorAll(`#colorDialog li`)].find((li) => li.dataset.name === focus.name && li.parentElement.id === (focus.kind === "rates" ? "colorRates" : "colorSignals"));
    if (row) { row.scrollIntoView({ block: "center" }); row.classList.add("focus"); row.querySelector("input").focus(); }
  }
}

function colorAll(kind) {
  const names = kind === "rates" ? [...new Set(S.meta.rates.map((r) => r.name))] : S.meta.signals.map((s) => s.name);
  names.forEach((name, i) => { Colors[kind][name] = PALETTE[i % PALETTE.length]; });
  colorsChanged();
  openColors();
}

function clearColors(kind) {
  const names = kind === "rates" ? S.meta.rates.map((r) => r.name) : S.meta.signals.map((s) => s.name);
  for (const name of names) delete Colors[kind][name];
  colorsChanged();
  openColors();
}

// ---------------------------------------------------------------------------
// state / loading
// ---------------------------------------------------------------------------
async function refreshState(preferId) {
  const state = await api("/api/state");
  S.traces = state.traces || [];
  renderTabs();
  if (!S.traces.length) {
    S.activeId = null;
    S.meta = null;
    S.marks = null; S.markAt = -1; S.markVectors = null;
    renderFails();
    $("emptyState").hidden = false;
    $("vcdBtn").disabled = true;
    $("runnerBtn").disabled = true;
    $("colorBtn").disabled = true;
    draw();
    return;
  }
  const ids = S.traces.map((t) => t.id);
  const next = preferId && ids.includes(preferId) ? preferId : (ids.includes(S.activeId) ? S.activeId : ids[0]);
  activate(next, true);
}

function activate(id, force = false) {
  if (!force && id === S.activeId) return;
  if (S.activeId) S.views.set(S.activeId, Object.assign({}, S.view));
  const highlighted = S.meta && S.selectedSignal >= 0 ? S.meta.signals[S.selectedSignal].name : null;
  S.activeId = id;
  S.meta = S.traces.find((t) => t.id === id) || null;
  S.wave = null;
  S.waveKey = "";
  S.rows.clear();
  S.rowsPending.clear();
  S.sel = -1;
  S.selEnd = -1;
  S.selDetail = null;  // the inspector's vector belongs to the trace it was read from
  S.hover = null;
  S.selectedSignal = S.meta && highlighted ? S.meta.signals.findIndex((s) => s.name === highlighted) : -1;  // e.g. on the edited copy
  $("emptyState").hidden = !!S.meta;
  $("vcdBtn").disabled = !S.meta;
  $("extBtn").disabled = !S.meta;
  $("runnerBtn").disabled = !S.meta || S.meta.kind !== "stil";
  $("colorBtn").disabled = !S.meta;
  renderTabs();
  renderTraceInfo();
  renderCharHead();
  setupTable();
  const saved = S.views.get(id);
  if (saved) S.view = Object.assign({}, saved); else fitAll();
  $("vectorInfo").innerHTML = '<h2>Vector</h2><p class="muted">Click a vector in the waveform or table. Shift+click selects a range.</p>';
  S.edit = null;
  S.editForm = null;
  $("eventsBox").hidden = true;
  S.marks = null; S.markAt = -1; S.markVectors = null;
  renderFails();
  if (S.meta && S.meta.fail_marks) loadMarks(id);
  loadEditInfo();
  requestWave();
  draw();
}

function renderTabs() {
  const host = $("traceTabs");
  host.innerHTML = "";
  for (const t of S.traces) {
    const b = document.createElement("button");
    b.className = "trace-tab" + (t.id === S.activeId ? " active" : "");
    b.type = "button";
    b.title = (t.sources || []).map((s) => `${s.kind}: ${s.path}`).join("\n");
    b.innerHTML = `<span class="kind"></span><span class="name"></span>`;
    b.querySelector(".kind").textContent = t.edit_of ? "stil ✎" : t.kind;
    b.querySelector(".name").textContent = t.title;
    b.addEventListener("click", () => activate(t.id));
    host.appendChild(b);
  }
}

function renderTraceInfo() {
  const box = $("traceInfo");
  if (!S.meta) { box.innerHTML = ""; return; }
  const m = S.meta;
  const rates = m.rates.slice(0, 12).map((r) => `${r.name} ${fmtTime(r.period_ps)}`).join(", ");
  box.innerHTML = `<h2>Pattern</h2>
    <dl class="kv">
      <dt>format</dt><dd></dd>
      <dt>vectors</dt><dd></dd>
      <dt>duration</dt><dd></dd>
      <dt>signals</dt><dd></dd>
      <dt>rates</dt><dd></dd>
    </dl>
    <ul class="sources"></ul>
    <ul class="notes"></ul>`;
  const dd = box.querySelectorAll("dd");
  dd[0].textContent = m.kind;
  dd[1].textContent = m.vector_count.toLocaleString();
  dd[2].textContent = fmtTime(m.total_ps);
  dd[3].textContent = String(m.signals.length);
  dd[4].textContent = rates + (m.rates.length > 12 ? ` … (+${m.rates.length - 12})` : "");
  const ul = box.querySelector(".sources");
  for (const s of m.sources) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="tag"></span><span class="p"></span>';
    li.querySelector(".tag").textContent = s.kind.replace("_", " ");
    li.querySelector(".p").textContent = s.path.split(/[\\/]/).pop();
    li.title = `${s.path}\nsha256 ${s.sha256}`;
    ul.appendChild(li);
  }
  const notes = box.querySelector(".notes");
  for (const n of m.notes) {
    const li = document.createElement("li");
    li.textContent = n;
    notes.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// waveform data
// ---------------------------------------------------------------------------
function plotWidth() { return Math.max(1, S.width - NAME_W); }

function visibleRange() {
  const t0 = S.view.t0;
  return [t0, t0 + plotWidth() * S.view.scale];
}

function requestWave() {
  if (!S.meta) return;
  const [a, b] = visibleRange();
  const w = S.wave;
  if (w && w.traceId === S.activeId && a >= w.t0 && b <= w.t1 && Math.abs(Math.log2(S.view.scale / w.scale)) < 0.35) return;
  if (w && w.traceId === S.activeId && w.mode === "events" && a >= w.coverStart && b <= w.coverEnd) return;
  if (S.fetching) { S.refetch = true; return; }
  S.fetching = true;
  const span = b - a;
  const t0 = Math.max(0, Math.floor(a - span * 0.5));
  const t1 = Math.min(S.meta.total_ps, Math.ceil(b + span * 0.5));
  const buckets = Math.min(4000, Math.ceil(plotWidth() * 2));
  const traceId = S.activeId;
  const scale = S.view.scale;
  api(`/api/wave?${q({ trace: traceId, t0, t1, buckets })}`).then((data) => {
    if (traceId === S.activeId) {
      S.wave = prepareWave(data, traceId, t0, t1, scale);
      draw();
    }
  }).catch((e) => setStatus(e.message, true)).finally(() => {
    S.fetching = false;
    if (S.refetch) { S.refetch = false; requestWave(); }
  });
}

function prepareWave(data, traceId, t0, t1, scale) {
  const n = S.meta.signals.length;
  const w = { traceId, t0, t1, scale, mode: data.mode };
  if (data.mode === "events") {
    w.vectors = data.vectors;
    const first = data.vectors[0];
    const last = data.vectors[data.vectors.length - 1];
    w.coverStart = first ? first[1] : t0;
    w.coverEnd = last ? last[1] + last[2] : t1;
    // per-signal level segments and compare marks
    const segs = Array.from({ length: n }, () => []);
    const marks = Array.from({ length: n }, () => []);
    const state = new Array(n);
    for (let s = 0; s < n; s++) {
      const bit = 2 ** s;
      const en = Math.floor(data.init_enable / bit) % 2 === 1;
      const val = Math.floor(data.init_value / bit) % 2 === 1;
      state[s] = en ? (val ? "1" : "0") : "Z";
    }
    const segStart = new Array(n).fill(w.coverStart);
    const events = data.events.slice().sort((a, b) => a[1] - b[1]);
    for (const [s, t, kind, value] of events) {
      if (kind === "C") { marks[s].push([t, value]); continue; }
      const next = kind === "D" ? value : "Z";
      if (next === state[s]) continue;
      if (t > segStart[s]) segs[s].push([segStart[s], t, state[s]]);
      state[s] = next;
      segStart[s] = t;
    }
    for (let s = 0; s < n; s++) segs[s].push([segStart[s], w.coverEnd, state[s]]);
    w.segs = segs;
    w.marks = marks;
  } else if (data.mode === "summary") {
    w.cells = data.cells;
    w.spanStart = data.span_start;
    w.bucketPs = data.bucket_ps;
    w.first = data.first;
    w.last = data.last;
    w.coverStart = data.span_start;
    w.coverEnd = data.span_start + data.bucket_ps * data.cells.length;
    w.waits = data.waits || [];
    w.rates = data.rates || [];
  }
  return w;
}

// ---------------------------------------------------------------------------
// drawing
// ---------------------------------------------------------------------------
function resizeCanvas() {
  const host = $("waveHost");
  S.dpr = window.devicePixelRatio || 1;
  S.width = host.clientWidth;
  S.height = host.clientHeight;
  canvas.width = Math.round(S.width * S.dpr);
  canvas.height = Math.round(S.height * S.dpr);
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
}

const xOf = (t) => NAME_W + (t - S.view.t0) / S.view.scale;
const tOf = (x) => S.view.t0 + (x - NAME_W) * S.view.scale;

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

function draw() {
  const C = S.colors;
  ctx.fillStyle = C.panel;
  ctx.fillRect(0, 0, S.width, S.height);
  if (!S.meta) return;
  const n = S.meta.signals.length;
  const maxY = Math.max(0, n * LANE_H - (S.height - RULER_H) + 8);
  S.view.y = Math.min(Math.max(0, S.view.y), maxY);
  const firstLane = Math.floor(S.view.y / LANE_H);
  const lastLane = Math.min(n - 1, Math.ceil((S.view.y + S.height - RULER_H) / LANE_H));

  // lane backgrounds
  for (let s = firstLane; s <= lastLane; s++) {
    const y = RULER_H + s * LANE_H - S.view.y;
    if (s % 2 === 1) { ctx.fillStyle = C["panel-2"]; ctx.fillRect(NAME_W, y, S.width - NAME_W, LANE_H); }
    const sc = signalColor(S.meta.signals[s].name);
    if (sc) { ctx.fillStyle = tint(sc, 0.07); ctx.fillRect(NAME_W, y, S.width - NAME_W, LANE_H); }
    if (s === S.selectedSignal) { ctx.fillStyle = C["accent-soft"]; ctx.fillRect(NAME_W, y, S.width - NAME_W, LANE_H); }
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(NAME_W, RULER_H, S.width - NAME_W, S.height - RULER_H);
  ctx.clip();
  const w = S.wave;
  if (w && w.mode === "events") drawEvents(w, firstLane, lastLane);
  else if (w && w.mode === "summary") drawSummary(w, firstLane, lastLane);
  drawMarks(firstLane, lastLane);
  // pattern end
  const xe = xOf(S.meta.total_ps);
  if (xe < S.width) { ctx.fillStyle = C.bg; ctx.fillRect(Math.max(NAME_W, xe), RULER_H, S.width, S.height); }
  if (S.hover && S.hover.x > NAME_W) {
    ctx.strokeStyle = C.cursor;
    ctx.globalAlpha = 0.6;
    ctx.beginPath(); ctx.moveTo(Math.round(S.hover.x) + 0.5, RULER_H); ctx.lineTo(Math.round(S.hover.x) + 0.5, S.height); ctx.stroke();
    ctx.globalAlpha = 1;
  }
  ctx.restore();

  drawNames(firstLane, lastLane);
  drawRuler();
  updateStatusView();
}

// ---------------------------------------------------------------------------
// fails of a run (placed by the runner page through /api/marks; the server checked each vector's STIL line)
// ---------------------------------------------------------------------------
async function loadMarks(id) {
  try {
    const data = await api(`/api/marks?${q({ trace: id })}`);
    if (S.activeId !== id) return;
    S.marks = data;
    S.markVectors = new Set(data.marks.map((m) => m.vector));
    renderFails();
    renderTable();
    draw();
  } catch (e) { setStatus(String(e.message || e), true); }
}

function drawMarks(firstLane, lastLane) {
  const M = S.marks;
  if (!M || !M.marks.length) return;
  const C = S.colors;
  const [t0, t1] = visibleRange();
  let lo = 0, hi = M.marks.length;  // sorted by start: the first mark that can still be visible
  while (lo < hi) { const mid = (lo + hi) >> 1; const m = M.marks[mid]; if (m.start_ps + m.period_ps < t0) lo = mid + 1; else hi = mid; }
  const current = S.markAt >= 0 ? M.marks[S.markAt] : null;
  for (let k = lo; k < M.marks.length; k++) {
    const m = M.marks[k];
    if (m.start_ps > t1) break;
    const xa = xOf(m.start_ps), xb = Math.max(xa + 2, xOf(m.start_ps + m.period_ps));
    ctx.fillStyle = C.fail;
    ctx.globalAlpha = m === current ? 0.28 : 0.12;
    ctx.fillRect(xa, RULER_H, xb - xa, S.height - RULER_H);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = C.fail;
    ctx.lineWidth = m === current ? 2 : 1.25;
    for (const lane of m.lanes) {
      if (lane < firstLane || lane > lastLane) continue;
      const y = RULER_H + lane * LANE_H - S.view.y;
      ctx.strokeRect(xa + 0.5, y + 2.5, Math.max(1, xb - xa - 1), LANE_H - 5);
    }
    ctx.lineWidth = 1;
  }
}

function goToFail(k) {
  const M = S.marks;
  if (!M || !M.marks.length) return;
  S.markAt = ((k % M.marks.length) + M.marks.length) % M.marks.length;
  const m = M.marks[S.markAt];
  if (m.lanes.length) S.selectedSignal = m.lanes[0];
  renderFails();
  selectVector(m.vector, { center: true });
}

function renderFails() {
  const box = $("failsInfo");
  const M = S.marks;
  box.innerHTML = "";
  if (!M || !M.total) { box.hidden = true; return; }
  box.hidden = false;
  box.append(el("h2", {}, "Fails"), el("p", { class: "muted", id: "failsSummary" }, `${M.title}: ${M.marks.length} of ${M.total} marked${M.unplaced.length ? `, ${M.unplaced.length} not placed` : ""}`));
  if (M.marks.length) {
    const k = S.markAt;
    box.append(el("div", { class: "fails-nav" },
      el("button", { class: "btn small", type: "button", id: "failsPrev", title: "Previous fail (in time)", onclick: () => goToFail(k < 0 ? M.marks.length - 1 : k - 1) }, "◀"),
      el("span", { id: "failsPos" }, `${k >= 0 ? k + 1 : "—"} / ${M.marks.length}`),
      el("button", { class: "btn small", type: "button", id: "failsNext", title: "Next fail (in time)", onclick: () => goToFail(k + 1) }, "▶")));
    if (k >= 0) {
      const m = M.marks[k];
      box.append(el("p", { id: "failsCurrent" }, m.text));
      if (m.missing_signals.length) box.append(el("p", { class: "muted small-print" }, `Not a signal of this view: ${m.missing_signals.join(", ")}.`));
    }
  }
  if (M.unplaced.length) {
    box.append(el("details", { id: "failsUnplaced" }, el("summary", {}, `${M.unplaced.length} not placed`),
      el("ul", {}, ...M.unplaced.slice(0, 50).map((u) => el("li", {}, `${u.text}: ${u.reason}`)))));
  }
  box.append(el("p", { class: "muted small-print" }, "A fail is marked only where this view's vector is on the STIL line that the SDK named for it; nothing is moved to a nearby vector. The waveform shows the pattern, not measured levels."));
}

function drawEvents(w, firstLane, lastLane) {
  const C = S.colors;
  const [ta, tb] = visibleRange();
  const vecs = w.vectors;
  // locate visible vectors
  let lo = 0, hi = vecs.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (vecs[mid][1] + vecs[mid][2] < ta) lo = mid + 1; else hi = mid; }
  const startIdx = lo;
  const avgPx = vecs.length ? (vecs[vecs.length - 1][1] + vecs[vecs.length - 1][2] - vecs[0][1]) / vecs.length / S.view.scale : 0;
  const top = RULER_H, bottom = S.height;

  // vector grid, waits, selection
  for (let i = startIdx; i < vecs.length && vecs[i][1] <= tb; i++) {
    const [index, start, period, , extra, waitAt, rateId] = vecs[i];
    const x0 = xOf(start), x1 = xOf(start + period);
    const rate = S.meta.rates[rateId];
    const rc = rate ? rateColor(rate.name) : null;
    if (rc) { ctx.fillStyle = tint(rc, 0.10); ctx.fillRect(x0, top, Math.max(1, x1 - x0), bottom - top); }
    if (inSel(index)) { ctx.fillStyle = C.sel; ctx.fillRect(x0, top, Math.max(1, x1 - x0), bottom - top); }
    if (extra > 0) {
      const xw = xOf(start + waitAt), xw1 = xOf(start + waitAt + extra);
      ctx.fillStyle = C["wait-fill"]; ctx.fillRect(xw, top, Math.max(1, xw1 - xw), bottom - top);
      hatch(xw, top, xw1 - xw, bottom - top, C.wait);
    }
    if (avgPx >= 4) {
      ctx.strokeStyle = C.line;
      ctx.beginPath(); ctx.moveTo(Math.round(x0) + 0.5, top); ctx.lineTo(Math.round(x0) + 0.5, bottom); ctx.stroke();
    }
  }

  const showChars = avgPx >= 16;
  ctx.font = `600 11px ${C.mono}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let s = firstLane; s <= lastLane; s++) {
    const y = RULER_H + s * LANE_H - S.view.y;
    const yHi = y + 6, yLo = y + LANE_H - 6, yMid = y + LANE_H / 2;
    const driveColor = signalColor(S.meta.signals[s].name) || C.drive;
    const driveFill = signalColor(S.meta.signals[s].name) ? tint(driveColor, 0.14) : C["drive-fill"];
    // levels
    ctx.lineWidth = 1.5;
    let prevY = null;
    for (const [a, b, st] of w.segs[s]) {
      if (b < ta || a > tb) { prevY = st === "1" ? yHi : st === "0" ? yLo : null; continue; }
      const xa = Math.max(NAME_W - 2, xOf(a)), xb = Math.min(S.width + 2, xOf(b));
      if (st === "1" || st === "0") {
        const yy = st === "1" ? yHi : yLo;
        if (st === "1") { ctx.fillStyle = driveFill; ctx.fillRect(xa, yHi, xb - xa, yLo - yHi); }
        ctx.strokeStyle = driveColor;
        ctx.setLineDash([]);
        ctx.beginPath();
        if (prevY !== null && prevY !== yy) { ctx.moveTo(xa, prevY); ctx.lineTo(xa, yy); } else ctx.moveTo(xa, yy);
        ctx.lineTo(xb, yy);
        ctx.stroke();
        prevY = yy;
      } else if (st === "Z") {
        ctx.strokeStyle = C.release;
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(xa, yMid); ctx.lineTo(xb, yMid); ctx.stroke();
        ctx.setLineDash([]);
        ctx.lineWidth = 1.5;
        prevY = null;
      }
    }
    // compare strobes
    ctx.lineWidth = 1;
    for (const [t, v] of w.marks[s]) {
      if (t < ta || t > tb) continue;
      const x = xOf(t);
      const col = v === "H" ? C["cmp-h"] : v === "L" ? C["cmp-l"] : C.masked;
      ctx.fillStyle = col;
      ctx.strokeStyle = col;
      ctx.beginPath();
      if (v === "H") { ctx.moveTo(x, yHi - 3); ctx.lineTo(x - 4, yHi + 4); ctx.lineTo(x + 4, yHi + 4); }
      else if (v === "L") { ctx.moveTo(x, yLo + 3); ctx.lineTo(x - 4, yLo - 4); ctx.lineTo(x + 4, yLo - 4); }
      else { ctx.moveTo(x, yMid - 4); ctx.lineTo(x + 4, yMid); ctx.lineTo(x, yMid + 4); ctx.lineTo(x - 4, yMid); }
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 0.35;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, y + 2); ctx.lineTo(Math.round(x) + 0.5, y + LANE_H - 2); ctx.stroke();
      ctx.globalAlpha = 1;
    }
    // tester characters
    if (showChars) {
      for (let i = startIdx; i < vecs.length && vecs[i][1] <= tb; i++) {
        const [, start, period, chars] = vecs[i];
        const c = chars[s];
        const x0 = xOf(start), x1 = xOf(start + period);
        const cx = Math.min(x1 - 9, Math.max(x0 + 9, x0 + 10));
        const cls = charClass(c);
        ctx.fillStyle = C.panel;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(cx - 6, yMid - 7, 12, 14);
        ctx.globalAlpha = 1;
        ctx.fillStyle = cls === "drive" ? driveColor : cls === "compare" ? (isHigh(c) ? C["cmp-h"] : C["cmp-l"])
          : cls === "masked" ? C.masked : C.muted;
        ctx.fillText(c, cx, yMid + 0.5);
      }
    }
  }
}

function hatch(x, y, w, h, color) {
  if (w < 2) return;
  ctx.save();
  ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.35;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let k = -h; k < w; k += 7) { ctx.moveTo(x + k, y + h); ctx.lineTo(x + k + h, y); }
  ctx.stroke();
  ctx.restore();
}

function drawSummary(w, firstLane, lastLane) {
  const C = S.colors;
  const cells = w.cells;
  for (const [a, b] of w.waits) {
    const xa = xOf(a), xb = xOf(b);
    if (xb < NAME_W || xa > S.width) continue;
    ctx.fillStyle = C["wait-fill"];
    ctx.fillRect(xa, RULER_H, Math.max(1, xb - xa), S.height - RULER_H);
    hatch(xa, RULER_H, xb - xa, S.height - RULER_H, C.wait);
  }
  for (let s = firstLane; s <= lastLane; s++) {
    const y = RULER_H + s * LANE_H - S.view.y;
    const yHi = y + 6, yLo = y + LANE_H - 6, yMid = y + LANE_H / 2;
    const driveColor = signalColor(S.meta.signals[s].name) || C.drive;
    let held = 0;  // last non-empty bucket bits: an empty bucket (e.g. a wait) holds that state
    for (let b = 0; b < cells.length; b++) {
      let bits = cells[b][s];
      if (!bits) {
        if (!held) continue;
        const t0 = w.spanStart + b * w.bucketPs;
        const xa = xOf(t0), xb = xOf(t0 + w.bucketPs);
        if (xb < NAME_W || xa > S.width) continue;
        if ((held & 1) && !(held & 2)) {
          ctx.fillStyle = driveColor;
          ctx.fillRect(xa, (held & 32) && !(held & 64) ? yHi : yLo - 1, Math.max(1, xb - xa), 1.5);
        } else if (held & 8) {
          ctx.fillStyle = C.release; ctx.fillRect(xa, yMid, Math.max(1, xb - xa), 1);
        }
        continue;
      }
      held = bits;
      const t0 = w.spanStart + b * w.bucketPs;
      const xa = xOf(t0), xb = xOf(t0 + w.bucketPs);
      if (xb < NAME_W || xa > S.width) continue;
      const wpx = Math.max(1, xb - xa);
      const changing = bits & 128;
      if (bits & 2) {                      // compares in this span
        ctx.fillStyle = (bits & 32) && (bits & 64) ? C.masked : (bits & 32) ? C["cmp-h"] : C["cmp-l"];
        ctx.globalAlpha = 0.55;
        ctx.fillRect(xa, yHi, wpx, yLo - yHi);
        ctx.globalAlpha = 1;
      } else if (bits & 1) {               // drives
        if (changing && (bits & 32) && (bits & 64)) {
          ctx.fillStyle = driveColor; ctx.globalAlpha = 0.35; ctx.fillRect(xa, yHi, wpx, yLo - yHi); ctx.globalAlpha = 1;
        } else {
          ctx.fillStyle = driveColor;
          ctx.fillRect(xa, (bits & 32) ? yHi : yLo - 1, wpx, 1.5);
        }
      } else if (bits & 4) {
        ctx.fillStyle = C.masked; ctx.globalAlpha = 0.4; ctx.fillRect(xa, yMid - 3, wpx, 6); ctx.globalAlpha = 1;
      } else if (bits & 8) {
        ctx.fillStyle = C.release; ctx.fillRect(xa, yMid, wpx, 1);
      }
    }
  }
}

function drawNames(firstLane, lastLane) {
  const C = S.colors;
  ctx.fillStyle = C["panel-2"];
  ctx.fillRect(0, RULER_H, NAME_W, S.height - RULER_H);
  ctx.strokeStyle = C.line;
  ctx.beginPath(); ctx.moveTo(NAME_W - 0.5, 0); ctx.lineTo(NAME_W - 0.5, S.height); ctx.stroke();
  ctx.textBaseline = "middle";
  for (let s = firstLane; s <= lastLane; s++) {
    const sig = S.meta.signals[s];
    const y = RULER_H + s * LANE_H - S.view.y;
    if (s === S.selectedSignal) { ctx.fillStyle = C["accent-soft"]; ctx.fillRect(0, y, NAME_W, LANE_H); }
    const sc = signalColor(sig.name);
    if (sc) { ctx.fillStyle = sc; ctx.fillRect(0, y + 3, 3, LANE_H - 6); }
    const dc = dirClass(sig.declared);
    ctx.fillStyle = dc === "in" ? C["dir-in"] : dc === "out" ? C["dir-out"] : dc === "inout" ? C["dir-inout"] : C.muted;
    ctx.fillRect(6, y + 7, 3, LANE_H - 14);
    ctx.textAlign = "left";
    ctx.font = `12px ${C.sans}`;
    ctx.fillStyle = C.text;
    ctx.fillText(ellipsize(sig.name, NAME_W - 70), 16, y + LANE_H / 2);
    ctx.textAlign = "right";
    ctx.font = `10px ${C.mono}`;
    ctx.fillStyle = C.muted;
    const dirShort = { in: "in", out: "out", inout: "io" }[dc] || "";
    const tag = [sig.pin, dirShort].filter(Boolean).join(" ");
    ctx.fillText(ellipsize(tag, 72), NAME_W - 8, y + LANE_H / 2);
    ctx.strokeStyle = C.line;
    ctx.beginPath(); ctx.moveTo(0, y + LANE_H - 0.5); ctx.lineTo(NAME_W, y + LANE_H - 0.5); ctx.stroke();
  }
}

function ellipsize(text, maxPx) {
  if (ctx.measureText(text).width <= maxPx) return text;
  let t = text;
  while (t.length > 1 && ctx.measureText(t + "…").width > maxPx) t = t.slice(0, -1);
  return t + "…";
}

function drawRuler() {
  const C = S.colors;
  ctx.fillStyle = C.panel;
  ctx.fillRect(0, 0, S.width, RULER_H);
  ctx.strokeStyle = C.line;
  ctx.beginPath(); ctx.moveTo(0, RULER_H - 0.5); ctx.lineTo(S.width, RULER_H - 0.5); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(NAME_W, 22.5); ctx.lineTo(S.width, 22.5); ctx.stroke();
  ctx.font = `10px ${C.mono}`;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = C.muted;
  ctx.fillText("time", 10, 11);
  ctx.fillText("vector", 10, 34);
  // time ticks
  const step = niceStep(S.view.scale * 90);
  const [ta, tb] = visibleRange();
  const first = Math.ceil(ta / step) * step;
  ctx.strokeStyle = C["line-strong"];
  for (let t = first; t <= tb; t += step) {
    const x = Math.round(xOf(t)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, 14); ctx.lineTo(x, 22); ctx.stroke();
    ctx.fillStyle = C.text;
    ctx.fillText(fmtTime(t), x + 3, 9);
  }
  // rate colours: a band under the vector numbers
  const w = S.wave;
  drawRateBand(w);
  // vector indices (needs per-vector starts: only in event mode)
  if (w && w.mode === "events") {
    const vecs = w.vectors;
    let lastLabelX = -1e9;
    for (const [index, start] of vecs) {
      if (start + 0 < ta - S.view.scale * 200 || start > tb) continue;
      const x = xOf(start);
      ctx.strokeStyle = C.line;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, 36); ctx.lineTo(Math.round(x) + 0.5, RULER_H); ctx.stroke();
      if (x - lastLabelX >= 44 && x >= NAME_W) {
        ctx.fillStyle = index === S.sel ? C.accent : C.text;
        ctx.fillText(String(index), x + 3, 31);
        lastLabelX = x;
      }
    }
  } else if (w && w.mode === "summary") {
    ctx.fillStyle = C.muted;
    ctx.fillText(`${w.first.toLocaleString()} … ${w.last.toLocaleString()}  (zoom in for vector detail)`, NAME_W + 6, 34);
  }
  if (S.hover && S.hover.x > NAME_W) {
    const label = fmtTime(S.hover.t, 4);
    ctx.font = `10px ${C.mono}`;
    const wLabel = ctx.measureText(label).width + 8;
    const x = Math.min(S.width - wLabel, S.hover.x + 4);
    ctx.fillStyle = C.cursor;
    ctx.fillRect(x, 1, wLabel, 14);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, x + 4, 8.5);
  }
}

function drawRateBand(w) {
  if (!w || !S.meta || !Object.keys(Colors.rates).length) return;
  const y = RULER_H - 5, h = 4;
  ctx.save();
  ctx.beginPath(); ctx.rect(NAME_W, 0, S.width - NAME_W, RULER_H); ctx.clip();
  if (w.mode === "events") {
    for (const [, start, period, , , , rateId] of w.vectors) {
      const rate = S.meta.rates[rateId];
      const rc = rate ? rateColor(rate.name) : null;
      if (!rc) continue;
      const xa = xOf(start), xb = xOf(start + period);
      if (xb < NAME_W || xa > S.width) continue;
      ctx.fillStyle = rc; ctx.fillRect(xa, y, Math.max(1, xb - xa), h);
    }
  } else if (w.mode === "summary") {
    w.rates.forEach((ids, b) => {
      const xa = xOf(w.spanStart + b * w.bucketPs), xb = xOf(w.spanStart + (b + 1) * w.bucketPs);
      if (xb < NAME_W || xa > S.width || !ids.length) return;
      const part = h / ids.length;
      ids.forEach((rateId, k) => {
        const rate = S.meta.rates[rateId];
        const rc = rate ? rateColor(rate.name) : null;
        if (rc) { ctx.fillStyle = rc; ctx.fillRect(xa, y + k * part, Math.max(1, xb - xa), part); }
      });
    });
  }
  ctx.restore();
}

function updateStatusView() {
  if (!S.meta) { $("statusView").textContent = ""; return; }
  const [a, b] = visibleRange();
  $("statusView").textContent = `view ${fmtTime(Math.max(0, a))} – ${fmtTime(Math.min(S.meta.total_ps, b))} · ${fmtTime(S.view.scale)}/px`;
}

// ---------------------------------------------------------------------------
// view control
// ---------------------------------------------------------------------------
function fitAll() {
  if (!S.meta) return;
  S.view.t0 = 0;
  S.view.scale = Math.max(1, S.meta.total_ps / Math.max(1, plotWidth() - 16));
}

function clampView() {
  if (!S.meta) return;
  const minScale = 1;
  const maxScale = Math.max(minScale, (S.meta.total_ps * 1.25) / plotWidth());
  S.view.scale = Math.min(maxScale, Math.max(minScale, S.view.scale));
  const span = plotWidth() * S.view.scale;
  S.view.t0 = Math.min(Math.max(-span * 0.05, S.view.t0), Math.max(-span * 0.05, S.meta.total_ps - span * 0.9));
}

function zoomAt(x, factor) {
  const t = tOf(x);
  S.view.scale *= factor;
  clampView();
  S.view.t0 = t - (x - NAME_W) * S.view.scale;
  clampView();
  requestWave();
  draw();
}

function panBy(dx) {
  S.view.t0 += dx * S.view.scale;
  clampView();
  requestWave();
  draw();
}

function centerOn(t, keepScale = true) {
  if (!keepScale) clampView();
  S.view.t0 = t - (plotWidth() / 2) * S.view.scale;
  clampView();
  requestWave();
  draw();
}

// ---------------------------------------------------------------------------
// selection / inspector
// ---------------------------------------------------------------------------
function selRange() {
  if (S.sel < 0) return [-1, -1];
  const end = S.selEnd < 0 ? S.sel : S.selEnd;
  return [Math.min(S.sel, end), Math.max(S.sel, end)];
}
function inSel(i) { const [a, b] = selRange(); return a >= 0 && i >= a && i <= b; }

async function selectVector(index, { center = false, scroll = true, extend = false } = {}) {
  if (!S.meta || index < 0 || index >= S.meta.vector_count) return;
  if (extend && S.sel >= 0) { S.selEnd = index; } else { S.sel = index; S.selEnd = index; }
  index = S.sel;
  const detail = await api(`/api/vector?${q({ trace: S.activeId, i: index })}`);
  renderVector(detail);
  if (center) {
    const vpx = detail.period / S.view.scale;
    if (vpx < 2 || vpx > plotWidth()) S.view.scale = Math.max(1, (detail.period * 12) / plotWidth());
    centerOn(detail.start + detail.period / 2);
  }
  if (scroll) scrollTableTo(S.selEnd >= 0 ? S.selEnd : index);
  renderTable();
  renderEditPanel();
  draw();
}

function renderVector(d) {
  S.selDetail = d;
  const box = $("vectorInfo");
  const sigs = S.meta.signals;
  const rows = [
    ["index", d.i.toLocaleString()],
    ["start", fmtTime(d.start, 4)],
    ["period", fmtTime(d.period, 4) + (d.extra ? `  (${waitWord()} +${fmtTime(d.extra, 4)} at +${fmtTime(d.wait_at, 4)})` : "")],
    ["rate", d.rate_name || String(d.rate)],
  ];
  if (d.line !== null && d.line !== undefined) rows.push(["STIL line", String(d.line)]);
  if (d.label) rows.push(["label", d.label]);
  if (d.record_index !== undefined) {
    rows.push(["record", `#${d.record_index} repeat ${d.repeat_iteration + 1}/${d.record.repeat}`]);
    if (d.loop_iteration !== null && d.loop_iteration !== undefined) rows.push(["loop", `iteration ${d.loop_iteration}, body ${d.loop_body_index}`]);
    rows.push(["drive", `${d.record.drive_value} en ${d.record.drive_enable}`]);
    rows.push(["expect", `${d.record.expect_value} mask ${d.record.expect_mask}`]);
  }
  box.innerHTML = "<h2>Vector</h2>";
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
  box.appendChild(dl);
  if (d.source_text) {
    const pre = document.createElement("div");
    pre.className = "source-line";
    pre.textContent = d.source_text;
    box.appendChild(pre);
  }
  const chars = document.createElement("div");
  chars.className = "chars";
  chars.style.marginTop = "8px";
  chars.style.flexWrap = "wrap";
  for (let s = 0; s < d.chars.length; s++) chars.appendChild(charEl(d.chars[s], sigs[s].name));
  box.appendChild(chars);
  const eventsHost = $("eventsList");
  eventsHost.innerHTML = "";
  $("eventsBox").hidden = !(d.events && d.events.length);
  if (d.events && d.events.length) {
    const table = document.createElement("table");
    table.className = "events";
    const sorted = d.events.slice().sort((a, b) => a.t - b.t || a.signal - b.signal);
    for (const e of sorted.slice(0, 400)) {
      const tr = document.createElement("tr");
      const what = e.kind === "D" ? `drive ${e.value}` : e.kind === "Z" ? "release (Z)" : `compare ${e.value}`;
      const cls = e.kind === "C" ? `ev-C-${e.value}` : `ev-${e.kind}`;
      tr.innerHTML = "<td></td><td></td><td></td>";
      tr.children[0].textContent = "+" + fmtTime(e.t, 3);
      tr.children[1].textContent = sigs[e.signal] ? sigs[e.signal].name : `#${e.signal}`;
      tr.children[2].textContent = what;
      tr.children[2].className = cls;
      table.appendChild(tr);
    }
    eventsHost.appendChild(table);
  }
}

function waitWord() {
  return S.meta && S.meta.wait_source === "inferred" ? "wait?" : "wait";
}

function charEl(c, title) {
  const b = document.createElement("b");
  const cls = charClass(c);
  b.className = `k-${cls}` + (cls === "compare" ? (isHigh(c) ? " h" : " l") : "");
  b.textContent = c;
  if (title) b.title = title;
  return b;
}

// ---------------------------------------------------------------------------
// editing (STIL only; edits make a copy, the source is never changed)
// ---------------------------------------------------------------------------
async function loadEditInfo() {
  if (!S.meta || S.meta.kind !== "stil") { S.edit = null; renderEditPanel(); return; }
  try { S.edit = await api(`/api/edits?${q({ trace: S.activeId })}`); }
  catch (e) { S.edit = { editable: false, reason: e.message }; }
  renderEditPanel();
}

function describeEdit(e) {
  const range = (a, b) => (b === undefined || b === null || a === b ? `#${a}` : `#${a}–${b}`);
  if (e.op === "set_char") return `${e.signal} → '${e.char}' for ${range(e.first, e.last)}`;
  if (e.op === "rename_signal") return `rename signal ${e.signal} → ${e.to}`;
  if (e.op === "add_signal") return `add signal ${e.signal} (${e.direction})`;
  if (e.op === "remove_signal") return `remove signal ${e.signal}`;
  if (e.op === "insert_wait") return `wait ${e.duration} after ${range(e.after)}`;
  if (e.op === "set_period") return `period ${e.period} for ${range(e.first, e.last)}`;
  if (e.op === "edge_time") {
    const where = e.first === undefined || e.first === null ? `in ${e.table || "table"} (every vector)` : `for ${range(e.first, e.last)}`;
    return `${e.signal} '${e.char}' event ${e.event} → ${e.time} ${where}`;
  }
  return JSON.stringify(e);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c.nodeType ? c : document.createTextNode(String(c)));
  return node;
}

function renderEditPanel() {
  const box = $("editPanel");
  box.innerHTML = "";
  if (!S.meta || S.meta.kind !== "stil") { box.hidden = true; return; }
  box.hidden = false;
  box.append(el("h2", {}, "Edit ", el("small", { class: "muted" }, "— saved as a copy; the source STIL is never changed")));
  if (S.edit && S.edit.editable === false) { box.append(el("p", { class: "muted" }, S.edit.reason)); return; }
  const [a, b] = selRange();
  const button = (label, kind, needs, title) => el("button", { class: "btn small", type: "button", disabled: needs && a < 0,
    title, onclick: () => openEditForm(kind) }, label);
  box.append(el("div", { class: "edit-actions" },
    button("Change character", "char", true, "Write another character of the same WaveformTable for one signal (0→1, H→L, drive↔compare on a bidirectional pin)"),
    button("Insert wait", "wait", true),
    button("Change period", "period", true),
    button("Move edge", "edge", true)));
  box.append(el("div", { class: "edit-actions" },
    button("Rename signal", "rename", false),
    button("Add signal", "add", false),
    button("Remove signal", "remove", false),
    el("button", { class: "btn small", type: "button", onclick: exportStil,
      title: "Write the STIL shown here (with its edits, if any) to a file of your choice" }, "Export STIL…")));
  if (a < 0) box.append(el("p", { class: "muted small-print" }, "Select a vector (Shift+click for a range) for the vector edits."));
  else box.append(el("p", { class: "muted small-print" }, a === b ? `Selected vector #${a}` : `Selected vectors #${a}–${b}`));
  if (S.editForm) box.append(buildEditForm(S.editForm));
  const edits = (S.edit && S.edit.edits) || [];
  if (edits.length) {
    const list = el("ol", { class: "edit-list" });
    box.append(el("p", { class: "muted small-print" }, `Edits (#numbers and lines refer to ${S.edit.source_name || "the source STIL"}):`));
    edits.forEach((e, i) => list.append(el("li", {}, el("span", {}, (S.edit.descriptions || [])[i] || describeEdit(e)),
      el("button", { class: "btn small", type: "button", title: "Remove this edit", onclick: () => editAction({ action: "remove", index: i }) }, "×"))));
    box.append(list);
    const preview = S.edit.preview_id;
    const footer = el("div", { class: "edit-actions" },
      preview && preview !== S.activeId ? el("button", { class: "btn small", type: "button", onclick: () => activate(preview) }, "Show edited") : null,
      el("button", { class: "btn small primary", type: "button", onclick: () => editAction({ action: "save" }) }, "Save edited STIL"),
      el("button", { class: "btn small", type: "button", onclick: () => { if (confirm("Remove all edits?")) editAction({ action: "clear" }); } }, "Clear"));
    box.append(footer);
    if (S.edit.saved) box.append(el("p", { class: "muted small-print saved" }, `Saved: ${S.edit.saved.stil}\nEdits: ${S.edit.saved.edits}`
      + (S.edit.saved.pin_map ? `\nPin map: ${S.edit.saved.pin_map}` : "")));
    if (S.edit.notes && S.edit.notes.length) box.append(el("ul", { class: "notes" }, ...S.edit.notes.map((n) => el("li", {}, n))));
  }
}

async function exportStil() {
  if (!S.meta || typeof pickPath !== "function") return;
  const stil = (S.meta.sources || []).find((s) => s.kind === "stil");
  const origin = S.meta.preprocessed_from ? S.meta.preprocessed_from.pattern : stil.path;
  const name = origin.split(/[\\/]/).pop().replace(/\.(stil|stl)$/i, "");
  const edited = S.edit && S.edit.edits && S.edit.edits.length;
  const start = joinPath(origin.slice(0, origin.length - origin.split(/[\\/]/).pop().length).replace(/[\\/]$/, "") || ".",
    `${name}${edited ? ".edited" : S.meta.preprocessed_from ? ".pre" : ".copy"}.stil`);
  const path = await pickPath({ title: edited ? "Export the edited STIL" : "Export this STIL", label: "STIL file to write", start });
  if (!path) return;
  try {
    const result = await busyWhile($("editPanel").querySelector("button[title^='Write the STIL']"),
      api(`/api/edits?${q({ trace: S.activeId })}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "export", path }) }));
    if (result.exported) setStatus(`exported ${result.exported.stil}` + (result.exported.pin_map ? ` (+ ${result.exported.pin_map.split(/[\\/]/).pop()})` : ""));
    else { S.edit = result; S.traces = result.traces || S.traces; renderTabs(); renderEditPanel(); setStatus(`exported ${result.saved.stil}`); }
  } catch (e) { setStatus(e.message, true); }
}

function openEditForm(kind) {
  S.editForm = kind;
  renderEditPanel();
}

function buildEditForm(kind) {
  const [a, b] = selRange();
  const d = S.selDetail || {};
  const form = el("form", { class: "edit-form" });
  const field = (label, input) => el("label", { class: "field" }, el("span", {}, label), input);
  const input = (name, value, attrs = {}) => el("input", Object.assign({ name, value: value ?? "", spellcheck: "false" }, attrs));
  const ns = (ps) => `${+(ps / 1000).toFixed(3)}ns`;
  const sigs = S.meta.signals;
  const sigIdx = S.selectedSignal >= 0 ? S.selectedSignal : 0;
  const signalSelect = () => el("select", { name: "signal" }, ...sigs.map((s, i) => el("option", { value: s.name, selected: i === sigIdx }, s.name)));
  const note = el("p", { class: "muted small-print" });
  if (kind === "wait") {
    form.append(field("Wait duration", input("duration", "1us", { placeholder: "e.g. 4ms" })),
      field("After vector", input("after", b, { type: "number", min: 0 })),
      el("p", { class: "muted small-print", id: "editWaitNote" }, "A hold-only vector after that one, in the edited copy (the source STIL is not changed). "
        + "Remove it later with × on its item in the edit list. Waits already in the STIL and the chunk waits Build inserts are not removed here."));
  } else if (kind === "period") {
    form.append(el("div", { class: "field-row two" },
      field("First vector", input("first", a, { type: "number", min: 0 })),
      field("Last vector", input("last", b, { type: "number", min: 0 }))),
      field("New period", input("period", d.period ? ns(d.period) : "", { placeholder: "e.g. 300ns" })));
  } else if (kind === "char") {
    const select = signalSelect();
    const current = el("input", { readonly: true, value: "" });
    const charSelect = el("select", { name: "char" });
    let choices = [];
    const warn = () => {
      const now = choices.find((c) => c.char === current.value);
      const next = choices.find((c) => c.char === charSelect.value);
      const sig = sigs.find((s) => s.name === select.value) || {};
      const turn = now && next && now.kind !== next.kind && [now.kind, next.kind].some((k) => k === "drive");
      note.className = turn ? "small-print warn-text" : "muted small-print";
      note.textContent = turn
        ? `Direction changes: ${now.kind} → ${next.kind}.` + ((sig.declared || "").toLowerCase() === "inout"
          ? " On this bidirectional pin the tester " + (next.kind === "drive" ? "starts driving: check that the DUT has released the line in these vectors."
            : "stops driving: check that the DUT drives the line when it is compared.") : "")
        : next ? `${next.char}: ${next.kind} — ${next.text}` : "";
    };
    const fill = async () => {
      const s = sigs.findIndex((x) => x.name === select.value);
      current.value = d.chars ? d.chars[s] : "";
      charSelect.innerHTML = "";
      try {
        const info = await api(`/api/edits/chars?${q({ trace: S.activeId, i: a, signal: select.value })}`);
        choices = info.choices;
        for (const c of choices) charSelect.append(el("option", { value: c.char, selected: c.char !== current.value && !charSelect.value }, `${c.char} · ${c.kind} · ${c.text}`));
        const other = choices.find((c) => c.char !== current.value);
        if (other) charSelect.value = other.char;
        warn();
      } catch (e) { note.className = "error"; note.textContent = e.message; }
    };
    select.addEventListener("change", fill);
    charSelect.addEventListener("change", warn);
    form.append(el("div", { class: "field-row two" }, field("Signal", select), field(`Now (vector #${a})`, current)),
      field("New character (defined in this vector's WaveformTable)", charSelect),
      el("div", { class: "field-row two" },
        field("First vector", input("first", a, { type: "number", min: 0 })),
        field("Last vector", input("last", b, { type: "number", min: 0 }))), note);
    fill();
  } else if (kind === "rename") {
    form.append(el("div", { class: "field-row two" }, field("Signal", signalSelect()), field("New name", input("to", "", { placeholder: "e.g. DATA_0" }))),
      el("p", { class: "muted small-print" }, "Renamed in Signals, SignalGroups, waveforms, vector targets and the pin map."));
  } else if (kind === "add") {
    const direction = el("select", { name: "direction" }, ...["In", "Out", "InOut"].map((x) => el("option", { value: x }, x)));
    const fillSelect = el("select", { name: "fill" });
    const fills = { In: ["0", "1", "Z"], Out: ["X", "H", "L"], InOut: ["Z", "0", "1", "X", "H", "L"] };
    const sync = () => { fillSelect.innerHTML = ""; for (const c of fills[direction.value]) fillSelect.append(el("option", { value: c }, c)); };
    direction.addEventListener("change", sync);
    sync();
    form.append(el("div", { class: "field-row" }, field("Name", input("signal", "", { placeholder: "e.g. TP1" })), field("Direction", direction),
      field("Every vector", fillSelect)),
      el("p", { class: "muted small-print" }, "Waveforms are added to every WaveformTable (In: 0/1/Z drives at 0 ns; Out: H/L/X compares at the table's compare time). "
        + "The edited copy gets a pin map that keeps the other signals on their bits; the new signal takes the next free bit. Change its characters afterwards with Change character."));
  } else if (kind === "remove") {
    form.append(field("Signal", signalSelect()),
      el("p", { class: "muted small-print" }, "Removed from Signals, its groups, its waveforms and every vector. The other signals keep their pin bits (the edited copy gets a pin map)."));
  } else {
    const select = signalSelect();
    const charInput = input("char", d.chars ? d.chars[sigIdx] : "");
    const eventSelect = el("select", { name: "event" });
    const timeInput = input("time", "", { placeholder: "e.g. 60ns" });
    const fillEvents = () => {
      const s = sigs.findIndex((x) => x.name === select.value);
      if (d.chars) charInput.value = d.chars[s];
      eventSelect.innerHTML = "";
      const evs = (d.events || []).filter((e) => e.signal === s).sort((x, y) => x.t - y.t);
      evs.forEach((e, k) => eventSelect.append(el("option", { value: k }, `${k}: ${ns(e.t)} ${e.kind === "D" ? "drive " + e.value : e.kind === "Z" ? "release" : "compare " + e.value}`)));
      if (!evs.length) eventSelect.append(el("option", { value: 0 }, "0"));
      timeInput.value = evs.length ? ns(evs[0].t) : "";
    };
    select.addEventListener("change", fillEvents);
    eventSelect.addEventListener("change", () => {
      const s = sigs.findIndex((x) => x.name === select.value);
      const evs = (d.events || []).filter((e) => e.signal === s).sort((x, y) => x.t - y.t);
      const ev = evs[Number(eventSelect.value)];
      if (ev) timeInput.value = ns(ev.t);
    });
    fillEvents();
    form.append(el("div", { class: "field-row two" }, field("Signal", select), field("Character", charInput)),
      el("div", { class: "field-row two" }, field("Event", eventSelect), field("New time", timeInput)),
      el("label", { class: "check" }, el("input", { type: "radio", name: "scope", value: "range", checked: true }), ` Only vectors #${a}–${b}`),
      el("label", { class: "check" }, el("input", { type: "radio", name: "scope", value: "table" }), ` Every vector using ${d.rate_name || "this WaveformTable"}`));
  }
  const error = el("p", { class: "error", hidden: true });
  form.append(error, el("div", { class: "edit-actions" },
    el("button", { class: "btn small", type: "button", onclick: () => { S.editForm = null; renderEditPanel(); } }, "Cancel"),
    el("button", { class: "btn small primary", type: "submit" }, kind === "remove" ? "Remove" : "Apply")));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    let edit;
    if (kind === "wait") edit = { op: "insert_wait", after: Number(f.get("after")), duration: f.get("duration") };
    else if (kind === "period") edit = { op: "set_period", first: Number(f.get("first")), last: Number(f.get("last")), period: f.get("period") };
    else if (kind === "char") edit = { op: "set_char", signal: f.get("signal"), char: f.get("char"), first: Number(f.get("first")), last: Number(f.get("last")) };
    else if (kind === "rename") edit = { op: "rename_signal", signal: f.get("signal"), to: String(f.get("to") || "").trim() };
    else if (kind === "add") edit = { op: "add_signal", signal: String(f.get("signal") || "").trim(), direction: f.get("direction"), fill: f.get("fill") };
    else if (kind === "remove") {
      if (!confirm(`Remove signal ${f.get("signal")} from the edited copy?`)) return;
      edit = { op: "remove_signal", signal: f.get("signal") };
    } else {
      edit = { op: "edge_time", signal: f.get("signal"), char: f.get("char"), event: Number(f.get("event")), time: f.get("time") };
      if (f.get("scope") === "table") { edit.at = a; } else { edit.first = a; edit.last = b; }
    }
    try {
      await editAction({ action: "add", edit }, edit.after ?? edit.first ?? (a >= 0 ? a : undefined));
    } catch (err) { error.textContent = err.message; error.hidden = false; }
  });
  return form;
}

async function editAction(body, focusIndex) {
  const fromId = S.activeId;
  let sourceIndex = focusIndex;
  if (S.meta && S.meta.edit_of && focusIndex !== undefined) {
    const m = await api(`/api/preview/map?${q({ trace: fromId, i: focusIndex })}`);
    sourceIndex = m.source ?? undefined;
  }
  const info = await api(`/api/edits?${q({ trace: fromId })}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  S.editForm = null;
  S.edit = info;
  if (body.action === "save") { setStatus(`saved ${info.saved.stil}`); renderEditPanel(); return; }
  S.traces = info.traces;
  renderTabs();
  const target = info.preview_id || info.source_id;
  activate(target, true);
  if (sourceIndex !== undefined && info.preview_id) {
    const m = await api(`/api/preview/map?${q({ trace: info.preview_id, source: sourceIndex })}`);
    if (m.preview !== undefined && m.preview !== null) selectVector(m.preview, { center: true });
  }
  setStatus(body.action === "add" ? `edit applied: ${describeEdit(body.edit)}` : `edits: ${(info.edits || []).length}`);
}

// ---------------------------------------------------------------------------
// vector table (virtualized)
// ---------------------------------------------------------------------------
function renderCharHead() {
  const head = $("charHead");
  head.innerHTML = "";
  if (!S.meta) return;
  for (const sig of S.meta.signals) {
    const span = document.createElement("span");
    span.textContent = sig.name;
    span.title = `${sig.name}  ${sig.pin || ""} ${sig.declared || ""}`;
    const dc = dirClass(sig.declared);
    if (dc) span.className = `d-${dc}`;
    const color = signalColor(sig.name);
    if (color) { span.style.boxShadow = `inset 0 -3px 0 ${color}`; span.classList.add("colored"); }
    head.appendChild(span);
  }
}

// Browsers cap an element's height (Firefox near 17.9 million px), so a table taller than this scrolls a
// shorter spacer and maps its scroll position onto the rows.
let TABLE_MAX_PX = 8_000_000;  // let: the browser tests lower it

function tableGeometry() {
  const box = $("tableScroll");
  const count = S.meta ? S.meta.vector_count : 0;
  const full = count * ROW_H;
  const height = Math.min(full, TABLE_MAX_PX);
  const view = box.clientHeight;
  const visibleRows = view / ROW_H;
  const maxScroll = Math.max(1, height - view);
  const maxTop = Math.max(0, count - visibleRows);
  const topRow = full <= TABLE_MAX_PX ? box.scrollTop / ROW_H : Math.min(1, box.scrollTop / maxScroll) * maxTop;
  return { box, count, full, height, visibleRows, maxScroll, maxTop, topRow };
}

function setupTable() {
  const count = S.meta ? S.meta.vector_count : 0;
  $("tableSpacer").style.height = `${Math.min(count * ROW_H, TABLE_MAX_PX)}px`;
  $("tableScroll").scrollTop = 0;
  fitRateColumn(false);
  renderTable();
}

function scrollTableTo(index) {
  const g = tableGeometry();
  if (index >= g.topRow && index <= g.topRow + g.visibleRows - 2) return;
  const top = Math.max(0, index - g.visibleRows / 2);
  g.box.scrollTop = g.full <= TABLE_MAX_PX ? top * ROW_H : (Math.min(top, g.maxTop) / Math.max(1, g.maxTop)) * g.maxScroll;
}

function renderTable() {
  const host = $("tableRows");
  if (!S.meta) { host.innerHTML = ""; return; }
  const g = tableGeometry();
  const box = g.box;
  const first = Math.max(0, Math.floor(g.topRow) - 5);
  const count = Math.ceil(g.visibleRows) + 10;
  const last = Math.min(S.meta.vector_count - 1, first + count);
  const frag = document.createDocumentFragment();
  for (let i = first; i <= last; i++) {
    const block = Math.floor(i / ROW_BLOCK) * ROW_BLOCK;
    const rows = S.rows.get(block);
    if (!rows) { loadRows(block); continue; }
    const r = rows[i - block];
    if (!r) continue;
    const div = document.createElement("div");
    div.className = "trow" + (inSel(i) ? " selected" : "") + (r.extra ? " wait" : "") + (S.markVectors && S.markVectors.has(i) ? " fail" : "");
    div.style.position = "absolute";
    div.style.top = `${box.scrollTop + (i - g.topRow) * ROW_H}px`;
    div.dataset.i = String(i);
    const rate = S.meta.rates[r.rate];
    div.innerHTML = `<span class="row-meta"><span class="c-i"></span><span class="c-t"></span><span class="c-p"></span><span class="c-r"></span><span class="c-l"></span></span><span class="chars"></span>`;
    const m = div.firstChild.children;
    m[0].textContent = i;
    m[1].textContent = fmtTime(r.start);
    m[2].textContent = fmtTime(r.period);
    m[3].textContent = rate ? rate.name : r.rate;
    m[3].title = rate ? `${rate.name} · ${fmtTime(rate.period_ps)}` : "";
    const rc = rate ? rateColor(rate.name) : null;
    if (rc) { m[3].style.color = rc; div.style.boxShadow = `inset 4px 0 0 ${rc}`; }
    m[4].textContent = r.line === null ? "" : r.line;
    const chars = div.lastChild;
    for (let s = 0; s < r.chars.length; s++) {
      const b = charEl(r.chars[s]);
      const sc = signalColor(S.meta.signals[s].name);
      if (sc) b.style.background = tint(sc, 0.16);
      chars.appendChild(b);
    }
    frag.appendChild(div);
  }
  host.innerHTML = "";
  host.appendChild(frag);
  $("charHead").style.transform = `translateX(${-box.scrollLeft}px)`;
  $("colMeta").style.transform = `translateX(${-box.scrollLeft}px)`;
}

// ---- table columns: drag an edge to resize, double-click it to fit; widths kept in this browser ----
const COLUMNS = { i: 76, t: 100, p: 84, r: 120, l: 56 };
const Columns = Object.assign({}, COLUMNS);
let columnsChosen = {};
try { columnsChosen = JSON.parse(localStorage.getItem("stpg-view-columns") || "{}") || {}; } catch (e) { columnsChosen = {}; }

function applyColumns() {
  const pane = document.querySelector(".table-pane");
  for (const key of Object.keys(COLUMNS)) pane.style.setProperty(`--w-${key}`, `${Columns[key]}px`);
}

function saveColumns() {
  try { localStorage.setItem("stpg-view-columns", JSON.stringify(columnsChosen)); } catch (e) { /* storage unavailable */ }
}

function textWidth(texts, font) {
  ctx.save();
  ctx.font = font;
  let widest = 0;
  for (const text of texts) widest = Math.max(widest, ctx.measureText(String(text)).width);
  ctx.restore();
  return widest;
}

// The rate column follows the longest rate name of the open pattern unless a width was chosen by hand.
function fitRateColumn(force) {
  if (!S.meta) return;
  if (!force && columnsChosen.r) { Columns.r = columnsChosen.r; applyColumns(); return; }
  const widest = textWidth(S.meta.rates.map((r) => r.name), `12px ${S.colors.mono}`);
  Columns.r = Math.max(COLUMNS.r, Math.min(480, Math.ceil(widest) + 18));
  if (force) { columnsChosen.r = Columns.r; saveColumns(); }
  applyColumns();
}

function fitColumn(key) {
  if (key === "r") { fitRateColumn(true); renderTable(); return; }
  const cells = [...document.querySelectorAll(`#tableRows .c-${key}`)].map((c) => c.textContent);
  const head = document.querySelector(`#colMeta .c-${key}`);
  const widest = Math.max(textWidth(cells, `12px ${S.colors.mono}`), textWidth([head ? head.textContent : ""], `11px ${S.colors.mono}`));
  Columns[key] = Math.max(28, Math.ceil(widest) + 20);
  columnsChosen[key] = Columns[key];
  saveColumns();
  applyColumns();
}

(function setupColumns() {
  for (const key of Object.keys(COLUMNS)) if (columnsChosen[key]) Columns[key] = columnsChosen[key];
  applyColumns();
  for (const key of Object.keys(COLUMNS)) {
    const cell = document.querySelector(`#colMeta .c-${key}`);
    const grip = document.createElement("i");
    grip.className = "col-grip";
    grip.title = "Drag to resize · double-click to fit";
    cell.appendChild(grip);
    grip.addEventListener("dblclick", (e) => { e.preventDefault(); fitColumn(key); });
    grip.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX, startW = Columns[key];
      document.body.classList.add("col-resizing");
      const move = (ev) => {
        Columns[key] = Math.max(24, Math.min(900, startW + ev.clientX - startX));
        applyColumns();
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", () => {
        window.removeEventListener("mousemove", move);
        document.body.classList.remove("col-resizing");
        columnsChosen[key] = Columns[key];
        saveColumns();
      }, { once: true });
    });
  }
})();

function loadRows(block) {
  if (S.rowsPending.has(block)) return;
  S.rowsPending.add(block);
  const traceId = S.activeId;
  api(`/api/rows?${q({ trace: traceId, start: block, count: ROW_BLOCK })}`).then((data) => {
    if (traceId !== S.activeId) return;
    S.rows.set(block, data.rows);
    renderTable();
  }).catch((e) => setStatus(e.message, true)).finally(() => S.rowsPending.delete(block));
}

// ---------------------------------------------------------------------------
// interaction
// ---------------------------------------------------------------------------
function vectorAtX(x) {
  const w = S.wave;
  if (!w || w.mode !== "events") return null;
  const t = tOf(x);
  const vecs = w.vectors;
  let lo = 0, hi = vecs.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const [, start, period] = vecs[mid];
    if (t < start) hi = mid - 1; else if (t >= start + period) lo = mid + 1; else return vecs[mid];
  }
  return null;
}

let drag = null;
canvas.addEventListener("mousedown", (e) => {
  if (!S.meta) return;
  drag = { x: e.offsetX, y: e.offsetY, t0: S.view.t0, viewY: S.view.y, moved: false };
});
window.addEventListener("mousemove", (e) => {
  if (!drag) return;
  const dx = e.clientX - canvas.getBoundingClientRect().left - drag.x;
  const dy = e.clientY - canvas.getBoundingClientRect().top - drag.y;
  if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
  drag.moved = true;
  canvas.classList.add("panning");
  S.view.t0 = drag.t0 - dx * S.view.scale;
  S.view.y = drag.viewY - dy;
  clampView();
  requestWave();
  draw();
});
window.addEventListener("mouseup", async (e) => {
  if (!drag) return;
  const wasDrag = drag.moved;
  const start = drag;
  drag = null;
  canvas.classList.remove("panning");
  if (wasDrag || e.target !== canvas) return;
  const x = start.x, y = start.y;
  if (x < NAME_W && y > RULER_H) {
    const lane = Math.floor((y - RULER_H + S.view.y) / LANE_H);
    S.selectedSignal = lane === S.selectedSignal ? -1 : lane;
    draw();
    return;
  }
  const extend = e.shiftKey;
  const hit = vectorAtX(x);
  if (hit) { selectVector(hit[0], { extend }); return; }
  try {
    const found = await api(`/api/find?${q({ trace: S.activeId, t: Math.round(tOf(x)) })}`);
    if (found.index >= 0) selectVector(found.index, { extend });
  } catch (err) { setStatus(err.message, true); }
});

canvas.addEventListener("mousemove", (e) => {
  if (!S.meta) return;
  const x = e.offsetX, y = e.offsetY;
  S.hover = { x, y, t: tOf(x) };
  updateTooltip(x, y);
  draw();
});
canvas.addEventListener("mouseleave", () => { S.hover = null; $("tooltip").hidden = true; draw(); });
canvas.addEventListener("contextmenu", (e) => {
  if (!S.meta || e.offsetX >= NAME_W || e.offsetY <= RULER_H) return;
  const sig = S.meta.signals[Math.floor((e.offsetY - RULER_H + S.view.y) / LANE_H)];
  if (!sig) return;
  e.preventDefault();
  openColors({ kind: "signals", name: sig.name });
});

function updateTooltip(x, y) {
  const tip = $("tooltip");
  const hit = x > NAME_W ? vectorAtX(x) : null;
  if (!hit || y < RULER_H) {
    tip.hidden = true;
    $("statusCursor").textContent = x > NAME_W ? `t = ${fmtTime(tOf(x), 4)}` : "—";
    return;
  }
  const lane = Math.floor((y - RULER_H + S.view.y) / LANE_H);
  const sig = S.meta.signals[lane];
  const [index, start, period, chars, extra] = hit;
  const lines = [`vector ${index}`, `start  ${fmtTime(start, 4)}`, `period ${fmtTime(period, 4)}${extra ? `  ${waitWord()} +${fmtTime(extra, 3)}` : ""}`];
  if (sig) lines.push(`${sig.name} = ${chars[lane]}`);
  tip.textContent = lines.join("\n");
  tip.hidden = false;
  const host = $("waveHost");
  tip.style.left = `${Math.min(x + 14, host.clientWidth - tip.offsetWidth - 6)}px`;
  tip.style.top = `${Math.min(y + 14, host.clientHeight - tip.offsetHeight - 6)}px`;
  $("statusCursor").textContent = `t = ${fmtTime(tOf(x), 4)} · vector ${index}` + (sig ? ` · ${sig.name}=${chars[lane]}` : "");
}

canvas.addEventListener("wheel", (e) => {
  if (!S.meta) return;
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    zoomAt(e.offsetX, Math.exp(e.deltaY * 0.0025));
  } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    panBy((e.shiftKey ? e.deltaY : e.deltaX) || 0);
  } else {
    S.view.y += e.deltaY;
    draw();
  }
}, { passive: false });

window.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
  if (document.querySelector("dialog[open]")) return;
  if (!S.meta) { if (e.key === "o") $("openBtn").click(); return; }
  const mid = NAME_W + plotWidth() / 2;
  switch (e.key) {
    case "+": case "=": zoomAt(mid, 0.5); break;
    case "-": case "_": zoomAt(mid, 2); break;
    case "f": case "F": fitAll(); requestWave(); draw(); break;
    case "ArrowRight": if (e.shiftKey) panBy(plotWidth() / 4); else selectVector(Math.min(S.meta.vector_count - 1, S.sel + 1), { center: true }); break;
    case "ArrowLeft": if (e.shiftKey) panBy(-plotWidth() / 4); else selectVector(Math.max(0, S.sel - 1), { center: true }); break;
    case "z": case "Z": if (S.sel >= 0) { S.view.scale = 1; selectVector(S.sel, { center: true }); } break;
    case "/": e.preventDefault(); $("gotoInput").focus(); break;
    default: return;
  }
  e.preventDefault();
});

$("tableScroll").addEventListener("scroll", () => renderTable());
$("tableRows").addEventListener("contextmenu", (e) => {
  const cell = e.target.closest(".c-r");
  if (!cell || !S.meta) return;
  e.preventDefault();
  openColors({ kind: "rates", name: cell.textContent });
});
$("tableRows").addEventListener("click", (e) => {
  const row = e.target.closest(".trow");
  if (row) selectVector(Number(row.dataset.i), { center: !e.shiftKey, scroll: false, extend: e.shiftKey });
});

$("gotoForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!S.meta) return;
  const text = $("gotoInput").value.trim();
  let params = null;
  const line = /^L(\d+)$/i.exec(text);
  const t = parseTime(text);
  if (line) params = { line: line[1] };
  else if (t !== null) params = { t };
  else if (/^\d+$/.test(text)) params = { vector: text };
  if (!params) { setStatus("Go to: vector number, L<line> or a time such as 3.2us", true); return; }
  try {
    const found = await api(`/api/find?${q(Object.assign({ trace: S.activeId }, params))}`);
    if (found.index < 0) { setStatus("not found", true); return; }
    setStatus("");
    selectVector(found.index, { center: true });
  } catch (err) { setStatus(err.message, true); }
});

// splitter
(() => {
  const split = $("splitter");
  let startY = 0, startH = 0;
  const pane = document.querySelector(".wave-pane");
  const onMove = (e) => {
    const h = Math.min(pane.clientHeight - 120, Math.max(60, startH - (e.clientY - startY)));
    pane.style.setProperty("--table-h", `${h}px`);
    onResize();
  };
  split.addEventListener("mousedown", (e) => {
    startY = e.clientY;
    startH = document.querySelector(".table-pane").clientHeight;
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", () => window.removeEventListener("mousemove", onMove), { once: true });
    e.preventDefault();
  });
})();

// ---------------------------------------------------------------------------
// open dialog, drag & drop, export
// ---------------------------------------------------------------------------
let browseDir = "";
async function browse(dir) {
  try {
    const data = await api(`/api/browse?${q({ dir: dir || "" })}`);
    browseDir = data.dir;
    $("browsePath").value = data.dir;
    $("browseHome").dataset.home = data.home;
    showRoots($("browseRoots"), data.roots, browse);
    const list = $("browseList");
    list.innerHTML = "";
    for (const entry of data.entries) {
      const li = document.createElement("li");
      const lower = entry.name.toLowerCase();
      const isPattern = /\.(stil|stl|bin)$/.test(lower) || /\.json(\.gz)?$/.test(lower);
      li.className = (entry.dir ? "dir" : "file") + (isPattern ? " pattern" : "");
      li.innerHTML = '<span class="n"></span><span class="size"></span>';
      li.querySelector(".n").textContent = entry.name;
      li.querySelector(".size").textContent = entry.dir ? "" : fmtSize(entry.size);
      const full = joinPath(data.dir, entry.name);
      li.addEventListener("click", () => { if (entry.dir) browse(full); else togglePath(full); });
      li.addEventListener("dblclick", () => { if (!entry.dir) { setPaths([full]); submitOpen(); } });
      if (entry.dir) {
        li.title = "Open folder (double-click a package folder name to open it as a pattern)";
        li.addEventListener("dblclick", (ev) => { ev.stopPropagation(); });
      }
      list.appendChild(li);
    }
  } catch (e) { showOpenError(e.message); }
}

// One button per root (every Windows drive that exists, or "/"); a network path can be typed and opened with Go.
function showRoots(box, roots, open) {
  box.innerHTML = "";
  for (const root of roots || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn small";
    button.textContent = root;
    button.addEventListener("click", () => open(root));
    box.appendChild(button);
  }
}

function joinPath(dir, name) {
  const sep = dir.includes("\\") && !dir.includes("/") ? "\\" : "/";
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

function fmtSize(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function currentPaths() { return $("openPaths").value.split("\n").map((s) => s.trim()).filter(Boolean); }
function setPaths(paths) { $("openPaths").value = paths.join("\n"); }
function togglePath(p) {
  const paths = currentPaths();
  const i = paths.indexOf(p);
  if (i >= 0) paths.splice(i, 1); else paths.push(p);
  setPaths(paths);
}
function showOpenError(msg) { const el = $("openError"); el.textContent = msg; el.hidden = !msg; }

async function submitOpen() {
  showOpenError("");
  const paths = currentPaths();
  if (!paths.length) { showOpenError("Choose at least one file."); return; }
  const submit = $("openSubmit");
  if (submit.disabled) return;  // one open at a time (a long pattern takes a while)
  submit.disabled = true;
  try {
    const result = await busyWhile(submit, api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, pin_map: $("openPinMap").value.trim() || null, replace: !$("openAdd").checked }),
    }));
    $("openDialog").close();
    setStatus(`opened ${result.loaded.join(", ")}`);
    await refreshState(result.loaded[0]);
  } catch (e) { showOpenError(e.message); } finally { submit.disabled = false; }
}

$("openBtn").addEventListener("click", () => {
  showOpenError("");
  $("openDialog").showModal();
  browse(browseDir);
});
$("openForm").addEventListener("submit", (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  submitOpen();
});
$("browseUp").addEventListener("click", () => browse(joinPath(browseDir, "..")));
$("browseHome").addEventListener("click", (e) => browse(e.currentTarget.dataset.home || ""));
$("browseGo").addEventListener("click", () => browse($("browsePath").value));
$("browsePath").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); browse($("browsePath").value); } });

$("vcdBtn").addEventListener("click", async () => {
  if (!S.activeId) return;
  const button = $("vcdBtn");
  const done = activityBegin("/api/export/vcd", {});
  markBusy(button, true);
  try {  // fetched (not a plain link) so that the page shows the export until the file is ready
    const response = await fetch(`/api/export/vcd?${q({ trace: S.activeId })}`, { headers: { "X-STPG-Token": TOKEN } });
    if (!response.ok) {
      let message = response.statusText;
      try { message = (await response.json()).error || message; } catch (e) { /* not JSON */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    const name = (/filename\*=UTF-8''([^;]+)/.exec(response.headers.get("Content-Disposition") || "") || [])[1];
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name ? decodeURIComponent(name) : "pattern.vcd";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    setStatus(`exported ${link.download}`);
  } catch (e) { setStatus(e.message, true); } finally { markBusy(button, false); done(); }
});
$("helpBtn").addEventListener("click", () => $("helpDialog").showModal());
$("extBtn").addEventListener("click", async () => {
  if (!S.activeId) return;
  try {
    const r = await api(`/api/external?${q({ trace: S.activeId })}`, { method: "POST" });
    setStatus(`opened ${r.vcd.split(/[\\/]/).pop()} in ${r.viewer.split(/[\\/]/).pop()}`);
  } catch (e) { setStatus(e.message, true); }
});
api("/api/viewers").then((r) => {
  if (r.external) {
    const name = r.external.split(/[\\/]/).pop().replace(/\.exe$/i, "");
    $("extBtn").textContent = `Open in ${name.charAt(0).toUpperCase()}${name.slice(1)}`;
    $("extBtn").hidden = false;
  }
}).catch(() => {});

// ---- build in the runner: a new runner project from the open STIL ----
// The runner's New project dialog, filled in from where this STIL came from: a plain STIL as is, an ATE
// generator file (or project + pattern) as the same ATE input form, an edited copy as the STIL it is now.
function runnerPrefill(meta) {
  const source = (kind) => (meta.sources || []).find((s) => s.kind === kind);
  const stil = source("stil");
  const pinMap = source("pin_map");
  const pre = meta.edit_of ? null : meta.preprocessed_from;
  const base = pre ? pre.pattern : stil.path;
  const name = base.split(/[\\/]/).pop();
  const folder = base.slice(0, base.length - name.length).replace(/[\\/]$/, "") || ".";
  const stem = name.replace(/\.(stil|stl)$/i, "").replace(/\.pre$/, "");
  const prefill = { directory: joinPath(folder, `${stem}-project`), pin_map: pinMap ? pinMap.path : "" };
  if (pre && pre.project === pre.pattern) Object.assign(prefill, { input_form: "ate-single", stil: pre.pattern });
  else if (pre) Object.assign(prefill, { input_form: "ate-pair", stil: pre.pattern, ate_project: pre.project });
  else Object.assign(prefill, { input_form: "stil", stil: stil.path });
  return prefill;
}

$("runnerBtn").addEventListener("click", async () => {
  if (!S.meta || S.meta.kind !== "stil" || typeof newProjectDialog !== "function") return;
  const prefill = runnerPrefill(S.meta);
  await setMode("run");
  newProjectDialog(prefill);
  if (S.meta.edit_of && !(S.edit && S.edit.saved)) {
    setStatus("the edited STIL is not saved: the project copies the preview from the viewer's work folder");
  }
});
$("colorBtn").addEventListener("click", () => openColors());
$("colorAutoRates").addEventListener("click", () => colorAll("rates"));
$("colorClearRates").addEventListener("click", () => clearColors("rates"));
$("colorAutoSignals").addEventListener("click", () => colorAll("signals"));
$("colorClearSignals").addEventListener("click", () => clearColors("signals"));
$("themeBtn").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("stpg-view-theme", root.dataset.theme); } catch (e) { /* storage unavailable */ }
  readColors();
  draw();
});

const host = $("waveHost");
host.addEventListener("dragover", (e) => { e.preventDefault(); host.classList.add("dragover"); });
host.addEventListener("dragleave", () => host.classList.remove("dragover"));
host.addEventListener("drop", async (e) => {
  e.preventDefault();
  host.classList.remove("dragover");
  const files = Array.from(e.dataTransfer.files || []);
  if (!files.length) return;
  const batch = Math.random().toString(16).slice(2, 10);
  try {
    setStatus(`uploading ${files.length} file(s)…`);
    const paths = [];
    for (const f of files) {
      const res = await api(`/api/upload?${q({ name: f.name, batch })}`, { method: "POST", body: f });
      paths.push(res.path);
    }
    const result = await api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, replace: true }),
    });
    setStatus(`opened ${result.loaded.join(", ")}`);
    await refreshState(result.loaded[0]);
  } catch (err) { setStatus(err.message, true); }
});

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
function onResize() {
  resizeCanvas();
  if (S.meta) { clampView(); requestWave(); }
  draw();
  renderTable();
}

(function boot() {
  try {
    const theme = localStorage.getItem("stpg-view-theme");
    if (theme) document.documentElement.dataset.theme = theme;
  } catch (e) { /* storage unavailable */ }
  readColors();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readColors(); draw(); });
  new ResizeObserver(onResize).observe($("waveHost"));
  resizeCanvas();
  refreshState().catch((e) => setStatus(e.message, true));
})();
