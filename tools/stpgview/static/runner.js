"use strict";

// ---------------------------------------------------------------------------
// Runner mode: project/bundle -> build/verify/plan -> run, local bindings, an optional
// external accessory process (for example a separately installed logic-analyzer tool)
// and results. Every action calls the shared SDK through the local server; nothing runs
// on page load or by itself. Accessory programs are never loaded into this page.
// Uses helpers from app.js: api, q, el, $, fmtTime, setStatus, refreshState.
// ---------------------------------------------------------------------------

const R = {
  state: null,
  available: false,
  project: null,
  bundle: null,
  jobs: {},              // slot -> {job, timer}
  description: null,     // the one STIL details shown: {dir, where: "run" | "results", pending, data}
  devices: [],
  analyzers: [],
  controllers: [],
  device: "",
  controller: "",        // manual GPIO card: selected controller binding ("" = none)
  runController: "",     // run card: controller for this invocation's GPIO actions ("" = no controller I/O)
  gpioPlan: null,        // stpg_gpio_action_plan for the current bundle (or null)
  plan: null,            // run plan (prepare_run) of the current bundle
  external: { config: "", summary: null, error: null, use: false },  // a separately installed accessory process
  lastRun: null,
  results: null,
};

const STAGE_LABELS = {
  preflight: "preflight", cache_verify: "verify cached bundle", snapshot_inputs: "snapshot inputs",
  source_analysis: "source analysis", segmentation: "segmentation decision", compile_package: "compile package", segmented_compile: "segmented compile",
  wait_plan: "wait plan", bundle_verify: "verify bundle", run_verify: "enter SDK preparation", device_preflight: "device preflight",
  workspace: "reserve result workspace", port_check: "check USB port metadata", bundle_files: "hash bundle files",
  bundle_check: "bundle check mode", verified_files: "rehash verified files", shared_files: "recheck shared snapshot files", timeline_files: "recheck timeline files",
  snapshot: "snapshot pattern bytes", snapshot_verify: "recheck snapshot inventory", snapshot_files: "hash snapshot files",
  upload: "upload", run: "RUN", fail_log: "read retained FAIL_LOG", observe: "observe (STATUS)", segment: "segment", analyzer: "analyzer",
  rebind_copy: "copy the unchanged bundle files (no recompile)",

};
// The SDK checks the STPG port twice: before its bundle verification and again right before it opens the port.
const PORT_CHECK_PHASES = { before_bundle_verify: "before the bundle check", before_open: "right before the port is opened" };
const PHASES = [["before_run", "before RUN (after upload accepted)"], ["after_terminal", "after a valid DONE/HOLD"]];

function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

function badge(text, kind = "muted", title = "") {
  return el("span", { class: `badge ${kind}`, title }, text);
}

function short(sha) { return sha ? `${sha.slice(0, 12)}…` : "—"; }

function copyable(text, label) {
  const span = el("span", { class: "path copy", title: "Click to copy" }, label || text);
  span.addEventListener("click", () => {
    try { navigator.clipboard.writeText(text); setStatus("copied"); } catch (e) { setStatus("copy failed", true); }
  });
  return span;
}

function kvList(rows) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of rows) {
    dl.append(el("dt", {}, k));
    const dd = el("dd");
    if (v && v.nodeType) dd.append(v); else dd.textContent = v === null || v === undefined ? "—" : String(v);
    dl.append(dd);
  }
  return dl;
}

function table(headers, rows, numeric = []) {
  const t = el("table", { class: "tbl" });
  const tr = el("tr");
  headers.forEach((h, i) => tr.append(el("th", { class: numeric.includes(i) ? "num" : "" }, h)));
  t.append(el("thead", {}, tr));
  const body = el("tbody");
  for (const row of rows) {
    const r = el("tr");
    row.forEach((cell, i) => {
      const td = el("td", { class: numeric.includes(i) ? "num" : "" });
      if (cell && cell.nodeType) td.append(cell); else td.textContent = cell === null || cell === undefined ? "—" : String(cell);
      r.append(td);
    });
    body.append(r);
  }
  t.append(body);
  return el("div", { class: "tbl-wrap" }, t);
}

function notice(kind, title, items) {
  const box = el("div", { class: `notice ${kind}` });
  if (title) box.append(el("strong", {}, title));
  if (items && items.length) box.append(el("ul", {}, ...items.map((x) => el("li", {}, x))));
  return box;
}

function errorText(e) { return e && e.message ? e.message : String(e); }

// ---------------------------------------------------------------------------
// mode switch
// ---------------------------------------------------------------------------
function setMode(mode) {
  const run = mode === "run";
  document.querySelector(".workspace").hidden = run;
  $("runnerPage").hidden = !run;
  $("modeView").classList.toggle("active", !run);
  $("modeRun").classList.toggle("active", run);
  $("modeView").setAttribute("aria-selected", String(!run));
  $("modeRun").setAttribute("aria-selected", String(run));
  document.body.classList.toggle("runner-mode", run);
  try { localStorage.setItem("stpg-view-mode", run ? "run" : "view"); } catch (e) { /* storage unavailable */ }
  if (run) return refreshRunner();
  if (typeof onResize === "function") onResize();
  return Promise.resolve();
}
$("modeView").addEventListener("click", () => setMode("view"));
$("modeRun").addEventListener("click", () => setMode("run"));

async function refreshRunner() {
  try {
    R.state = await api("/api/runner/state");
    R.available = true;
  } catch (e) {
    R.available = false;
    $("runnerBanner").hidden = false;
    $("runnerBanner").textContent = `Runner unavailable: ${errorText(e)}`;
    return;
  }
  const banner = $("runnerBanner");
  const parts = [];
  if (R.state.demo) parts.push("Demo mode: the device, analyzer (Reset LA target) and GPIO controller named \"simulated\" are host-only models — no hardware is touched and their results prove nothing about a DUT or a board. A simulated run accepts only an accessory config in simulated mode.");
  if (!R.state.device_run) parts.push("Device execution is disabled (--no-device-run): build, verify and plan work; Run, external accessory runs and GPIO controller operations on real bindings are off.");
  parts.push("Execution is the runner SDK's unvalidated preview (STATUS counters and, after compare failures, the retained fail log; no samples; no STOP on cancel).");
  banner.textContent = parts.join(" ");
  banner.hidden = false;
  await loadBindings();
  renderAll();
  reattachDeviceJob();
}

// After a reload (or in a second tab) the server may still be running a device job. Watch it again so its
// progress, result and Cancel stay reachable; nothing is started, resent or confirmed by this.
function reattachDeviceJob() {
  const id = R.state && R.state.active_device_job;
  if (!id || Object.values(R.jobs).some((entry) => entry && entry.job && entry.job.id === id)) return;
  const summary = (R.state.jobs || []).find((j) => j.id === id);
  if (!summary) return;
  const slot = { run: "run", control: "control" }[summary.kind];
  if (!slot) {  // an Identify belongs to a dialog that no longer exists; its evidence file stays in Results
    setStatus(`A device job is still running on the server: ${summary.title}. Run and GPIO stay locked until it ends.`);
    return;
  }
  watchJob(slot, Object.assign({}, summary, { events: [], next: 0 }), async (done) => {
    R.state = await api("/api/runner/state").catch(() => R.state);
    renderAll(); loadResults();
  });
}

function renderAll() {
  renderProject();
  renderBuild();
  renderBindings();
  renderControl();
  renderExternal();
  renderRun();
  renderResults();
}

// ---------------------------------------------------------------------------
// path picker (server-side browse, same as the Open dialog)
// ---------------------------------------------------------------------------
let pickResolve = null;
let pickDirNow = "";
let pickEntries = [];
// newDir: choose where a folder that does not exist yet will be made (a parent folder + a new name).
// Nothing is created here; the operation that needs the folder creates it exclusively.
function pickPath({ title, label = "Path", start = "", pickDir = false, newDir = false } = {}) {
  $("pickTitle").textContent = title || "Choose";
  $("pickLabel").textContent = label;
  $("pickPath").value = start || "";
  $("pickError").hidden = true;
  $("pickDialog").dataset.pickDir = pickDir && !newDir ? "1" : "";
  $("pickDialog").dataset.newDir = newDir ? "1" : "";
  $("pickNewRow").hidden = !newDir;
  $("pickPath").readOnly = newDir;  // the path follows the browsed folder and the new name
  $("pickNewName").value = newDir && start ? start.split(/[\\/]/).filter(Boolean).pop() || "" : "";
  $("pickDialog").showModal();
  pickBrowse(start ? start.replace(/[\\/][^\\/]*$/, "") : pickDirNow);
  if (newDir) $("pickNewName").focus();
  return new Promise((resolve) => { pickResolve = resolve; });
}
function syncNewPath() {
  if (!$("pickDialog").dataset.newDir) return;
  const name = $("pickNewName").value.trim();
  $("pickPath").value = name ? joinPath(pickDirNow, name) : "";
}
async function pickBrowse(dir) {
  try {
    const data = await api(`/api/browse?${q({ dir: dir || "" })}`);
    pickDirNow = data.dir;
    pickEntries = data.entries;
    $("pickDir").value = data.dir;
    $("pickHome").dataset.home = data.home;
    showRoots($("pickRoots"), data.roots, pickBrowse);
    const list = $("pickList");
    list.innerHTML = "";
    const newDir = Boolean($("pickDialog").dataset.newDir);
    for (const entry of data.entries) {
      const full = joinPath(data.dir, entry.name);
      const li = el("li", { class: entry.dir ? "dir" : "file" }, el("span", { class: "n" }, entry.name),
        el("span", { class: "size" }, entry.dir ? "" : fmtSize(entry.size)));
      if (/\.(stil|stl|json|csv|lac)$/i.test(entry.name) || entry.name === "runner.bundle.json") li.classList.add("pattern");
      if (newDir) {
        li.addEventListener("click", () => { if (entry.dir) pickBrowse(full); });  // an existing name is never the answer
      } else {
        li.addEventListener("click", () => { if (entry.dir) { pickBrowse(full); if ($("pickDialog").dataset.pickDir) $("pickPath").value = full; } else $("pickPath").value = full; });
        li.addEventListener("dblclick", () => { if (!entry.dir) { $("pickPath").value = full; finishPick(); } });
      }
      list.append(li);
    }
    if ($("pickDialog").dataset.pickDir && !$("pickPath").value) $("pickPath").value = data.dir;
    syncNewPath();
  } catch (e) { $("pickError").textContent = errorText(e); $("pickError").hidden = false; }
}
function newDirProblem() {
  const name = $("pickNewName").value.trim();
  if (!name) return $("pickPath").value.trim() ? "" : "Type a name for the new folder.";
  if (/[\\/]/.test(name) || name === "." || name === "..") return "Type one folder name (no / or \\); browse to the parent folder above.";
  // compared without case: on Windows and macOS "Run" and "run" are the same folder
  if (pickEntries.some((entry) => entry.name.toLowerCase() === name.toLowerCase())) return `${name} already exists here (names that differ only in upper and lower case count as the same). Type a new name (an existing folder is never reused).`;
  return "";
}
function finishPick(value) {
  const dialog = $("pickDialog");
  if (dialog.open) dialog.close();
  if (pickResolve) { const r = pickResolve; pickResolve = null; r(value === undefined ? $("pickPath").value.trim() || null : value); }
}
$("pickNewName").addEventListener("input", () => { $("pickError").hidden = true; syncNewPath(); });
// Enter in a text field means Choose (the form's first button is Cancel, which implicit submission would press).
for (const id of ["pickNewName", "pickPath"]) {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("pickOk").click(); } });
}
$("pickDir").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("pickGo").click(); } });
$("pickForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.submitter && e.submitter.value === "cancel") { finishPick(null); return; }
  if ($("pickDialog").dataset.newDir) {
    const problem = newDirProblem();
    if (problem) { $("pickError").textContent = problem; $("pickError").hidden = false; return; }
  }
  finishPick();
});
$("pickDialog").addEventListener("close", () => { if (pickResolve) finishPick(null); });
$("pickUp").addEventListener("click", () => pickBrowse(joinPath(pickDirNow, "..")));
$("pickHome").addEventListener("click", (e) => pickBrowse(e.currentTarget.dataset.home || ""));
$("pickGo").addEventListener("click", () => pickBrowse($("pickDir").value));

// ---------------------------------------------------------------------------
// generic form dialog
// ---------------------------------------------------------------------------
function formDialog(title, build, onOk, { okText = "OK", wide = true } = {}) {
  const dialog = $("formDialog");
  $("formDialogTitle").textContent = title;
  const body = $("formDialogBody");
  body.innerHTML = "";
  $("formDialogError").hidden = true;
  $("formDialogOk").textContent = okText;
  $("formDialogOk").hidden = !onOk;
  dialog.classList.toggle("wide", wide);
  build(body);
  dialog._onOk = onOk;
  dialog.showModal();
}
function formError(msg) { const e = $("formDialogError"); e.textContent = msg || ""; e.hidden = !msg; }
$("formDialogForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const dialog = $("formDialog");
  if (!dialog._onOk) { dialog.close(); return; }
  formError("");
  const ok = $("formDialogOk");
  ok.disabled = true;  // one request at a time (creating a project can take a while)
  try {
    const keep = await dialog._onOk($("formDialogBody"));
    if (keep !== true) dialog.close();
  } catch (err) { formError(errorText(err)); } finally { ok.disabled = false; }
});
// Enter in a text field must not submit: the form's first button is Close, which implicit submission would press.
$("formDialogForm").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target instanceof HTMLInputElement) e.preventDefault();
});

function field(label, input, hint) {
  return el("label", { class: "field" }, el("span", {}, label), input, hint ? el("span", { class: "hint" }, hint) : null);
}
function input(name, value = "", attrs = {}) { return el("input", Object.assign({ name, value: value ?? "", spellcheck: "false" }, attrs)); }
function select(name, options, value, placeholder) {
  const s = el("select", { name });
  if (placeholder) s.append(el("option", { value: "", disabled: true, selected: !value }, placeholder));
  for (const opt of options) {
    const [v, text] = Array.isArray(opt) ? opt : [opt, opt];
    s.append(el("option", { value: v, selected: v === value }, text));
  }
  return s;
}
function pathInput(name, value, opts) {
  const inp = input(name, value);
  const wrap = el("div", { class: "browse-bar" }, inp,
    el("button", { class: "btn small", type: "button", onclick: async () => {
      const dialog = $("formDialog");
      const keep = dialog._onOk;
      const chosen = await pickPath(Object.assign({ start: inp.value }, opts));
      if (!dialog.open) dialog.showModal();
      dialog._onOk = keep;
      if (chosen) { inp.value = chosen; inp.dispatchEvent(new Event("change")); }
    } }, "Browse…"));
  wrap.input = inp;
  return wrap;
}
function intValue(text) {
  const t = String(text ?? "").trim();
  if (!t) return null;
  const v = /^0x[0-9a-f]+$/i.test(t) ? parseInt(t, 16) : /^\d+$/.test(t) ? parseInt(t, 10) : NaN;
  if (!Number.isSafeInteger(v)) throw new Error(`not an integer: ${t}`);
  return v;
}

// ---------------------------------------------------------------------------
// 1. project / bundle
// ---------------------------------------------------------------------------
$("rpOpenProject").addEventListener("click", async () => {
  const path = await pickPath({ title: "Open runner project", label: "project.stpg-project.json (or its folder)" });
  if (path) busyWhile($("rpOpenProject"), openProject(path));
});
$("rpOpenBundle").addEventListener("click", async () => {
  const path = await pickPath({ title: "Open build bundle", label: "Bundle folder (contains runner.bundle.json)", pickDir: true });
  if (path) busyWhile($("rpOpenBundle"), openBundle(path));
});

// While a request for a card is on the server: a spinner and the seconds in that card's body.
function pendingIn(body, text, promise) {
  const started = performance.now();
  const seconds = el("span", { class: "elapsed" }, "0 s");
  const box = el("div", { class: "preparing" }, el("span", { class: "spinner" }), text, " ", seconds);
  body.prepend(box);
  const timer = setInterval(() => { seconds.textContent = `${Math.floor((performance.now() - started) / 1000)} s`; }, 250);
  return Promise.resolve(promise).finally(() => { clearInterval(timer); box.remove(); });
}
$("rpNewProject").addEventListener("click", () => newProjectDialog());

// Finished build/verify/plan jobs belong to the project they ran for; a running one stays (it can be cancelled).
function forgetFinishedOfflineJobs() {
  for (const slot of ["build", "verify", "plan"]) if (R.jobs[slot] && R.jobs[slot].job.status !== "running") delete R.jobs[slot];
  R.plan = null;
}

async function openProject(path) {
  try {
    R.project = await pendingIn($("rpProjectBody"), "Opening the project…", post("/api/runner/project/open", { path }));
    forgetFinishedOfflineJobs();
    R.bundle = null;
    R.plan = null;
    setStatus(`project ${R.project.project_dir}`);
  } catch (e) { setStatus(errorText(e), true); R.project = { error: errorText(e), project_path: path }; }
  renderAll();
  loadResults();
}

async function openBundle(path, sha) {
  setStatus("Opening the bundle… (the first check of a bundle in this GUI session verifies it in full)");
  try {
    R.bundle = await pendingIn($("rpProjectBody"), "Opening and checking the bundle…",
      post("/api/runner/bundle/open", { path, manifest_sha256: sha || null }));
    R.plan = null;
    R.external.summary = null; R.external.use = false;  // checked against one bundle and STPG: check it again
    setStatus(`bundle ${short(R.bundle.manifest_sha256)} ${R.bundle.verification.status}`, R.bundle.verification.status !== "PASS");
  } catch (e) { setStatus(errorText(e), true); R.bundle = { error: errorText(e), bundle_dir: path }; }
  renderAll();
  loadResults();
}

function settingsView(settings, defaults = []) {
  const groups = [
    ["Input", "input"], ["Target", "target"], ["Compile", "compile"], ["Wait", "wait"],
    ["Segmentation", "segmentation"], ["Execution", "execution"], ["Capture", "capture"],
  ];
  const box = el("div", { class: "settings" });
  for (const [title, key] of groups) {
    const value = settings[key];
    if (!value) continue;
    const dl = el("dl");
    for (const [k, v] of Object.entries(value)) {
      let shown = Array.isArray(v) ? (v.length ? v.join(", ") : "[]") : v === null ? "null" : String(v);
      if (key === "wait" && k === "chunk_wait_us" && typeof v === "number") shown += ` (${usAsMs(v)} ms)`;
      dl.append(el("dt", {}, k), el("dd", { class: defaults.includes(`${key}.${k}`) ? "default" : "" }, shown));
    }
    box.append(el("section", {}, el("h3", {}, title), dl));
  }
  return box;
}

function renderProject() {
  const body = $("rpProjectBody");
  body.innerHTML = "";
  const P = R.project, B = R.bundle;
  if (!P && !B) {
    body.append(el("p", { class: "muted" }, "Open a portable project (project.stpg-project.json), a bundle built by stpg-runner build (CUI or GUI), or create a new project from a STIL file. Opening never converts or rebuilds anything."));
    return;
  }
  if (P) {
    if (P.error) body.append(notice("bad", "Project rejected by the SDK loader", [P.error]));
    else {
      const pre = P.preprocess;
      const rows = [["project", copyable(P.project_path)], ["inputs", Object.entries(P.inputs).map(([r, i]) => `${r}: ${i.path.split(/[\\/]/).pop()} (${short(i.sha256)})`).join(" · ") || "—"]];
      if (pre && pre.sources) {
        const src = pre.form === "ate-single" ? pre.sources.pattern.path.split(/[\\/]/).pop()
          : `${pre.sources.project.path.split(/[\\/]/).pop()} + ${pre.sources.pattern.path.split(/[\\/]/).pop()}`;
        rows.push(["preprocessed from", `${src}${pre.ignore_signals && pre.ignore_signals.length ? ` · ignored: ${pre.ignore_signals.join(", ")}` : ""}${pre.copied_from ? " (record copied from the original project)" : ""}`]);
      }
      body.append(kvList(rows));
      if (P.input_errors.length) body.append(notice("bad", "Inputs", P.input_errors));
      const blockers = P.copy_blockers || [];
      const editBlockers = P.edit_blockers || [];
      body.append(el("div", { class: "card-actions" },
        el("button", { class: "btn small", type: "button", id: "rpEditSettings", disabled: Boolean(editBlockers.length) || Boolean(P.input_errors.length),
          title: editBlockers.length ? `Not available: ${editBlockers.join("; ")}` : "Change the settings of this project in place (it has no successful build); the failed attempts stay listed below with the settings they used",
          onclick: () => editProjectDialog(P) }, "Edit settings…"),
        el("button", { class: "btn small", type: "button", disabled: Boolean(blockers.length) || Boolean(P.input_errors.length),
          title: blockers.length ? `Not available: ${blockers.join("; ")}` : "A new project folder with these settings and the same inputs; change what you need (for example segmentation)",
          onclick: () => newProjectDialog(projectPrefill(P)) }, "Copy to a new project…")));
      if (editBlockers.length) body.append(el("p", { class: "muted small-print" }, `Edit settings is not available: ${editBlockers.join("; ")}.`));
      body.append(settingsView(P.settings, P.defaults));
      if (!("mode" in P.settings.segmentation)) body.append(el("p", { class: "muted small-print" },
        `Segmentation has no mode: this project was made before automatic segmentation and keeps the old meaning (${P.settings.segmentation.max_vectors ? `segments only above ${P.settings.segmentation.max_vectors} vectors` : "always one package"}). Edit settings… (while it has no successful build) or Copy to a new project… stores an explicit mode.`));
      if (!("allow_approx" in P.settings.compile)) body.append(el("p", { class: "muted small-print" },
        "Timing: exact. The project file has no compile.allow_approx (made before the timing choice); that means exact ticks. Edit settings… keeps the file that way unless you choose Approx; a copy stores the choice explicitly."));
      if (P.defaults.length) body.append(el("p", { class: "muted small-print" }, "“(default)” marks values the file leaves out; the SDK fills them explicitly and they are part of the build identity."));
      if (P.reservations.length) {
        const rows = P.reservations.map((r) => {
          const kind = r.status === "HOST_ONLY_VERIFIED" ? "ok" : r.status === "FAILED" ? "bad" : "warn";
          const open = r.status === "HOST_ONLY_VERIFIED" ? el("button", { class: "btn small", type: "button", onclick: () => openBundle(r.path, r.manifest_sha256) }, "Open") : null;
          const used = r.settings ? el("span", { class: r.current_settings ? "" : "muted" }, `${settingsLine(r.settings)}${r.current_settings ? " (= current settings)" : ""}`) : "—";
          return [el("span", {}, badge(r.status, kind, r.error || "")), short(r.key), used, r.manifest_sha256 ? short(r.manifest_sha256) : "—", r.error || "", open];
        });
        body.append(el("h3", { class: "sub" }, "Build reservations"), table(["state", "cache key", "settings used", "manifest", "error", ""], rows));
        if (P.reservations.some((r) => r.status !== "HOST_ONLY_VERIFIED")) body.append(el("p", { class: "muted small-print" }, "Failed or incomplete reservations are kept as evidence and never overwritten; a build with the same inputs and settings rejects until you change a setting (Edit settings…) or an input."));
      }
    }
  }
  if (B) {
    if (B.error) { body.append(notice("bad", "Bundle", [B.error])); return; }
    const v = B.verification;
    if (R.project && R.project.derivation) body.append(notice("warn", "Project derived by the logic-analyzer tool", [
      "This project was made by the separately installed logic-analyzer tool (a marker pattern). Its settings belong to that tool; check its derivation there before running the bundle."]));
    if (v.status === "PASS" && B.role_pins && B.role_pins.length) body.append(notice("warn", "Pins with a special role in this bundle",
      B.role_pins.map((r) => `${r.signal}: role ${r.role}, bit ${r.bit}${r.logical_pin ? ` (${r.logical_pin})` : ""} — driven like any other pin`)));
    body.append(el("div", { class: "badges" }, el("strong", {}, "Bundle"), badge(v.status === "PASS" ? "verified (host only)" : "REJECT", v.status === "PASS" ? "ok" : "bad"),
      badge("not authorized for physical execution", "muted", "physical_execution_authorized is always false in SDK artifacts")));
    body.append(kvList([["folder", copyable(B.bundle_dir)], ["manifest", B.manifest_sha256 ? copyable(B.manifest_sha256, short(B.manifest_sha256)) : "—"],
      ["package", B.package_kind || "—"], ["tool", B.tool_sha256 ? short(B.tool_sha256) : "—"], ["python", B.python || "—"]]));
    if (v.errors && v.errors.length) body.append(notice("bad", "Verification errors", v.errors));
    const waitRejected = v.status !== "PASS" && (v.errors || []).find((e) => /wait plan rejected/.test(e));
    if (waitRejected) body.append(waitPlanNotice(waitRejected, "bundle"));
    if (v.status === "PASS" && B.rebound) {
      body.append(notice("warn", "Updated for this SDK without recompiling", [B.rebound.warning,
        "Its pattern files are the ones an older SDK compiled, unchanged; the installed SDK checked the whole bundle. Build the project again to include later compiler fixes."]));
      body.lastChild.id = "rpRebound";
    }
    if (v.status === "PASS" && B.sdk_matches === false) {
      const busy = Boolean(R.jobs.rebind && R.jobs.rebind.job.status === "running");
      body.append(notice("warn", "Built with another SDK version", [
        "Run refuses this bundle: the installed compiler/SDK differs from the one this bundle is bound to.",
        "Build (in its project) compiles it again with the installed SDK. Or update it without recompiling: the installed SDK checks it in full and writes a new bundle beside this one, which stays unchanged; later compiler fixes are then not included."]),
        el("div", { class: "card-actions" }, el("button", { class: "btn small", type: "button", id: "rpRebind", disabled: busy, onclick: rebindDialog }, "Update for this SDK without recompiling…")));
      body.lastChild.previousSibling.id = "rpSdkDiffers";
    }
    if (v.status === "PASS" && B.check) body.append(el("p", { class: "muted small-print", id: "rpBundleCheck" }, `Bundle check: ${bundleCheckText(B.check)}.`));
    if (B.timing) body.append(timingView(B.timing, B.package_kind));
    if (B.settings) body.append(settingsView(B.settings));
    if (B.patterns) {
      // PSRAM use can be constrained by bytes, events, samples, compares or an explicit vector cap.
      const region = B.psram_pattern_bytes;
      const share = (p) => `${(100 * p.expected.size_bytes / region).toFixed(p.expected.size_bytes * 1000 < region ? 2 : 1)} %`;
      body.append(table(["#", "pattern", "mode", "pins", "events", "samples", "compares", "bytes"].concat(region ? ["PSRAM region"] : []), B.patterns.map((p, i) => [i, p.path, p.expected.mode, p.expected.active_pins,
        p.expected.events.toLocaleString(), p.expected.samples.toLocaleString(), p.expected.compares.toLocaleString(), p.expected.size_bytes.toLocaleString()].concat(region ? [share(p)] : [])), [3, 4, 5, 6, 7, 8]));
      if (region) body.append(el("p", { class: "muted small-print", id: "rpPsramShare" }, `PSRAM region: the share of the ${region.toLocaleString()}-byte pattern region of the runtime profile that each pattern fills.`));
      body.append(el("div", { class: "card-actions" }, el("button", { class: "btn small", type: "button", onclick: showBundleInViewer }, "Show pattern in viewer")));
    }
  }
}

// The timing choice of a bundle and the rounding its compile reports record (the server's timing_summary).
function timingLine(T) {
  if (!T.policy) return "exact (built before the timing choice: no compile.policy.json)";
  if (!T.allow_approx) return "exact ticks";
  if (!T.records) return "approx allowed; no time needed rounding";
  return `approx: ${T.records.toLocaleString()} rounded time${T.records === 1 ? "" : "s"} (per vector) in ${T.group_count} group${T.group_count === 1 ? "" : "s"}, largest |Δ| ${T.max_abs_delta_ns} ns`;
}

function timingView(T, packageKind) {
  const box = el("div", { class: "timing", id: "rpTiming" });
  box.append(kvList([["timing", timingLine(T)]]));
  if (T.unreadable && T.unreadable.length) box.append(notice("bad", "Compile reports not readable", T.unreadable));
  if (!T.records) return box;
  const pkg = (i) => (packageKind === "segmented" ? `seg ${i}` : "package");
  box.append(table(["kind", "WaveformTable", "signal", "STIL time", "emitted", "Δ ns", "records", "first STIL line", "in"],
    T.groups.map((g) => [g.kind === "period_quantization" ? "period" : g.kind === "event_quantization" ? "edge" : g.kind,
      g.waveform_table || "—", g.signal || "—", g.original_time, `${g.emitted_time_ns} ns`, g.delta_ns, g.records.toLocaleString(),
      g.first_stil_line ?? "—", g.packages.length > 4 ? `${g.packages.length} segments` : g.packages.map(pkg).join(", ")]), [5, 6, 7]));
  if (T.group_count > T.groups.length) box.append(el("p", { class: "muted small-print" }, `${T.groups.length} of ${T.group_count} groups shown (largest |Δ| first); every record is in the packages' pattern.compile_report.json.`));
  box.append(el("p", { class: "muted small-print" },
    "These times were rounded to the target tick grid (Timing = Approx). The run and the bundle in the viewer use the emitted times; the STIL view shows the source times. "
    + "A rounded period changes every vector of its WaveformTable, so the difference adds up over vectors and Loop repeats. Compile success is not physical timing evidence."));
  return box;
}

async function showBundleInViewer() {
  if (!R.bundle || !R.bundle.patterns) return;
  try {
    const paths = R.bundle.patterns.map((p) => joinPath(R.bundle.bundle_dir, p.path));
    const result = await api("/api/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: paths.slice(0, 8), replace: false }) });
    setMode("view");
    await refreshState(result.loaded[0]);
  } catch (e) { setStatus(errorText(e), true); }
}

const SEGMENT_SUGGESTION = 100000;  // optional manual cap suggestion, not the auto planner's ceiling
const SERVICE_FIELDS = ["service_us", "service_source", "service_scope", "max_total_wait_us", "forbidden_stil_rates"];

// Whole microseconds as the milliseconds a person types (integer arithmetic: exact, no float formatting).
function usAsMs(us) {
  const frac = us % 1000;
  return `${Math.floor(us / 1000)}${frac ? `.${String(frac).padStart(3, "0").replace(/0+$/, "")}` : ""}`;
}

// A Custom chunk-wait length as the server reads it: a decimal text in ms, 1 µs steps, nothing rounded.
function parseWaitMs(text) {
  const m = /^([0-9]{1,9})(?:\.([0-9]{1,12}))?$/.exec(String(text).trim());
  if (!m) return { error: "type a positive number of milliseconds such as 6.5 or 0.001 (digits and one decimal point only)" };
  const frac = m[2] || "";
  if (/[1-9]/.test(frac.slice(3))) return { error: "finer than 1 µs (3 decimals); nothing is rounded" };
  const us = Number(m[1]) * 1000 + Number(frac.slice(0, 3).padEnd(3, "0"));
  return us < 1 ? { error: "it must be longer than 0 ms" } : { us };
}

// Settings of an open project as New-project form values (used by "Copy to a new project…").
function projectPrefill(P, extra = {}) {
  const s = P.settings, w = s.wait;
  return Object.assign({
    input_form: "stil", stil: P.inputs.stil ? P.inputs.stil.path : "", pin_map: P.inputs.pin_map ? P.inputs.pin_map.path : "",
    directory: `${P.project_dir}-copy`, wait_preset: w.preset, completion: s.execution.completion, storage: s.execution.storage,
    compile_mode: s.compile.mode, loop_policy: s.compile.loop_policy, allow_approx: String(s.compile.allow_approx === true),
    service_us: w.service_us ?? "", service_source: w.service_source || "", service_scope: w.service_scope || "",
    max_total_wait_us: w.max_total_wait_us ?? "", forbidden_stil_rates: (w.forbidden_stil_rates || []).join(", "),
    // Custom only: the exact stored length back as ms (never turned into a named preset)
    chunk_wait_ms: typeof w.chunk_wait_us === "number" ? usAsMs(w.chunk_wait_us) : "",
    // A project made before automatic segmentation has no mode: its copy gets the mode chosen in the dialog
    // (an explicit mode, so the old project itself is never reinterpreted).
    segmentation: s.segmentation.mode || (s.segmentation.max_vectors ? "auto" : "single"), max_segment_vectors: s.segmentation.max_vectors || "",
    legacy_segmentation: !("mode" in s.segmentation), copy_of: P.project_path, stil_loops: P.stil_loops,
    stil_has_loop: P.stil_has_loop,
  }, extra);
}

// One line of the settings a build reservation used (the server's settings_summary).
function settingsLine(s) {
  const wait = s.wait_preset && s.wait_preset !== "none"
    ? `${s.wait_preset}${typeof s.chunk_wait_us === "number" ? ` ${usAsMs(s.chunk_wait_us)} ms` : ""} ${s.service_scope || "?"} ${s.service_us ?? "?"}µs/${s.max_total_wait_us ?? "?"}µs` : "no wait";
  const seg = s.segmentation ? `seg ${s.segmentation}${s.max_vectors ? ` ≤${s.max_vectors}` : ""}` : `seg (no mode${s.max_vectors ? `, ${s.max_vectors}` : ""})`;
  return [s.compile_mode, s.loop_policy, s.allow_approx === true ? "quantized timing" : "exact timing",
    wait, seg, s.storage, s.completion].filter(Boolean).join(" · ");
}

function editProjectDialog(P, extra = {}) {
  newProjectDialog(projectPrefill(P, Object.assign({ edit_of: P.project_path, project_sha256: P.project_sha256, copy_of: "" }, extra)));
}

function labelled(label, control, hint = "") {
  const caption = el("span", {}, label), note = el("span", { class: "hint" }, hint);
  const box = el("label", { class: "field" }, caption, control, note);
  box.caption = caption; box.note = note;
  return box;
}

function newProjectDialog(prefill = {}) {
  const editing = Boolean(prefill.edit_of);
  formDialog(editing ? "Edit project settings" : prefill.copy_of ? "Copy to a new runner project" : "New runner project", (body) => {
    const S = R.state || {};
    const presets = S.wait_presets || ["none", "chunk-4ms", "chunk-12ms"];
    const presetUs = S.wait_preset_us || { none: 0, "chunk-4ms": 4000, "chunk-12ms": 12000 };
    const forms = Object.entries(S.input_forms || { stil: "STIL as is (toolchain form, or already preprocessed)",
      "ate-pair": "ATE generator: project file + pattern file (preprocessed at Create)",
      "ate-single": "ATE generator: one file with both (preprocessed at Create)" });
    const form = select("input_form", forms, prefill.input_form || "", "choose…");
    const stil = labelled("STIL input", pathInput("stil", prefill.stil || "", { title: "STIL file", label: "STIL" }));
    const ate = labelled("ATE project file", pathInput("ate_project", prefill.ate_project || "", { title: "ATE project file", label: "Project file (Signals/Timing)" }),
      "The file with Signals, SignalGroups and Timing that belongs to the pattern file above.");
    const ignore = labelled("Ignore signals (optional)", input("ignore_signals", prefill.ignore_signals || "", { placeholder: "comma-separated signal names" }),
      "Rewritten to T in the vectors and left out of a generated pin map (stpg-stil-preprocess --ignore-signal).");
    const pins = labelled("Pin map (optional)", pathInput("pin_map", prefill.pin_map || "", { title: "Pin map", label: "Pin map JSON" }));
    const dir = labelled("Project folder", pathInput("directory", prefill.directory || "", { title: "New project folder", label: "Folder to create", newDir: true }),
      "A new folder: Browse to the parent and type a new name. It is created when you press Create; an existing folder is never reused.");
    const syncForm = () => {
      const v = form.value;
      ate.hidden = v !== "ate-pair";
      ignore.hidden = !v || v === "stil";
      stil.caption.textContent = { "ate-pair": "ATE pattern file", "ate-single": "ATE file (project + pattern)" }[v] || "STIL input";
      stil.note.textContent = !v ? "Choose the input form first."
        : v === "stil" ? "Copied into the new project as inputs/pattern.stil. Tester event codes ('0ns' D;) need an ATE generator form."
        : "Preprocessed when you press Create (the code of stpg-stil-preprocess). The converted STIL becomes inputs/pattern.stil; preprocess.json records the original files.";
      pins.note.textContent = v && v !== "stil"
        ? "Empty: the preprocessor's default pin map is written (declaration order without the ignored signals), as stpg-stil-preprocess --pin-map does."
        : "Empty: the compiler maps the Signals in declaration order.";
    };
    form.addEventListener("change", syncForm);
    syncForm();
    const stilInput = stil.querySelector("input[name=stil]");
    // A new project: read the named STIL's Loop statements for the scope (Edit/Copy have them from the project).
    const checkLoops = async () => {
      const path = stilInput.value.trim();
      hasLoop = null; loops = null;
      syncScope();
      if (!path) return;
      try {
        const info = await post("/api/runner/stil_info", { path });
        if (stilInput.value.trim() !== path) return;
        hasLoop = info.has_loop; loops = info.loop_lines;
        syncScope();
      } catch (e) { /* a missing file is reported by Create */ }
    };

    const custom = S.custom_wait_preset || "chunk-custom";
    const maxUs = S.custom_wait_max_us || 21474836;
    const wait = select("wait_preset", (S.wait_modes || presets).map((m) => (m === custom ? [m, `${m} (type the length)`] : m)),
      prefill.wait_preset || "", "choose…");
    const waitMs = input("chunk_wait_ms", prefill.chunk_wait_ms ?? "", { placeholder: "e.g. 6.5", inputmode: "decimal" });
    const waitLength = labelled("Chunk wait length (ms)", waitMs, "");
    waitLength.id = "rpChunkWait";
    const syncLength = () => {
      waitLength.hidden = wait.value !== custom;
      const text = waitMs.value.trim();
      const parsed = text ? parseWaitMs(text) : null;
      const over = parsed && !parsed.error && parsed.us > maxUs;
      waitLength.note.className = parsed && (parsed.error || over) ? "hint bad-text" : "hint";
      waitLength.note.textContent = (!parsed ? "" : parsed.error ? `Not accepted: ${parsed.error}. `
        : over ? `${parsed.us.toLocaleString()} µs is more than ${usAsMs(maxUs)} ms, the most a 32-bit tick count holds at 200 MHz; the SDK rejects it. `
        : `= ${parsed.us.toLocaleString()} µs. `)
        + "The length of each inserted chunk wait, in ms with up to 3 decimals (1 µs steps); nothing is rounded. "
        + "It is not the service budget below and is not computed from it. Whether the plan fits (total wait limit, capacity, service model) is decided by Build.";
    };
    waitMs.addEventListener("input", syncLength);
    const waitAtVector = el("p", { class: "muted small-print", id: "rpWaitAtVector" },
      "A wait at a vector you choose is an edit of the STIL, not a preset: in the viewer select the vector, then Edit → Insert wait (on an edited copy; the source is not changed). "
      + "Remove it with × on its item in the edit list. Chunk waits are placed by Build and are not moved or removed one by one here.");
    const serviceUs = input("service_us", prefill.service_us ?? "", { placeholder: "e.g. 4000" });
    const totalUs = input("max_total_wait_us", prefill.max_total_wait_us ?? "", { placeholder: "e.g. 1000000" });
    const source = input("service_source", prefill.service_source || "", { placeholder: "where the budget comes from" });
    const scope = select("service_scope", ["non-loop", "loop-generic"], prefill.service_scope || "", "choose…");
    const scopeNote = el("p", { class: "muted small-print" });
    const service = el("div", { class: "form-grid", hidden: !(prefill.wait_preset && prefill.wait_preset !== "none") },
      el("div", { class: "field-row" }, field("Service budget (µs)", serviceUs), field("Scope", scope), field("Max total wait (µs)", totalUs)),
      scopeNote,
      field("Budget source (required)", source, "Where the service budget comes from. Filled in as “declared, not measured” — change it if you measured it. A planner input, not an inserted wait."),
      field("Forbidden WFTs", input("forbidden_stil_rates", prefill.forbidden_stil_rates || "", { placeholder: "comma-separated WaveformTable names" }), "Matched against the next chunk's first event: no chunk wait goes before such a chunk. It excludes a boundary; it does not move a wait."),
      el("p", { class: "muted small-print", id: "rpWaitWhere" }, "Build puts the chunk wait at each eligible chunk boundary: not at the first boundary of a segment, not before a chunk that starts with a forbidden WFT, "
        + "and not where the chunk before it already takes long enough by itself. So not at every vector or every chunk. "
        + "A plan the SDK rejects is never used (no zero-wait fallback), and no limit, forbidden WFT or service value is changed to make it fit."));
    for (const box of [serviceUs, totalUs, source]) box.addEventListener("input", () => { delete box.dataset.suggested; });
    scope.addEventListener("change", () => { delete scope.dataset.suggested; syncScope(); });
    const suggest = (box, value) => { if (!box.value || box.dataset.suggested) { box.value = value; box.dataset.suggested = "1"; } };
    const loop = select("loop_policy", ["preserve", "expand"], prefill.loop_policy || "preserve");
    // The build compares the scope with the compiled pattern: a pattern that keeps Loop blocks (loop policy
    // preserve on a STIL with Loop; a one-vector Loop counts too) needs loop-generic, any other non-loop.
    let loops = prefill.stil_loops;  // Loop statements in the project's STIL (Edit/Copy), unknown for a new STIL
    // The textual count is only a hint: comments, inline loops and unselected
    // patterns can fool it. Preserve alone does not prove selected Loop use.
    // No Loop keyword at all (outside comments) does prove it: no statement can keep a Loop block, so the
    // build needs non-loop whatever the loop policy (it rejects loop-generic).
    let hasLoop = prefill.stil_has_loop ?? null;
    const expectedScope = () => (loop.value === "expand" || hasLoop === false ? "non-loop" : null);
    const syncScope = () => {
      const expected = expectedScope();
      if (!service.hidden && expected) suggest(scope, expected);
      else if (!expected && scope.dataset.suggested) { scope.value = ""; delete scope.dataset.suggested; }  // you choose it for preserve
      const known = expectedScope();
      const where = hasLoop === false
        ? " This STIL has no Loop statement, so its compiled pattern keeps no Loop block: the build needs non-loop."
        : loops === undefined || loops === null ? "" : loops > 0
        ? ` The STIL text has ${loops.toLocaleString()} line${loops === 1 ? "" : "s"} starting with Loop (a text count: Loops in comments or in a Pattern that is not selected count too).`
        : " The STIL text has no line starting with Loop.";
      const mismatch = known && scope.value && scope.value !== known;
      scopeNote.className = mismatch ? "hint bad-text" : "muted small-print";
      scopeNote.textContent = (mismatch ? (loop.value === "expand"
        ? `With loop policy ${loop.value} the build expects scope ${known}; ${scope.value} is rejected. `
        : `This STIL has no Loop statement, so the build expects scope ${known}; ${scope.value} is rejected. `) : "")
        + "Scope is for the whole pattern: loop-generic when the compiled pattern keeps a Loop block in at least one segment, non-loop when it keeps none (always with loop policy expand). "
        + `With preserve it depends on the selected pattern, so you choose it.${where} A wrong scope is rejected by Build, which names the scope it found. `
        + "The service budget is declared, not measured, and has to hold for every segment, also the ones without a Loop.";
    };
    loop.addEventListener("change", syncScope);
    if (!editing) {
      stilInput.addEventListener("change", checkLoops);
      if (prefill.stil && hasLoop === null) checkLoops();
    }
    // Create/Save refuse a scope the build is certain to reject (see expectedScope).
    body._scopeProblem = () => {
      const known = expectedScope();
      if (service.hidden || !known || !scope.value || scope.value === known) return null;
      return loop.value === "expand"
        ? `Scope must be ${known} with loop policy expand (the compiled pattern keeps no Loop block); the build rejects ${scope.value}.`
        : `This STIL has no Loop statement, so the compiled pattern keeps no Loop block: choose Scope = ${known}. The build rejects ${scope.value}.`;
    };
    wait.addEventListener("change", () => {
      const on = Boolean(wait.value) && wait.value !== "none";
      service.hidden = !on;
      syncLength();
      if (!on) return;
      // Visible suggestions (the values of the CUI's example); edit them when you have better numbers.
      suggest(serviceUs, presetUs[wait.value] || 4000);
      suggest(totalUs, 1000000);
      suggest(source, "declared, not measured");
      syncScope();
    });
    syncScope();
    syncLength();

    // The SDK's two modes; a vector upper bound is optional in automatic mode ("split" is only an API alias).
    const seg = select("segmentation", [["auto", "automatic — one package when it fits, otherwise capacity-aware segments"],
      ["single", "one package only — Build rejects a pattern that does not fit"]], prefill.segmentation || "auto");
    const maxVectors = input("max_segment_vectors", prefill.max_segment_vectors || "", { placeholder: "blank = fill to capacity when needed" });
    const maxField = labelled("Max vectors / segment (optional)", maxVectors, `An upper bound, not a segment count. Blank: a fitting pattern stays one package; otherwise the SDK grows from ${SEGMENT_SUGGESTION} vectors to the first capacity limit (bytes, events, samples or compares), without a vector ceiling. An explicit number limits segment length and may split a fitting pattern too. Separate uploads/RUNs do not guarantee electrical continuity across segment boundaries.`);
    const syncSeg = () => { maxField.hidden = seg.value === "single"; };
    seg.addEventListener("change", syncSeg);
    syncSeg();

    const inputs = editing
      ? [kvList([["project", prefill.edit_of], ["STIL", prefill.stil ? prefill.stil.split(/[\\/]/).pop() : "—"], ["pin map", prefill.pin_map ? prefill.pin_map.split(/[\\/]/).pop() : "—"]]),
        el("p", { class: "muted small-print" }, "Only the settings below change; the inputs, the Pattern/PatternBurst selection and the target stay. For another STIL make a new project. The failed attempts stay in the project's build list with the settings they used; a build with the saved settings is a new attempt."),
        el("input", { type: "hidden", name: "project", value: prefill.edit_of }),
        el("input", { type: "hidden", name: "project_sha256", value: prefill.project_sha256 || "" })]
      : [field("Input form", form), stil, ate, ignore, pins, dir];
    const legacy = !prefill.legacy_segmentation ? null : el("p", { class: "hint" }, editing
      ? "This project was made before automatic segmentation (its segmentation has no mode). Saving stores the mode chosen here."
      : "The original project was made before automatic segmentation (its segmentation has no mode). This copy stores the mode chosen here; the original is not changed.");
    body.append(...inputs,
      el("div", { class: "field-row" }, field("Wait preset", wait), field("Completion", select("completion", [["hold", "hold — keep final levels"], ["release", "release — RESET(1) at the end"]], prefill.completion || "", "choose…")),
        field("Storage", select("storage", ["psram", "sram"], prefill.storage || "psram"))),
      waitLength, waitAtVector,
      el("div", { class: "field-row" }, field("Compile mode", select("compile_mode", ["auto", "rate-vector", "raw-event", "macro-stream"], prefill.compile_mode || "auto")),
        field("Loop policy", loop),
        field("Segmentation", seg)),
      field("Timing", select("allow_approx", [["false", "Exact ticks (default)"],
        ["true", "Approx — allow nearest-tick rounding"]], String(prefill.allow_approx ?? "false")),
        "Exact: every period and edge time must be a whole number of target ticks (5 ns at 200 MHz); Build rejects a STIL off that grid and lists the times. Approx: each such time is rounded to the nearest tick (ties up); the STIL is not changed, and the build result lists every rounded time. Rounding cannot repair streaming gaps, capacity or an edge at the period."),
      maxField, legacy, service,
      editing ? null : el("input", { type: "hidden", name: "copy_of", value: prefill.copy_of || "" }),
      el("p", { class: "muted small-print" }, (editing ? "" : "Preselected values are the CUI defaults of stpg-runner init (storage psram, compile mode auto, loop policy preserve, automatic segmentation); every value is shown and goes into the project file. ")
        + "Splitting means separate upload/RUN operations and per-segment comparisons; electrical continuity across boundaries is not proven. The SDK validates the project; SRAM needs raw-event + expand."));
  }, async () => {
    const data = Object.fromEntries(new FormData($("formDialogForm")).entries());
    const scopeProblem = $("formDialogBody")._scopeProblem && $("formDialogBody")._scopeProblem();
    if (scopeProblem) throw new Error(scopeProblem);
    // Custom only: the length goes as typed (the server reads it again); another preset sends none.
    if (data.wait_preset === ((R.state || {}).custom_wait_preset || "chunk-custom")) {
      if (!String(data.chunk_wait_ms || "").trim()) throw new Error("Type the Custom chunk wait length in milliseconds (for example 6.5).");
      const parsed = parseWaitMs(data.chunk_wait_ms);
      if (parsed.error) throw new Error(`Custom chunk wait length: ${parsed.error}.`);
    } else delete data.chunk_wait_ms;
    if (editing) {
      if (data.segmentation === "single") delete data.max_segment_vectors;
      if (!data.wait_preset || data.wait_preset === "none") SERVICE_FIELDS.forEach((key) => delete data[key]);
      else if (!String(data.service_source || "").trim()) throw new Error("Budget source is required with a chunk wait preset: say where the service budget comes from (for example “declared, not measured”).");
      R.project = await post("/api/runner/project/edit", data);
      R.bundle = null;
      forgetFinishedOfflineJobs();
      renderAll();
      setStatus("settings saved; Build makes a new attempt with them");
      return;
    }
    if (!data.input_form) throw new Error("Choose the input form: STIL as is, or ATE generator input to preprocess.");
    if (data.input_form !== "ate-pair") delete data.ate_project;
    if (!data.input_form || data.input_form === "stil") delete data.ignore_signals;
    if (data.segmentation === "single") delete data.max_segment_vectors;
    if (!data.wait_preset || data.wait_preset === "none") SERVICE_FIELDS.forEach((key) => delete data[key]);
    else if (!String(data.service_source || "").trim()) throw new Error("Budget source is required with a chunk wait preset: say where the service budget comes from (for example “declared, not measured”).");
    if (!data.copy_of) delete data.copy_of;
    const ok = $("formDialogOk");
    ok.textContent = data.input_form && data.input_form !== "stil" ? "Preprocessing…" : "Creating…";  // a large pattern takes a while
    let info;
    try { info = await post("/api/runner/project/init", data); } finally { ok.textContent = "Create"; }
    R.project = info; R.bundle = null;
    forgetFinishedOfflineJobs();
    renderAll();
    loadResults();
  }, { okText: editing ? "Save" : "Create" });
}

// ---------------------------------------------------------------------------
// jobs
// ---------------------------------------------------------------------------
function watchJob(slot, job, onDone) {
  const prev = R.jobs[slot];
  if (prev && prev.timer) clearTimeout(prev.timer);
  const entry = { job, events: job.events || [], next: job.next || 0, started: performance.now() - (job.elapsed_s || 0) * 1000, onDone };
  R.jobs[slot] = entry;
  const tick = async () => {
    try {
      // The server replaces a running stage's heartbeat in place: ask for the last event again and replace it.
      const since = Math.max(0, entry.next - 1);
      const update = await api(`/api/runner/job?${q({ id: job.id, since })}`);
      const fresh = update.events || [];
      if (entry.next > 0 && update.first === since && fresh.length && entry.events.length) entry.events.splice(-1, 1, ...fresh);
      else entry.events.push(...fresh);
      entry.next = update.next;
      entry.job = update;
      entry.started = performance.now() - (update.elapsed_s || 0) * 1000;  // server time, also after a reload
    } catch (e) { entry.job = Object.assign({}, entry.job, { status: "failed", error: errorText(e) }); }
    renderSlot(slot);
    if (entry.job.status === "running") entry.timer = setTimeout(tick, 400);
    else { entry.timer = null; if (entry.onDone) entry.onDone(entry.job); }
  };
  renderSlot(slot);
  renderActivity();
  entry.timer = setTimeout(tick, 250);
}

const JOB_LABELS = { build: "Build", verify: "Verify", plan: "Plan", run: "Run", control: "GPIO operation",
  controller_identify: "Controller identify", rebind: "Update for this SDK" };

// Running jobs for the top bar's activity (app.js renderActivity): the job and its current stage.
function runnerActivities() {
  const now = performance.now();
  return Object.entries(R.jobs).filter(([, e]) => e && e.job && e.job.status === "running").map(([slot, e]) => {
    const stages = stagesOf(e.events);
    const stage = stages.length ? stages[stages.length - 1].stage : "";
    return { label: `${JOB_LABELS[slot] || slot}${stage ? ` · ${STAGE_LABELS[stage] || stage}` : ""}`, since: Math.min(now, e.started) };
  });
}

function renderSlot(slot) {
  if (slot === "build" || slot === "verify" || slot === "plan" || slot === "rebind") renderBuild();
  else if (slot === "run") renderRun();
  else if (slot === "controller_identify") renderControllerIdentify();
  else if (slot === "control") renderControl();
}

function stagesOf(events) {
  const stages = new Map();
  const cycleSegments = new Map();
  let uploadCycle = 0;
  for (const e of events) {
    const t = e.job_s ?? e.elapsed_s;  // the job's clock when the server stamped it (SDK times start at the SDK call)
    if (e.stage === "upload" && e.event === "start") uploadCycle += 1;
    // A later upload must never inherit an earlier segment's accepted state,
    // detail or row position. This is a local occurrence key, not an inferred
    // segment number: a retained event prefix may start part-way through a run.
    const perUpload = ["upload", "run", "observe", "fail_log", "segment"].includes(e.stage);
    const key = e.stage === "port_check" ? `port_check:${e.detail}` : perUpload ? `${e.stage}:${uploadCycle}` : e.stage;
    const s = stages.get(key) || { stage: e.stage, state: "running", start: t, end: null, detail: "", completed: null, total: null, segmentIndex: null, cycle: perUpload ? uploadCycle : null };
    const segment = /segment=(\d+)/.exec(e.detail || "");
    if (segment) {
      s.segmentIndex = Number(segment[1]);  // only an explicit SDK event names the segment
      if (perUpload) cycleSegments.set(uploadCycle, s.segmentIndex);
    }
    if (e.event === "done") { s.state = "done"; s.end = t; }
    if (e.event === "progress" || e.event === "done" || e.event === "start") { if (e.completed !== null && e.completed !== undefined) s.completed = e.completed; if (e.total !== null && e.total !== undefined) s.total = e.total; }
    if (e.detail) s.detail = e.detail;
    s.last = t;
    s.lastEvent = e.event;
    stages.set(key, s);
  }
  return [...stages.values()].map(s => {
    // Legacy uploads have no acceptance detail; an observation in that same
    // upload cycle can still identify them, never the next journal's segment.
    if (s.segmentIndex === null && s.cycle !== null) s.segmentIndex = cycleSegments.get(s.cycle) ?? null;
    return s;
  });
}

function jobView(entry, { cancel, stages: showStages = true } = {}) {
  const job = entry.job;
  const box = el("div", { class: "form-grid" });
  const running = job.status === "running";
  const elapsed = running ? (performance.now() - entry.started) / 1000 : job.elapsed_s;
  const statusKind = { running: "info", done: "ok", failed: "bad", cancelled: "warn" }[job.status] || "muted";
  // A job that ended with a result is "done" only when the run completed; otherwise its badge names the outcome,
  // so that the end of the job is never read as a successful run.
  const ended = job.status === "done" && job.result && job.result.run ? job.result.run.outcome : null;
  const jobBadge = ended && ended !== "COMPLETED"
    ? badge(`finished · ${ended}`, ended === "CANCELLED" ? "warn" : "bad", `The job finished; the run's outcome is ${ended}.`)
    : badge(job.status, statusKind);
  box.append(el("div", { class: "badges" }, jobBadge, el("span", { class: "elapsed" }, `${elapsed.toFixed(1)} s`),
    el("span", { class: "muted" }, job.title), job.cancel_requested ? badge("cancel requested", "warn") : null,
    running && cancel ? el("button", { class: "btn small danger", type: "button", onclick: cancel }, "Cancel") : null));
  const list = el("ul", { class: "stages" });
  const stages = stagesOf(entry.events);
  // A run job is "done" when the SDK returned a result, also a REJECT: then its last stage is where it stopped.
  const outcome = job.result && job.result.run ? job.result.run.outcome : null;
  const succeeded = job.status === "done" && (!outcome || outcome === "COMPLETED");
  // The result journal can name a failed boundary even when acceptance events
  // are missing. Do not reuse another segment's event as proof for that boundary.
  const stop = !running && !succeeded && job.result ? job.result.stopped_at : null;
  // The SDK reports an accepted UPLOAD_END (upload / done) and an accepted RUN (run / accepted). Older event streams
  // or a run loaded without its events have neither; then the result file's journal fills in what was answered.
  const uploadStage = stop && stages.filter(s => s.stage === "upload" && s.state !== "done"
    && (s.segmentIndex === null || s.segmentIndex === stop.segment)).pop();
  if (uploadStage) uploadStage.answered = stop.upload_end;
  const runStage = stages.filter(s => s.stage === "run" && s.lastEvent === "accepted"
    && (!stop || s.segmentIndex === stop.segment)).pop();
  const segmentOf = (s) => { const m = /segment=(\d+)/.exec(s.detail || ""); return m ? ` (segment ${Number(m[1]) + 1})` : ""; };
  stages.forEach((s, i) => {
    const isLast = i === stages.length - 1;
    // Normally the final unresolved progress row is marked failed. When a
    // journal names the exact boundary, keep any earlier unresolved row neutral.
    if (s.end === null && !isLast) s.end = stages[i + 1].start;
    const accepted = s.stage === "run" && s.lastEvent === "accepted";  // RUN was answered and checked
    const state = s.state === "done" || s.answered || (accepted && !running) ? "done" : !isLast ? "done"
      : stop ? "recorded" : !running ? (succeeded ? "done" : "failed") : "running";
    if (s.end === null && !running) s.end = job.elapsed_s;
    const mark = state === "done" ? "✓" : state === "failed" ? "✗" : state === "recorded" ? "–" : "●";
    let text = STAGE_LABELS[s.stage] || s.stage;
    if (s.total) text += ` ${(s.completed || 0).toLocaleString()} / ${s.total.toLocaleString()}`;
    else if (s.completed) text += ` ${s.completed.toLocaleString()}`;
    if (accepted) text = `RUN accepted${segmentOf(s)}`;
    else if (s.stage === "fail_log") text = `retained fail log${segmentOf(s)}: ${(s.completed || 0).toLocaleString()} / ${(s.total || 0).toLocaleString()} entries received${state === "failed" ? " · reading stopped (see the result below)" : ""}`;
    else if (s.stage === "upload" && s.state === "done" && /UPLOAD_END accepted/.test(s.detail || "")) text += ` · UPLOAD_END accepted${segmentOf(s)}`;
    else if (s.detail && s.stage !== "fail_log") text += ` · ${s.stage === "port_check" ? PORT_CHECK_PHASES[s.detail] || s.detail : s.detail}`;
    if (s.lastEvent === "heartbeat" && state === "running") text += " (working; no percentage is measured)";
    if (s.answered) text += ` · UPLOAD_END answered ${s.answered} (from the result file)`;
    // The event clock does not bound an upload reconstructed from the journal.
    const dur = s.answered || state === "recorded" ? "" : s.end !== null ? `${(s.end - s.start).toFixed(1)} s` : running && isLast ? `${(elapsed - s.start).toFixed(1)} s…` : "";
    list.append(el("li", { class: state }, el("span", { class: "st" }, mark), el("span", {}, text), el("span", { class: "t" }, dur)));
    if (s.total && state === "running") {
      const bar = el("div", { class: "bar" }, el("i"));
      bar.firstChild.style.width = `${Math.min(100, 100 * (s.completed || 0) / s.total)}%`;
      list.append(el("li", {}, el("span"), bar, el("span")));
    }
  });
  if (stop) {
    const took = (v) => (typeof v === "number" ? (v < 1 ? `${(v * 1000).toFixed(1)} ms` : `${v.toFixed(2)} s`) : "");
    const row = (state, text, time) => list.append(el("li", { class: state }, el("span", { class: "st" }, state === "done" ? "✓" : "✗"),
      el("span", {}, `${text} (from the result file)`), el("span", { class: "t" }, time)));
    if (!uploadStage && !stages.some(s => s.stage === "upload" && s.state === "done" && s.segmentIndex === stop.segment)) row("done", `UPLOAD_END answered ${stop.upload_end}`, "");
    if (!runStage) row("done", `RUN answered ${stop.run}`, took(stop.run_took_s));
    const reply = stop.response_bytes ? `${stop.response_bytes} response bytes, not a complete reply` : "no reply";
    row("failed", `${stop.failed_request} after RUN · ${reply} (${stop.failure_stage})`, took(stop.failed_took_s));
  }
  if (!stop && !running && !succeeded && runStage && stages[stages.length - 1] === runStage) {
    // Failed after RUN was accepted, before any STATUS progress event: the result below says how (a reply that did not
    // come, a terminal ERROR, a stop). Nothing is concluded here from the missing event alone.
    list.append(el("li", { class: "recorded" }, el("span", { class: "st" }, "–"),
      el("span", {}, "after RUN accepted: how the run ended is in the result below"), el("span", { class: "t" }, "")));
  }
  if ((stages.length || stop) && showStages) box.append(list);
  if (job.error) box.append(notice("bad", "Error", [job.error]));
  if (job.device && job.error && !job.result) box.append(notice("warn", "Device contact: unknown", ["The error escaped after this device job started. Whether a port was opened or a byte was sent is not known — this is not a \"no I/O\" result."]));
  return box;
}

// ---------------------------------------------------------------------------
// 2. build / verify / plan
// ---------------------------------------------------------------------------
$("rpBuild").addEventListener("click", async () => {
  if (!R.project || R.project.error) return;
  try {
    const job = await post("/api/runner/build", { project: R.project.project_path });
    watchJob("build", job, async (done) => {
      if (done.status === "done" && done.result) {
        R.bundle = done.result.bundle;
        if (R.project) { try { R.project = await post("/api/runner/project/open", { path: R.project.project_path }); } catch (e) { /* keep */ } }
        renderAll();
      } else if (R.project) {
        try { R.project = await post("/api/runner/project/open", { path: R.project.project_path }); } catch (e) { /* keep */ }
        renderProject();
        renderBuild();
      }
    });
  } catch (e) { setStatus(errorText(e), true); }
});
$("rpVerify").addEventListener("click", async () => {
  if (!R.bundle) return;
  try { watchJob("verify", await post("/api/runner/verify", { bundle: R.bundle.bundle_dir, manifest_sha256: R.bundle.manifest_sha256 })); }
  catch (e) { setStatus(errorText(e), true); }
});
$("rpPlan").addEventListener("click", async () => {
  if (!R.bundle) return;
  try {
    watchJob("plan", await post("/api/runner/plan", { bundle: R.bundle.bundle_dir, manifest_sha256: R.bundle.manifest_sha256 }), (done) => {
      if (done.status === "done") R.plan = done.result;
      renderBuild();
    });
  } catch (e) { setStatus(errorText(e), true); }
});

function cancelJob(slot, message) {
  return async () => {
    const entry = R.jobs[slot];
    if (!entry) return;
    if (message && !confirm(message)) return;
    try { await post("/api/runner/job/cancel", { id: entry.job.id }); } catch (e) { setStatus(errorText(e), true); }
  };
}

// The SDK refused a chunk-wait plan (build error, or a bundle whose saved plan the installed SDK rejects). Nothing
// is adjusted for the user and a rejected partial plan is never run.
function waitPlanNotice(text, where) {
  const reason = /not executable: (.*)$/.exec(text || "");
  const why = reason ? ` (${reason[1]})` : "";
  const box = where === "build" ? notice("warn", "Wait plan rejected", [
    `The SDK rejected the chunk-wait plan${why}. A rejected plan stays in the build folder as a record and is never used: there is no zero-wait fallback.`,
    "Nothing was changed for you: not the wait length, the total wait limit, the forbidden WFTs or the service values. Change them yourself (Edit settings…, or Copy to a new project…), then Build again."])
    : notice("warn", "Wait plan rejected", [
      `This bundle carries a chunk-wait plan that the installed SDK rejects${why}: an older SDK kept a partial plan that is not executable.`,
      "Update for this SDK cannot repair it, and the bundle does not run. Check the project's wait settings and Build it again."]);
  box.id = where === "build" ? "rpWaitPlanRejected" : "rpBundleWaitPlan";
  return box;
}

function renderBuild() {
  const body = $("rpBuildBody");
  body.innerHTML = "";
  const canBuild = R.project && !R.project.error;
  const bundleOk = R.bundle && R.bundle.verification && R.bundle.verification.status === "PASS";
  const running = (slot) => Boolean(R.jobs[slot] && R.jobs[slot].job.status === "running");
  $("rpBuild").disabled = !canBuild || running("build");
  $("rpVerify").disabled = !R.bundle || !R.bundle.manifest_sha256 || running("verify");
  $("rpPlan").disabled = !bundleOk || running("plan");
  for (const [id, slot] of [["rpBuild", "build"], ["rpVerify", "verify"], ["rpPlan", "plan"]]) $(id).classList.toggle("running", running(slot));
  const build = R.jobs.build;
  if (build) {
    body.append(el("h3", { class: "sub" }, "Build"), jobView(build, { cancel: cancelJob("build", "Cancel the build? The SDK stops at the next stage boundary; the partial reservation is kept (FAILED).") }));
    const res = build.job.result;
    const failed = (build.job.status === "failed" || build.job.status === "cancelled") && R.project && !R.project.error;
    const actions = [];
    if (failed && !(R.project.edit_blockers || []).length && !R.project.input_errors.length) {
      actions.push(el("button", { class: "btn small", type: "button", id: "rpBuildEdit", onclick: () => editProjectDialog(R.project),
        title: "Change the settings of this project and build again; this attempt stays in the build list" }, "Edit settings…"));
    }
    const scopeFound = /service profile scope '([a-z-]+)' does not match (?:artifact|whole segmented sequence) scope '([a-z-]+)'/.exec(build.job.error || "");
    if (failed && scopeFound && scopeFound[1] !== scopeFound[2] && !(R.project.edit_blockers || []).length && !R.project.input_errors.length) {
      actions.push(el("button", { class: "btn small primary", type: "button", id: "rpBuildScope",
        onclick: () => editProjectDialog(R.project, { service_scope: scopeFound[2] }),
        title: "Edit settings with the scope the build found; press Save, then Build" }, `Use Scope = ${scopeFound[2]}…`));
    }
    if (failed && build.job.status === "failed" && /does not fit in one package/.test(build.job.error || "") && !(R.project.copy_blockers || []).length) {
      actions.push(el("button", { class: "btn small", type: "button",
        onclick: () => newProjectDialog(projectPrefill(R.project, { segmentation: "auto", max_segment_vectors: "" })) },
        "Copy to a new project with automatic segmentation…"));
    }
    if (actions.length) body.append(el("div", { class: "card-actions" }, ...actions));
    if (failed && /wait plan rejected/.test(build.job.error || "")) body.append(waitPlanNotice(build.job.error, "build"));
    if (res) body.append(kvList([["bundle", copyable(res.bundle_dir)], ["manifest", copyable(res.manifest_sha256, short(res.manifest_sha256))],
      ["package", res.package_kind === "segmented" ? `segmented: ${res.segment_count} segments (one upload/RUN each; compare results per segment)` : res.package_kind === "single" ? "one package" : "—"],
      ["cache hit", res.cache_hit ? "yes (fully re-verified)" : "no"], ["status", res.status],
      ...(res.bundle && res.bundle.timing ? [["timing", timingLine(res.bundle.timing)]] : [])]));
  }
  const verify = R.jobs.verify;
  if (verify) {
    body.append(el("h3", { class: "sub" }, "Verify"), jobView(verify));
    const v = verify.job.result;
    if (v) body.append(el("div", { class: "badges" }, badge(v.status === "PASS" ? "PASS (bundle integrity)" : "REJECT", v.status === "PASS" ? "ok" : "bad"), el("span", { class: "muted small-print" }, "Integrity and decoded counts only — not an execution or DUT result.")), ...(v.errors || []).map((x) => notice("bad", null, [x])));
  }
  const plan = R.jobs.plan;
  if (plan) {
    body.append(el("h3", { class: "sub" }, "Plan (offline)"), jobView(plan));
    if (R.plan) {
      body.append(table(["segment", "events", "samples", "compares", "bytes", "uploads", "run flags", "terminal"], R.plan.segments.map((s) => [s.index,
        s.expected.events.toLocaleString(), s.expected.samples.toLocaleString(), s.expected.compares.toLocaleString(), s.expected.size_bytes.toLocaleString(),
        s.payload_acks, `0x${s.run_args[0].toString(16)}`, s.terminal_state === 8 ? "HOLD" : "DONE"]), [1, 2, 3, 4, 5]));
      body.append(el("p", { class: "muted small-print" }, `Observation bound: ≤${R.plan.observation.max_status_per_segment} STATUS per segment, ${R.plan.observation.interval_s}s apart, ${R.plan.observation.deadline_s}s deadline${R.plan.observation.response_timeout_s ? `; after RUN each STATUS reply may take up to ${R.plan.observation.response_timeout_s}s (no retry)` : ""}. Sample payloads are not collected.${R.plan.fail_log_collection ? ` After a segment ends with compare failures, its retained fail log is read (up to ${R.plan.fail_log_collection.max_entries_per_segment} entries, ${R.plan.fail_log_collection.max_pages_per_segment} requests; while HOLD the held levels stay driven that much longer).` : ""}`));
    }
  }
  const rebind = R.jobs.rebind;
  if (rebind) {
    body.append(el("h3", { class: "sub" }, "Update for this SDK (no recompile)"), jobView(rebind));
    if (/wait plan rejected/.test(rebind.job.error || "")) body.append(waitPlanNotice(rebind.job.error, "bundle"));
    const res = rebind.job.result;
    if (res) body.append(kvList([["bundle", copyable(res.bundle_dir)], ["manifest", copyable(res.manifest_sha256, short(res.manifest_sha256))],
      ["result", res.no_op ? "already bound to the installed SDK: nothing was written" : "a new bundle beside the old one (unchanged); both were checked in full. Compiled with an older SDK; later compiler fixes are not included."]]));
  }
  if (!build && !verify && !plan && !rebind) body.append(el("p", { class: "muted" }, "Build runs the SDK in a background job and shows real elapsed time per stage; long compiler stages report heartbeats, not percentages."));
}

// ---------------------------------------------------------------------------
// local bindings (devices, controllers; analyzer identities are read only)
// ---------------------------------------------------------------------------
async function loadBindings() {
  try { R.configInfo = (await api("/api/runner/state")).config; } catch (e) { setStatus(errorText(e), true); }
  try {
    R.devices = (await api("/api/runner/bindings?kind=devices")).items;
    R.analyzers = (await api("/api/runner/bindings?kind=analyzers")).items;
    R.controllers = (await api("/api/runner/bindings?kind=controllers")).items;
  } catch (e) { setStatus(errorText(e), true); }
  if (R.device && !R.devices.some((d) => d.name === R.device)) R.device = "";
  if (R.controller && !R.controllers.some((d) => d.name === R.controller)) R.controller = "";
  if (R.runController && !R.controllers.some((d) => d.name === R.runController)) { R.runController = ""; R.gpioPlan = null; }
}

function renderBindings() {
  const body = $("rpBindingsBody");
  body.innerHTML = "";
  const devs = R.devices.map((d) => [d.name, d.config ? d.config.port : "—", d.config ? `${hex4(d.config.vid)}:${hex4(d.config.pid)}` : "—",
    d.config ? d.config.serial_number : (d.error || "invalid"), d.simulated ? badge("simulated", "warn") : d.error ? badge("invalid", "bad") : ""]);
  body.append(el("h3", { class: "sub" }, "STPG devices"), devs.length ? table(["name", "port", "vid:pid", "serial", ""], devs) : el("p", { class: "muted" }, "No device binding yet. A binding names one port, its exact USB identity and the expected firmware/capability; it is stored on this machine only."));
  const las = R.analyzers.map((a) => [a.name, a.config ? a.config.port : "—", a.config ? `${hex4(a.config.vid)}:${hex4(a.config.pid)}` : "—",
    a.config ? a.config.serial_number : (a.error || "invalid"), a.simulated ? badge("simulated", "warn") : a.error ? badge("invalid", "bad") : ""]);
  body.append(el("h3", { class: "sub" }, "Logic analyzers (USB identity only)"), las.length ? table(["name", "port", "vid:pid", "serial", ""], las)
    : el("p", { class: "muted" }, "No analyzer binding (optional). Analyzer bindings are written by the separately installed logic-analyzer tool (stpg-la); this GUI reads only their USB identity, as the target of Reset LA… and to keep it apart from the STPG and the controller. See “How to set up a logic analyzer” in the External accessory card."));
  const ctl = R.controllers.map((c) => [c.name, c.config ? c.config.port : "—", c.config ? c.config.protocol : (c.error || "invalid"),
    c.config ? (c.config.allow_high.length ? c.config.allow_high.map((n) => `GPIO${n}`).join(" ") : "none") : "—",
    c.simulated ? badge("simulated", "warn") : c.error ? badge("invalid", "bad") : ""]);
  body.append(el("h3", { class: "sub" }, "GPIO controllers"), ctl.length ? table(["name", "port", "protocol", "HIGH allowed", ""], ctl) : el("p", { class: "muted" }, "No controller binding (optional). Without one, nothing is enumerated, opened or reset."));
  body.append(settingsFolderView());
}

function settingsFolderView() {
  const c = R.configInfo || (R.state && R.state.config);
  const box = el("div", { id: "rpSettingsFolder" });
  if (!c) return box;
  box.append(el("p", { class: "muted small-print" }, "Settings folder: ", el("span", { class: "path" }, c.path), ` (${c.source_text}). ${c.text[0].toUpperCase()}${c.text.slice(1)}. Never written into a portable project or bundle.`));
  if (c.problem) box.append(notice("bad", "Settings folder problem", [c.problem, "No other folder is used instead. Fix it, or restart with --config-dir and a folder you can use."]));
  if (c.notes.length) box.append(notice("warn", "Settings folder", c.notes));
  if (c.state === "new" || c.state === "empty") box.append(el("p", { class: "muted small-print" }, "Have bindings from an earlier setup? Start with --config-dir and that folder, or set STPG_CONFIG_DIR. Nothing is moved or copied for you."));
  return box;
}

function hex4(v) { return (v ?? 0).toString(16).padStart(4, "0"); }

async function scanPortsInto(target, onPick) {
  target.innerHTML = "";
  target.append(el("p", { class: "muted small-print" }, "Scanning (metadata only, no port is opened)…"));
  try {
    const data = await post("/api/runner/ports", {});
    target.innerHTML = "";
    if (data.error) target.append(notice("warn", "Port listing", [data.error]));
    if (data.real_scan_performed === false) target.append(notice("warn", "No real port scan", ["Device execution is disabled (--no-device-run), so USB metadata is not listed; only demo entries are shown."]));
    const rows = data.ports.map((p) => [p.device, p.vid !== null && p.vid !== undefined ? `${hex4(p.vid)}:${hex4(p.pid)}` : "—", p.serial_number || "—", p.description || "",
      el("button", { class: "btn small", type: "button", onclick: () => onPick(p) }, "Use")]);
    target.append(rows.length ? table(["port", "vid:pid", "serial", "description", ""], rows) : el("p", { class: "muted" }, "No serial ports found."));
    target.append(el("p", { class: "muted small-print" }, "Nothing is selected automatically; pick the exact port yourself."));
  } catch (e) { target.innerHTML = ""; target.append(notice("bad", "Scan failed", [errorText(e)])); }
}

$("rpManageDevices").addEventListener("click", () => manageBindings("devices"));
$("rpManageAnalyzers").addEventListener("click", () => manageBindings("analyzers"));
$("rpManageControllers").addEventListener("click", () => manageBindings("controllers"));

const BINDING_TITLES = { devices: ["STPG device bindings", "STPG device"], analyzers: ["Logic-analyzer bindings (read only)", "analyzer"], controllers: ["GPIO controller bindings", "GPIO controller"] };
const BINDING_KINDS = { devices: "stpg_runner_device", controllers: "stpg_gpio_controller" };
function bindingItems(kind) { return { devices: R.devices, analyzers: R.analyzers, controllers: R.controllers }[kind]; }

function manageBindings(kind) {
  const items = bindingItems(kind);
  formDialog(BINDING_TITLES[kind][0], (body) => {
    const list = el("ul", { class: "list" });
    for (const item of items) {
      const out = el("div");
      const row = el("div", { class: "row" }, el("strong", {}, item.name), item.simulated ? badge("simulated (demo)", "warn") : null,
        item.error ? badge(item.error, "bad") : el("span", { class: "path muted" }, item.config ? item.config.port : ""),
        el("span", { class: "card-actions" },
          kind === "controllers" && !item.error ? el("button", { class: "btn small", type: "button", disabled: !(item.simulated || (R.state && R.state.device_run)),
            title: "Opens and closes the controller port and sends only the identify (rp2040-gpio-v1) or status (legacy-3) request of this binding's protocol; no pin command, no fallback. Not electrically read-only: USB open/close effects are not measured",
            onclick: () => identifyController(item.name, out) }, "Identify") : null,
          item.simulated || kind === "analyzers" ? null : el("button", { class: "btn small", type: "button", onclick: () => editBinding(kind, item) }, "Edit"),
          item.simulated || kind === "analyzers" ? null : el("button", { class: "btn small danger", type: "button", onclick: async () => {
            if (!confirm(`Delete the local binding ${item.name}?`)) return;
            try { await post("/api/runner/bindings", { kind, action: "delete", name: item.name }); await loadBindings(); renderAll(); manageBindings(kind); }
            catch (e) { formError(errorText(e)); }
          } }, "Delete")));
      list.append(el("li", {}, row, out));
    }
    body.append(items.length ? list : el("p", { class: "muted" }, "No bindings yet."),
      kind === "analyzers"
        ? el("p", { class: "muted small-print", id: "rpAnalyzerReadOnly" }, "Read only. Analyzer bindings are created, edited and identified on the page of the separately installed logic-analyzer tool (start it with this environment's Python and -m stpg_capture.ui.server; stpg-la is an alias in an activated environment). It is its own program and process. This GUI lists their USB identity only: the RUN target of Reset LA…, kept apart from the STPG and the controller.")
        : el("div", { class: "card-actions" }, el("button", { class: "btn small primary", type: "button", onclick: () => editBinding(kind, null) }, "Add…")));
  }, null);
}

function editBinding(kind, item) {
  const cfg = item && item.config ? item.config : {};
  formDialog(`${item ? "Edit" : "Add"} ${BINDING_TITLES[kind][1]} binding`, (body) => {
    const ports = el("div");
    const fields = {
      name: input("name", item ? item.name : "", { placeholder: kind === "controllers" ? "e.g. bench-gpio" : "e.g. bench-stpg", readonly: !!item }),
      port: input("port", cfg.port || "", { placeholder: "/dev/serial/by-id/… or COM5" }),
      vid: input("vid", cfg.vid !== undefined ? `0x${hex4(cfg.vid)}` : "", { placeholder: "0x2e8a" }),
      pid: input("pid", cfg.pid !== undefined ? `0x${hex4(cfg.pid)}` : "", { placeholder: "0x000a" }),
      serial: input("serial_number", cfg.serial_number || ""),
    };
    body.append(el("div", { class: "field-row two" }, field("Name", fields.name), field("Port", fields.port)),
      el("div", { class: "field-row" }, field("USB VID", fields.vid), field("USB PID", fields.pid), field("Serial number", fields.serial)),
      el("div", { class: "card-actions" }, el("button", { class: "btn small", type: "button", onclick: () => scanPortsInto(ports, (p) => {
        fields.port.value = p.device; fields.vid.value = p.vid !== null && p.vid !== undefined ? `0x${hex4(p.vid)}` : ""; fields.pid.value = p.pid !== null && p.pid !== undefined ? `0x${hex4(p.pid)}` : ""; fields.serial.value = p.serial_number || "";
      }) }, "Scan ports")), ports);
    if (kind === "devices") {
      const fw = input("firmware_version", cfg.firmware_version !== undefined ? `0x${cfg.firmware_version.toString(16)}` : "", { placeholder: "exact INFO firmware_version" });
      const cap = input("capability", cfg.capability !== undefined ? `0x${cfg.capability.toString(16)}` : "", { placeholder: "exact INFO capability" });
      const names = el("p", { class: "muted small-print" });
      const decode = async () => { try { const v = intValue(cap.value); names.textContent = v === null ? "" : (await api(`/api/runner/capabilities?${q({ value: v })}`)).names.join(" · "); } catch (e) { names.textContent = errorText(e); } };
      cap.addEventListener("input", decode);
      decode();
      body.append(el("div", { class: "field-row two" }, field("Firmware version", fw, "Must equal the device INFO reply; a mismatch rejects the run (no fallback)."), field("Capability", cap)), names,
        el("details", { class: "help", id: "rpDeviceHelp" }, el("summary", {}, "What are these two values?"),
          el("p", { class: "small-print" }, "They are what the STPG firmware reports about itself in its INFO reply, the first thing every run asks for. This binding holds what you expect; each run compares it with what the board reports, and a difference refuses the run before anything is uploaded."),
          el("p", { class: "small-print" }, "Firmware version: a 32-bit identity of the firmware build (not the USB device version or a Python package version). Capability: the bits of the features the firmware was built with; the names of the bits you type are shown above."),
          el("p", { class: "small-print" }, "Take both from the release of the firmware you installed. No firmware release with recorded values is published for this candidate yet: until then, use the values given with the firmware file you were provided. If a run reports other values, its result shows both side by side; nothing is copied into a binding for you."),
          el("p", { class: "muted small-print" }, "Full guide: manual/stpg-device.md in the STPG_RP2350 repository.")));
    } else {
      const pins = (R.state && R.state.controller && R.state.controller.pins) || [];
      const proto = select("protocol", [["rp2040-gpio-v1", "rp2040-gpio-v1 — updated RP2040 sketch (tagged ACKs, 12 GPIOs)"], ["legacy-3", "legacy-3 — old 3-channel RP2040/XIAO sketch (untagged)"]], cfg.protocol || "rp2040-gpio-v1");
      const high = el("div", { class: "pin-checks" });
      const legacy = input("legacy_pins", (cfg.legacy_pins || []).join(","), { placeholder: "legacy-3 only: the pin= numbers of CH0,CH1,CH2" });
      const roles = (R.state && R.state.controller && R.state.controller.roles) || {};
      const allowed = new Set(cfg.allow_high || []);
      const drawHigh = () => {
        high.innerHTML = "";
        const list = proto.value === "legacy-3" ? [0, 1, 2] : pins;
        for (const n of list) {
          const box = el("input", { type: "checkbox", name: `high_${n}`, checked: allowed.has(n) });
          box.addEventListener("change", () => { if (box.checked) allowed.add(n); else allowed.delete(n); });
          high.append(el("label", { class: "check", title: roles[String(n)] || "user-defined" }, box, ` ${proto.value === "legacy-3" ? "CH" : "GPIO"}${n}${n <= 1 ? " (RUN)" : ""}`));
        }
        legacy.disabled = proto.value !== "legacy-3";
      };
      proto.addEventListener("change", drawHigh);
      drawHigh();
      body.append(field("Protocol", proto, "Chosen explicitly: the host never probes, upgrades or falls back between protocols."),
        field("Pins allowed to drive HIGH", high, "Unticked pins accept only LOW and Hi-Z. GPIO0/1 (RUN) additionally need a separate per-operation override; a reset never uses HIGH."),
        field("Legacy pin map", legacy, "legacy-3: the three numbers the old sketch prints after pin= for CH0..CH2 in its status reply (board pin numbers, which can differ from the D labels). Actions still address CH0..CH2."),
        notice("warn", "Wiring roles (new RP2040 board)", ["GPIO0 = STPG RUN, GPIO1 = LA RUN: manual reset only, never touched by a run.", "GPIO2 = current bench FPGA enable: driven only by an explicitly listed action.", "GPIO3–8, 14, 15, 26: user-defined. The program cannot see what they are wired to."]));
      body._controllerHigh = allowed;
    }
    body.append(el("label", { class: "check" }, el("input", { type: "checkbox", name: "replace", checked: !!item }), " Replace an existing binding with this name"));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    const config = { kind: BINDING_KINDS[kind], schema_version: 1,
      port: f.get("port"), vid: intValue(f.get("vid")), pid: intValue(f.get("pid")), serial_number: f.get("serial_number") };
    if (kind === "devices") { config.firmware_version = intValue(f.get("firmware_version")); config.capability = intValue(f.get("capability")); }
    else {
      config.protocol = f.get("protocol");
      config.allow_high = [...$("formDialogBody")._controllerHigh].sort((a, b) => a - b);
      config.legacy_pins = config.protocol === "legacy-3" ? String(f.get("legacy_pins") || "").split(",").map((x) => x.trim()).filter(Boolean).map(Number) : [];
    }
    await post("/api/runner/bindings", { kind, action: "save", name: f.get("name"), config, replace: f.get("replace") === "on" });
    await loadBindings();
    renderAll();
  }, { okText: "Save" });
}

function identifyPortLifecycle(result) {
  const transport = result.transport;
  const journal = transport && transport.journal;
  const counts = transport && (transport.counters || transport);
  if (!Array.isArray(journal) || !counts) return "Port lifecycle evidence is missing; contact is unknown.";
  const opened = journal.some((item) => item.event === "open.end");
  const attempted = journal.some((item) => item.event === "open.begin");
  if (!attempted && !opened) return "No port-open attempt was recorded for this invocation.";
  if (!opened) return "Port open was attempted; success and effects are unknown.";
  return counts.close_successes === 1 ? "The port was opened; close was recorded successful."
    : "The port was opened; successful close is unproved.";
}

let controllerIdentifyTarget = null;
async function identifyController(name, target) {
  controllerIdentifyTarget = target;
  try { watchJob("controller_identify", await post("/api/runner/control/identify", { controller: name })); }
  catch (e) { target.innerHTML = ""; target.append(notice("bad", "Identify", [errorText(e)])); }
}
function renderControllerIdentify() {
  const entry = R.jobs.controller_identify, target = controllerIdentifyTarget;
  if (!entry || !target) return;
  target.innerHTML = "";
  target.append(jobView(entry));
  const r = entry.job.result;
  if (!r) return;
  const lines = [`protocol ${r.protocol} · sent ${r.sent.length ? r.sent.map((x) => JSON.stringify(x)).join(", ") : "nothing"} · pin commands written: ${r.pin_commands_written}`];
  const states = Object.entries(r.legacy_states || {});
  if (states.length) lines.push(`reported: ${states.map(([ch, st]) => `CH${ch} ${st}`).join(", ")} (software state, not measured)`);
  lines.push(identifyPortLifecycle(r));
  lines.push("A v1 Identify may change the session token; USB effects and the pin voltages are not measured.");
  lines.push(r.result_persisted ? `evidence saved: ${r.result_file}` : `evidence NOT saved${r.result_file ? ` (${r.result_file})` : ""}: treat this identify as incomplete`);
  if (r.status === "IDENTIFIED" && r.result_persisted) target.append(notice("ok", "The controller reply matches this binding.", lines));
  else if (r.status === "CANCELLED") target.append(notice("warn", "Identify cancelled", [r.error, ...lines]));
  else target.append(notice("bad", "Identify rejected", [r.error, ...(r.hint ? [r.hint] : []), ...lines]));
  if (r.received_lines && r.received_lines.length) target.append(el("pre", { class: "path small-print" }, r.received_lines.join("\n")));
}

// ---------------------------------------------------------------------------
// external accessory: a separately installed program, started by the SDK for one run (its own process)
// ---------------------------------------------------------------------------
function currentBundleOk() { return R.bundle && R.bundle.verification && R.bundle.verification.status === "PASS"; }

function externalState() {
  const X = R.external;
  if (!X.config) return ["not selected", "muted", "No accessory is selected: the next run uses the STPG, and a GPIO controller if you chose one."];
  if (X.error) return ["check failed", "bad", "This config cannot be used; the reason is below. Nothing was started."];
  if (!X.summary) return ["not checked", "warn", "Press Check. The run ignores this config until it is checked and ticked."];
  if (!X.use) return ["checked, not in use", "info", "Tick “use with the next run” to include it."];
  return ["used with the next run", "warn", "The next run starts this program. If it fails the run stops; nothing falls back to an STPG-only run."];
}

function laToolLine() {
  const tool = R.state && R.state.accessory_tool;
  if (!tool) return null;
  return el("p", { class: "small-print", id: "rpLaTool" }, tool.installed
    ? `Logic-analyzer tool: installed in this Python environment (${tool.distribution} ${tool.version}). It has its own page: use this environment's Python with -m stpg_capture.ui.server (or ${tool.command} in an activated environment).`
    : `Logic-analyzer tool: not found in this Python environment. It is a separate download, needed only for captures; STPG-only and GPIO runs work without it.`);
}

function laSetupGuide() {
  const c = R.configInfo || (R.state && R.state.config);
  const steps = [
    "Install the logic-analyzer tool (a separate download, GPL v3) into the same Python environment as this GUI, then restart this GUI.",
    "For a source install, put the matching source folders beside each other as STPG_RP2350 and STPG_RP2350_LA. From the core folder use the commands below. They name local sources, not a package on an index; build dependencies may need Internet access.",
    "Start the separate page with this environment's Python and -m stpg_capture.ui.server. The demo commands below disable device I/O. This GUI starts nothing for you.",
    `Check that its page shows the same settings folder as this GUI${c ? ` (${c.path})` : ""}. If not, start it with --config-dir and that folder.`,
    "On its page add the analyzer (port, USB identity, firmware limits, wiring) and press Identify. Analyzers are set up there, not here.",
    "Open the same bundle on its page and plan a capture around a vector.",
    "On its page, Use in an STPG run writes external.json in a new folder.",
    "Here, choose that external.json, press Check, tick “use with the next run” and press Run….",
    "Open the run's folder on its page to see the capture, its alignment and the waveform.",
  ];
  const guide = el("details", { class: "guide", id: "rpLaGuide" }, el("summary", {}, "How to set up a logic analyzer"),
    el("ol", {}, ...steps.map((step) => el("li", {}, step))),
    el("p", {}, "Windows PowerShell — from the core source folder; no activation or PATH change:"),
    el("pre", { id: "rpLaInstallWindows", class: "small-print" }, ".\\stpg-env\\Scripts\\python.exe -m pip install . ../STPG_RP2350_LA\n.\\stpg-env\\Scripts\\python.exe -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check"),
    el("p", {}, "Linux / macOS — from the same core source folder:"),
    el("pre", { id: "rpLaInstallPosix", class: "small-print" }, "./stpg-env/bin/python -m pip install . ../STPG_RP2350_LA\n./stpg-env/bin/python -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check"),
    el("p", {}, "For local wheel files, install both matching core and LA wheels in one command. See the source manual's Logic analyzer and developer installation pages. Remove the no-device/demo flags only after reviewing the real hardware setup."));
  guide.open = !!R.laGuideOpen;  // the card re-renders while a job runs: keep it open for the reader
  guide.addEventListener("toggle", () => { R.laGuideOpen = guide.open; });
  return guide;
}

function renderExternal() {
  const body = $("rpExternalBody");
  body.innerHTML = "";
  const X = R.external;
  const [stateLabel, stateKind, stateText] = externalState();
  body.append(el("div", { class: "badges", id: "rpExternalState" }, badge(stateLabel, stateKind), el("span", { class: "muted small-print" }, stateText)));
  body.append(el("p", { class: "muted small-print" }, "Optional. A separately installed program can take part in one run: it writes a local config file (stpg_external_accessory) naming its program, its device binding and its plan. "
    + "Choose that file here. The program runs as its own process; this page never loads its code, and its detailed results stay with that program."));
  const toolLine = laToolLine();
  if (toolLine) body.append(toolLine);
  body.append(laSetupGuide());
  const config = input("external_config", X.config);
  const choose = (value) => { X.config = String(value || "").trim(); X.summary = null; X.error = null; X.use = false; renderExternal(); renderRun(); };
  config.addEventListener("change", () => choose(config.value));
  const browse = el("button", { class: "btn small", type: "button", onclick: async () => {
    const chosen = await pickPath({ title: "External accessory config", label: "stpg_external_accessory JSON (written by the accessory program)", start: config.value });
    if (chosen) choose(chosen);
  } }, "Browse…");
  const use = el("input", { type: "checkbox", name: "external_use", checked: X.use, disabled: !X.summary });
  use.addEventListener("change", () => { X.use = use.checked; renderExternal(); renderRun(); });
  body.append(field("Accessory config", el("div", { class: "browse-bar" }, config, browse)),
    el("div", { class: "card-actions" },
      el("button", { class: "btn small", type: "button", id: "rpExternalCheck", disabled: !X.config || !currentBundleOk() || !R.device,
        title: "Reads the config and the files it names (hashes, USB identity, segment); starts nothing and enumerates nothing",
        onclick: () => busyWhile($("rpExternalCheck"), checkExternal()) }, "Check"),
      el("label", { class: "check" }, use, " use with the next run"),
      X.config ? el("button", { class: "btn small", type: "button", onclick: () => { R.external = { config: "", summary: null, error: null, use: false }; renderExternal(); renderRun(); } }, "Clear") : null));
  if (!currentBundleOk() || !R.device) body.append(el("p", { class: "muted small-print" }, "Open a verified bundle and choose the STPG device first: the config is checked against both."));
  if (X.error) body.append(notice("bad", "Accessory config", [X.error]));
  if (X.summary) {
    const x = X.summary;
    body.append(kvList([["accessory", x.identifier], ["program", el("span", { class: "path" }, x.command.join(" "))], ["mode", x.mode],
      ["segment", String(x.segment)], ["device", `${x.device.port} · ${hex4(x.device.vid)}:${hex4(x.device.pid)} serial ${x.device.serial_number}`],
      ["limits", `open ${x.open_timeout_s ?? x.timeout_s} s · arm/complete ${x.timeout_s} s · stdin EOF grace ${x.close_timeout_s ?? 1} s`]]),
      notice("warn", "Checked by this GUI", x.notes));
  }
}

async function checkExternal() {
  const X = R.external;
  X.error = null; X.summary = null; X.use = false;
  try {
    X.summary = await post("/api/runner/external/inspect", { bundle: R.bundle.bundle_dir, manifest_sha256: R.bundle.manifest_sha256, device: R.device, config: X.config });
  } catch (e) { X.error = errorText(e); }
  renderExternal(); renderRun();
}

function externalResultView(x) {
  const box = el("div", { class: "form-grid" });
  const kind = x.outcome === "COMPLETED" ? (x.adopted ? "info" : "warn") : x.outcome === "CANCELLED" ? "warn" : "bad";
  box.append(el("div", { class: "badges" }, el("strong", {}, x.identifier || "accessory"), badge(x.outcome, kind),
    x.outcome === "COMPLETED" ? badge(x.adopted ? "data adopted (the accessory's own judgement)" : "data not adopted", x.adopted ? "info" : "warn") : null,
    x.mode === "simulated" ? badge("SIMULATED", "warn") : null, x.cancel_requested ? badge("cancel requested", "warn") : null));
  box.append(el("p", { class: "muted small-print" }, "A neutral result reported by the accessory program. The core does not interpret its samples or algorithms; open its folder with that program for the details. Not a DUT PASS, firmware or waveform validation."));
  if (x.outcome === "INCOMPLETE") box.append(notice("warn", "Accessory not asked to collect", [
    `The invocation ended before the accessory's COMPLETE request (it is sent only after a valid DONE/HOLD of its segment, and only if nothing stopped the run before). See the STPG and GPIO controller sections for where it stopped.`]));
  if (x.errors && x.errors.length) box.append(notice("bad", "Accessory errors", [...new Set(x.errors)]));  // the session file keeps every entry
  if (x.unadmitted_stdout_bytes) box.append(notice("warn", "Unadmitted bytes on cancellation", [`${x.unadmitted_stdout_bytes} byte(s) from the accessory were not accepted as a reply in the cancelled exchange. This can include bytes buffered before the cancel; it does not establish their arrival time. session.json keeps the bounded received prefix.`]));
  if (x.outcome !== "COMPLETED" && x.hardware_effects_possible) box.append(notice("warn", "Accessory process", ["The program was started, so its device may have been opened. No cleanup command was sent; its stdin was closed once. Process exit does not prove that its device or pins were released."]));
  if (x.stderr_text) box.append(el("h3", { class: "sub" }, "Messages from the accessory (its own text, not checked by the core)"),
    el("pre", { class: "path small-print", id: "rpExternalStderr" }, x.stderr_text));
  box.append(kvList([["accessory folder", copyable(x.output_dir)], ["session record", copyable(x.session_file)]].concat(x.result_file ? [["result file", copyable(x.result_file)]] : [])));
  return box;
}









// ---------------------------------------------------------------------------
// 3. run
// ---------------------------------------------------------------------------
function renderRun() {
  const body = $("rpRunBody");
  body.innerHTML = "";
  const devices = R.devices.filter((d) => d.config);
  const dev = select("device", devices.map((d) => [d.name, d.simulated ? `${d.name} (demo, no hardware)` : `${d.name} · ${d.config.port}`]), R.device, "choose device…");
  dev.addEventListener("change", () => {
    R.device = dev.value;
    R.external.summary = null; R.external.use = false;  // a check is against one STPG binding
    renderExternal();
    renderRun();
  });
  body.append(field("Device", dev, R.state && !R.state.device_run ? "Real devices are disabled (--no-device-run)." : "Local binding: exact port + USB identity + firmware/capability."));
  if (R.external.use && R.external.summary) {
    const x = R.external.summary;
    body.append(notice("warn", `This run also starts the external accessory ${x.identifier}`, [
      `It is started before the run, answers READY after opening its own device, and is armed immediately before segment ${x.segment}'s RUN (ARMED). After a valid DONE/HOLD of that segment it collects and reports COMPLETE.`,
      "If it does not answer as required, the run is refused or stopped; nothing falls back to an STPG-only run.",
    ]));
  }
  if (currentBundleOk()) body.append(gpioActionsView());
  const running = R.jobs.run && R.jobs.run.job.status === "running";
  const prepare = R.runPrepare && R.runPrepare.key === runPrepareKey() ? R.runPrepare : null;  // this bundle and device only
  $("rpRun").disabled = !currentBundleOk() || !R.device || running || (R.state && R.state.active_device_job && !running)
    || Boolean(prepare && prepare.preparing);
  $("rpRun").classList.toggle("running", Boolean(running));
  if (prepare && prepare.preparing) {
    body.append(el("p", { class: "preparing", id: "rpRunPreparing" }, el("span", { class: "spinner" }),
      el("strong", {}, "Preparing the run… "), el("span", { id: "rpRunPreparingTime" }, preparingTime(prepare)),
      " — checking that the STPG port is in the USB port list (no port is opened), then the bundle (no device contact yet). The first check of a bundle in this GUI session verifies it in full, which takes a while for a large bundle; later ones re-hash its files and read its patterns."));
  }
  if (prepare && prepare.error) {
    const box = notice("bad", "The run could not be prepared — nothing was sent to the device", [prepare.error,
      ...(/installed compiler\/SDK differs from the bundle/.test(prepare.error) ? ["This bundle is bound to another SDK version. Build the project again, or use “Update for this SDK without recompiling…” in the Project card."] : [])]);
    box.id = "rpRunPrepareError";
    body.append(box);
  }
  $("rpCancel").hidden = !running;
  if (R.jobs.run) {
    // While running, one live view (segment k of N, the phase and its bar, the last update); the per-stage list
    // merges the stages of every segment, so it is shown only once the run has ended.
    body.append(jobView(R.jobs.run, { stages: !running }));
    if (running) {
      const view = runProgressView(R.jobs.run);
      body.append(view);
      if (R.runScrolledFor !== R.jobs.run.job.id) {  // the Run card is often below the fold: show the live view once
        R.runScrolledFor = R.jobs.run.job.id;
        view.scrollIntoView({ block: "center" });
      }
    } else if (R.runStatusFor !== R.jobs.run.job.id) {
      R.runStatusFor = R.jobs.run.job.id;
      const r = R.jobs.run.job.result && R.jobs.run.job.result.run;
      setStatus(`Run ${R.jobs.run.job.status}${r ? `: ${r.outcome}, ${r.completed_segments}/${r.planned_segments ?? "unknown"} segments` : ""}`, R.jobs.run.job.status !== "done");
    }
  }
  const result = R.jobs.run && R.jobs.run.job.result;
  if (result) body.append(runResultView(result));
  if (!currentBundleOk()) body.append(el("p", { class: "muted" }, "A verified bundle is required. Run is one explicit confirmation per invocation; nothing runs after a build, on reload or on reconnect."));
}

// What the run is doing now, from the job's events: the GUI's own run_verify (the hand-off to the SDK), the SDK's
// host preparation steps before it opens the port, then its upload / observe / segment events. Nothing here is
// measured on the device beyond what STATUS reported.
function runProgressState(events) {
  let planned = null, uploads = 0, finished = 0, last = null, poll = null, observedSince = null, acceptedSince = null;
  for (const e of events) {
    if (e.stage === "run_verify" && e.segments) planned = e.segments;
    if (e.stage === "upload" && e.event === "start") { uploads += 1; poll = null; observedSince = null; acceptedSince = null; }
    if (e.stage === "run" && e.event === "accepted") acceptedSince = e.job_s ?? null;  // RUN acknowledged, no STATUS yet
    if (e.stage === "segment" && e.event === "done") { finished = e.completed ?? finished + 1; if (e.total) planned = e.total; }
    if (e.stage === "observe") {
      if (e.detail) { const m = /poll=(\d+)/.exec(e.detail); if (m) poll = Number(m[1]); }
      if (observedSince === null) observedSince = e.job_s ?? null;  // first STATUS reply of this segment's RUN
    }
    last = e;
  }
  return { planned, current: Math.max(uploads, 1), finished, last, poll, observedSince, acceptedSince };
}

function runProgressView(entry) {
  const st = runProgressState(entry.events);
  const now = (performance.now() - entry.started) / 1000;
  const of = st.planned ? ` of ${st.planned}` : "";
  const e = st.last;
  let phase = "Starting…", short = "starting", bar = null, live = true;
  const count = (a, b) => `${(a || 0).toLocaleString()} / ${(b || 0).toLocaleString()}`;
  if (!e || ["run_verify", "workspace", "port_check", "bundle_verify", "bundle_check", "bundle_files", "verified_files", "shared_files", "timeline_files", "snapshot", "snapshot_verify", "snapshot_files", "capture_timeline", "capture_decode"].includes(e.stage)) {
    let step = e ? (STAGE_LABELS[e.stage] || e.stage) : "start SDK preparation";
    if (e && e.stage === "port_check" && PORT_CHECK_PHASES[e.detail]) step += ` ${PORT_CHECK_PHASES[e.detail]}`;
    if (e && e.stage === "bundle_check" && e.detail) step += ` · ${e.detail}`;
    phase = `Host preparation: ${step}. This is not pattern execution; the STPG serial port is not opened yet by the executor. A selected external accessory or GPIO controller is handled separately.`;
    short = `host preparation: ${step}`;
    if (e && e.total && e.completed !== null && e.completed !== undefined) {
      const unit = ["bundle_files", "snapshot_files", "verified_files", "shared_files", "timeline_files"].includes(e.stage) ? "files" : "segments";
      phase += ` This step: ${count(e.completed, e.total)} ${unit}.`;
      bar = e.completed / e.total;
    }
    live = false;
  } else if (e.stage === "device_preflight") {
    phase = "Opening the port and reading the device's INFO, clock and STATUS.";
    short = "opening the port, reading INFO / clock / STATUS";
  } else if (e.stage === "upload" && e.event === "done") {
    // UPLOAD_END was answered and checked; bytes sent alone are not acceptance
    const hooks = entry.accessories && (entry.accessories.controller || entry.accessories.external);
    phase = `Segment ${st.current}${of}: upload accepted (UPLOAD_END, ${count(e.completed, e.total)} bytes). `
      + (hooks ? "Next: the before-RUN step of the selected GPIO controller or accessory, if this segment has one, then RUN." : "Next: RUN.");
    short = `segment ${st.current}${of}: upload accepted`;
  } else if (e.stage === "run" && e.event === "accepted") {
    // RUN was acknowledged and checked. No STATUS has arrived yet: nothing about progress or the waveform is known.
    const t = st.acceptedSince !== null ? `${Math.max(0, now - st.acceptedSince).toFixed(1)} s` : "";
    const budget = entry.statusBudget ? `; each STATUS reply may take up to ${entry.statusBudget} s` : "";
    phase = `Segment ${st.current}${of}: RUN accepted; waiting for the first STATUS reply${t ? ` (${t} so far${budget})` : budget ? ` (${budget.slice(2)})` : ""}. No STATUS has arrived yet, so no progress has been reported either way and nothing about the waveform is known yet.`;
    short = `segment ${st.current}${of}: RUN accepted, waiting for the first STATUS`;
  } else if (e.stage === "upload") {
    phase = `Segment ${st.current}${of}: sending the pattern, ${count(e.completed, e.total)} bytes (not accepted until UPLOAD_END is answered)`;
    short = `segment ${st.current}${of}: upload ${count(e.completed, e.total)} bytes`;
    bar = e.total ? (e.completed || 0) / e.total : null;
  } else if (e.stage === "observe" && !(e.completed > 0)) {
    // A zero STATUS count is not a stall, nor proof that every firmware route
    // reports only at completion. Streaming paths can report queued work.
    const t = st.observedSince !== null ? ` · ${Math.max(0, now - st.observedSince).toFixed(1)} s in this segment` : "";
    phase = `Segment ${st.current}${of}: not finished yet (STATUS poll ${st.poll || 1}${t}). The device currently reports zero progress; no physical completion percentage can be inferred.`;
    short = `segment ${st.current}${of}: not finished, STATUS poll ${st.poll || 1}${t}`;
  } else if (e.stage === "observe") {
    phase = `Segment ${st.current}${of}: not finished yet — ${count(e.completed, e.total)} reported events (device STATUS${st.poll ? `, poll ${st.poll}` : ""}; not physical waveform completion)`;
    short = `segment ${st.current}${of}: not finished, ${count(e.completed, e.total)} events`;
    bar = e.total ? (e.completed || 0) / e.total : null;
  } else if (e.stage === "fail_log") {
    // the transfer of a mismatching segment's retained compare log: not RUN, waveform or STATUS progress
    const m = /segment=(\d+)/.exec(e.detail || "");
    const held = m && entry.holdSegments && entry.holdSegments.includes(Number(m[1]));
    const done = e.event === "done";
    const wait = entry.failLog && entry.failLog.response_timeout_s ? ` Each reply may take up to ${entry.failLog.response_timeout_s} s; nothing is retried.` : " Nothing is retried.";
    phase = `Segment ${st.current}${of} ended with compare failures. ${done ? "Its retained fail log was received" : "Reading its retained fail log"}: ${count(e.completed, e.total)} entries${done ? "." : " received and checked."} This is the transfer of the compare log, not RUN or waveform progress.`
      + (held ? " The segment ended in HOLD: its levels stay driven until this read ends and the next segment starts." : "") + (done ? "" : wait);
    short = `segment ${st.current}${of}: ${done ? "fail log received" : "reading the fail log"}, ${count(e.completed, e.total)} entries`;
    bar = !done && e.total ? (e.completed || 0) / e.total : null;
  } else if (e.stage === "segment") {
    phase = `Segment ${st.finished}${of} finished${st.planned && st.finished < st.planned ? "; preparing the next one" : ""}.`;
    short = `segment ${st.finished}${of} finished`;
  } else {
    phase = `${STAGE_LABELS[e.stage] || e.stage}${e.detail ? ` · ${e.detail}` : ""}`;
    short = STAGE_LABELS[e.stage] || e.stage;
  }
  const box = el("div", { class: "form-grid", id: "rpRunProgress" });
  box.append(el("p", { id: "rpRunPhase" }, phase));
  const meter = (fraction) => { const b = el("div", { class: "bar" }, el("i")); b.firstChild.style.width = `${Math.max(0, Math.min(100, 100 * fraction))}%`; return b; };
  if (bar !== null) box.append(meter(bar));
  if (st.planned && st.planned > 1) {
    box.append(el("p", { class: "muted", id: "rpRunSegments" }, `Segments finished: ${st.finished} / ${st.planned}`), meter(st.finished / st.planned));
  }
  const since = e && e.job_s !== undefined ? Math.max(0, now - e.job_s) : now;
  const quiet = live && since > 5;
  box.append(el("p", { class: `small-print ${quiet ? "warn-text" : "muted"}`, id: "rpRunLive" },
    live ? `Last report from the run ${since.toFixed(1)} s ago${quiet
      ? (e && e.stage === "run" && e.event === "accepted"
        ? ` — no STATUS reply yet after RUN${entry.statusBudget ? ` (the SDK waits up to ${entry.statusBudget} s for each reply)` : ""}. This note is information, not a timeout; nothing is retried.`
        : e && e.stage === "fail_log" && e.event !== "done"
        ? ` — no fail-log page for a while${entry.failLog && entry.failLog.response_timeout_s ? ` (the SDK waits up to ${entry.failLog.response_timeout_s} s for each reply)` : ""}. This note is information, not a timeout; nothing is retried.`
        : " — no report for a while: the host is waiting for the device (the SDK gives up on its own deadline). This note is information, not a timeout.")
      : " (a STATUS reply counts, even without a new count)."}`
      : `Host preparation for ${now.toFixed(1)} s; last host update ${since.toFixed(1)} s ago (not a device STATUS).`));
  setStatus(`Run: ${short} · ${live ? `last update ${since.toFixed(1)} s ago` : `${now.toFixed(1)} s`}`, quiet);
  return box;
}

// The last STPG exchanges of a run that did not complete, from the SDK's journal in run.json (names, byte counts
// and host times only), and what is and is not known when the serial I/O itself failed.
function exchangesView(x) {
  const took = (s) => s === null ? null : s < 1 ? `${(s * 1000).toFixed(1)} ms` : `${s.toFixed(2)} s`;
  const answer = (row) => row.answer ? (row.error ? `${row.answer} (rejected; see Errors)` : row.answer)
    : row.response_bytes ? `${row.response_bytes} response bytes (see Errors)` : "no complete response (see Errors)";
  const rows = x.rows.map((row) => [row.seq, row.request, row.sent_bytes === null ? null : `${row.sent_bytes.toLocaleString()} B`, answer(row), took(row.took_s)]);
  const parts = [el("p", { class: "muted small-print", id: "rpExchanges" }, `Last STPG exchanges (${x.rows.length} of ${x.total}, from the result file):`),
    table(["seq", "request", "sent", "answer", "time"], rows, [0, 2, 4])];
  if (x.link_lost) {
    const last = x.rows[x.rows.length - 1];
    parts.push(notice("warn", "USB link to the STPG", [
      x.last_write_completed ? "The host reported a full write of the last request. This does not prove target consumption or execution."
        : "A write was attempted, but its sent-byte count is unknown or incomplete. Some bytes may have reached the target.",
      `${last.response_bytes ?? "Unknown"} response bytes recorded; failing stage: ${last.failure_stage || "not recorded (older journal)"}.`,
      // only an error that carries Windows error 121 gets its definition; any other I/O error gets the same limits
      /\b121\b|semaphore|セマフォ/i.test(last.error || "")
        ? 'Windows error 121 ("semaphore timeout") reports a Windows API timeout; it does not identify a USB, driver or target root cause, or establish physical disconnection.'
        : "This host I/O error does not identify a USB, driver or target root cause, or establish physical disconnection.",
      "The SDK sends no further STPG request after the failed boundary. This does not release previously held outputs."
        + (x.run_write_attempted ? " A RUN write was attempted; outputs may still be driven."
          : " No new RUN write was attempted; previously held outputs may still be driven."),
      "Stop here and review the local evidence. Do not retry, reset or replug automatically; further device work needs separate approval and confirmed safe wiring.",
      ...(x.close_s !== null && x.close_s > 5 ? [`Closing the port took ${x.close_s.toFixed(1)} s.`] : [])]));
  }
  return parts;
}

// How far the current segment got before a request went unanswered (from run.json): only answered steps are named,
// and a completed run is never assumed. Nothing is retried, reset or asked again.
function stopPointNotice(x) {
  const segment = `Segment ${x.segment + 1} of ${x.planned_segments}`;
  const reply = x.response_bytes ? `${x.response_bytes} response bytes, not a complete reply` : "no reply";
  return notice("warn", "Where the run stopped (from the result file)", [
    `${segment}: UPLOAD_END was answered ${x.upload_end}: the upload was accepted.`,
    `RUN was answered ${x.run}: the STPG accepted it.`,
    `The first ${x.failed_request} after RUN got ${reply} (failing stage: ${x.failure_stage}).`,
    "Whether this segment ran to its end is not known: no terminal STATUS was observed for it. Outputs may still be driven.",
    "This record does not tell whether the cause is the firmware, the USB link or the host. After this failure, the runner did not retry or send a recovery reset."
  ]);
}

// The SDK's start check refused the run (from run.json): what the STPG reported, what a run needs, and that nothing
// was uploaded. Nothing here resets, retries or asks the STPG again.
function startCheckNotice(s) {
  const allowed = (s.allowed_states || []).join(" or ");
  const lines = [s.request === "INFO"
    ? `Before uploading anything, the SDK asks the STPG for INFO. A run starts only when the STPG reports ${allowed}; it reported ${s.state}${s.error_code ? ` with error code ${s.error_code}` : ""}.`
    : `The STPG's ${s.request} answer (${s.answer}${s.error_code ? `, error code ${s.error_code}` : ""}) did not match what the start check needs: state ${allowed} and no error.`,
    "Nothing was uploaded and no RUN was sent: the run stopped at this check."];
  if (s.state === "READY") lines.push("READY means a pattern was loaded (an upload finished or a flash slot was chosen) and has not been run since. "
    + "An earlier run that stopped after its upload and before RUN (for example at a GPIO controller step that failed) leaves the STPG like this.");
  lines.push("This program does not reset the STPG, run it anyway or try again. Find out what left it in this state first; a reset (Reset STPG… or the board's own reset) is your decision. Then press Run… again.");
  return notice("bad", "Refused at the STPG start check", lines);
}

// A finished record ends in an accepted UPLOAD_END. This is not a live device
// check, and earlier segments may already have run. Report only the saved reply.
function leftLoadedNotice(x, control) {
  const where = control && control.open_attempted && control.outcome !== "COMPLETED"
    ? "; the GPIO controller step before RUN did not complete (see GPIO controller below)" : "; see the errors below for why";
  const box = notice("warn", "Upload accepted; no subsequent STPG command recorded", [
    `The STPG accepted the upload of segment ${(x.segment ?? 0) + 1}${x.planned_segments ? ` of ${x.planned_segments}` : ""} (UPLOAD_END → OK · ${x.state}); no subsequent STPG command is recorded for this upload${where}.`,
    `The last recorded reply reported ${x.state}. If the STPG is still in that state, the next run is refused at its start check. This saved record is not a live device check and does not establish current state or pin levels.`,
    "This program does not reset the STPG or try again. A reset (Reset STPG… or the board's own reset) is your decision; find out first why the run stopped."]);
  box.id = "rpLeftLoaded";
  return box;
}

// A reply's firmware version or capability differed from the binding (from run.json): both values side by side.
// The binding is never changed for the user, and nothing is asked again.
function identityMismatchNotice(m) {
  const hex = (v) => (typeof v === "number" ? `0x${v.toString(16).padStart(8, "0")}` : "—");
  const rows = m.fields.map((f) => [f.name === "firmware_version" ? "firmware version" : "capability", hex(f.expected), hex(f.reported), f.expected === f.reported ? "same" : "differs"]);
  const lines = [m.uploaded_before ? "The STPG answered with another identity during the run; the SDK stopped there."
    : "Nothing was uploaded and no RUN was sent: the run stopped at the first reply."];
  if (m.only_expected.length) lines.push(`Only in this binding: ${m.only_expected.join(", ")}.`);
  if (m.only_reported.length) lines.push(`Only in what the STPG reported: ${m.only_reported.join(", ")}.`);
  lines.push("Check that this is the board you meant and that it runs the firmware you installed. Change the binding (Devices… → Edit) only if this firmware is the one you want to use; the program never copies reported values into a binding.");
  return el("div", {}, notice("bad", "The STPG's identity differs from this binding", lines),
    table(["", "this binding expects", "the STPG reported", ""], rows));
}

// One segment's retained fail log as the result file records it (runResultView's table).
function segmentFailLogText(f) {
  if (!f) return "—";  // older result: no fail-log record
  const of = `${(f.entries_read || 0).toLocaleString()} / ${(f.to_read ?? 0).toLocaleString()}`;
  return { NOT_NEEDED: "—", PENDING: "not read (the run stopped before)", READ: `${of} read${f.overflow ? " (STPG kept the first 341)" : ""}`,
    READ_ERROR: `stopped at ${of}`, NOT_COLLECTED_ERROR: "not read (ERROR)" }[f.status] || f.status;
}

// Reading a mismatching segment's retained fail log stopped (from run.json). The comparison stands; only what the
// journal records is named. Nothing is retried, reset or asked again.
function failLogStopNotice(x) {
  const segment = `Segment ${x.segment + 1}${x.planned_segments ? ` of ${x.planned_segments}` : ""}`;
  const how = x.answer ? `the STPG answered ${x.answer}, which the SDK did not accept`
    : x.failure_stage ? `${x.response_bytes ? `${x.response_bytes} of ${x.expected_bytes} bytes, not a complete reply` : "no reply"} (failing stage: ${x.failure_stage})` : null;
  return notice("warn", "Reading the retained fail log stopped (from the result file)", [
    `${segment} ended in ${x.terminal} and the STPG reported ${(x.fail_count || 0).toLocaleString()} compare failures: the comparison result MISMATCH stands.`,
    `Reading its retained fail log stopped after ${x.entries_read} of ${x.to_read} entries${x.request && how ? `: ${x.request}, ${how}` : how ? `: ${how}` : " (the reason is under Errors)"}.`,
    "The entries read before that are kept in the result file (STIL details lists them).",
    "After this the runner sent nothing more: no further segment, no release, no retry, no reset. "
      + (x.terminal === "HOLD_AWAITING_NEXT" ? "The segment ended in HOLD: its levels may still be driven." : "Outputs may still be driven.")]);
}

// STIL details of one result: the SDK's offline description (where each segment is in the STIL; which vector,
// line and signal failed). It is asked for explicitly (it verifies the bundle) and never contacts a device.
function describeButton(dir, where) {
  const d = R.description && R.description.dir === dir && R.description.where === where ? R.description : null;
  return el("div", { class: "card-actions" }, el("button", { class: "btn small", type: "button", "data-describe": dir, disabled: Boolean(d && d.pending),
    title: "Match this result with the STIL of its bundle (offline; the bundle is checked first)", onclick: () => describeResult(dir, where) },
    d && d.pending ? "Matching with the STIL…" : d ? "STIL details (again)" : "STIL details"));
}
// One STIL details at a time, in the card where it was asked for (its ids are unique on the page).
function descriptionOf(dir, where) {
  const d = R.description;
  return d && d.dir === dir && d.where === where && d.data ? [descriptionView(d.data)] : [];
}
async function describeResult(dir, where) {
  const asked = { dir, where, pending: true, data: null };
  R.description = asked;
  renderRun(); renderResults();
  let data;
  try { data = await post("/api/runner/result/describe", { result: dir, bundle: R.bundle && R.bundle.bundle_dir }); }
  catch (e) { data = { available: false, result: dir, reason: errorText(e) }; }
  if (R.description !== asked) return;  // another one was asked for meanwhile
  R.description = { dir, where, pending: false, data };
  renderRun(); renderResults();
  const shown = $("rpDescription");
  if (shown) shown.scrollIntoView({ block: "nearest" });
}
const FAIL_LOG_WORDS = { NOT_NEEDED: "not needed", PENDING: "not read: the run stopped before", READ: "read", READ_ERROR: "reading stopped",
  NOT_COLLECTED_ERROR: "not read (segment ended in ERROR)", UNAVAILABLE_LEGACY: "not in this result (older version)" };
function stilWhere(src) {
  return src ? `line ${src.stil_line} · vector ${src.global_vector_index.toLocaleString()}${src.pattern_name ? ` · ${src.pattern_name}` : ""}${src.label ? ` · ${src.label}` : ""}` : "—";
}
function failedSignals(f) {
  return f.signals.map((g) => `${g.name}: expected ${g.expected ? "H" : "L"}, got ${g.actual ? "H" : "L"}`).join("; ")
    + (f.unmapped_fail_bits ? `${f.signals.length ? "; " : ""}unmapped bits 0x${f.unmapped_fail_bits.toString(16)}` : "");
}
const FAILS_ON_SCREEN = 500;
function descriptionView(d) {
  const box = el("div", { class: "form-grid", id: "rpDescription" });
  box.append(el("h3", { class: "sub" }, "STIL details"));
  if (!d.available) { box.append(notice("warn", "STIL details are not available", [d.reason])); return box; }
  const x = d.description;
  if (x.rebind) box.append(notice("warn", "This bundle was updated without recompiling", [x.rebind.warning]));
  box.append(table(["segment", "STIL from", "STIL to", "comparison", "fails", "fail log"], x.segments.map((g) => [g.index, stilWhere(g.source_range.first), stilWhere(g.source_range.last),
    g.comparison || "—", g.fail_count.toLocaleString(), `${FAIL_LOG_WORDS[g.fail_log.status] || g.fail_log.status}${g.fail_log.entries_read ? `, ${g.fail_log.entries_read.toLocaleString()} entries` : ""}${g.fail_log.overflow ? " (the STPG keeps only the first 341)" : ""}`]), [0, 4]));
  const rows = [];
  for (const g of x.segments) g.fail_log.entries.forEach((f, i) => rows.push([g.index, i + 1, f.source.global_vector_index.toLocaleString(), f.source.stil_line, f.source.pattern_name || "", f.source.label || "", failedSignals(f)]));
  if (rows.length) {
    box.append(el("div", { id: "rpFailList" }, table(["segment", "#", "vector", "line", "pattern", "label", "failed signals"], rows.slice(0, FAILS_ON_SCREEN), [0, 1, 2, 3])));
    if (rows.length > FAILS_ON_SCREEN) box.append(el("p", { class: "muted small-print" }, `The first ${FAILS_ON_SCREEN} of ${rows.length.toLocaleString()} entries are listed here; Save as text lists up to 2,000 and the result file keeps them all.`));
  }
  box.append(el("p", { class: "muted small-print" }, "Vectors are zero-based STIL vector occurrences (Loop iterations counted); lines are one-based. They come from the SDK's mapping of the STPG's retained fail log: not a DUT pass, timing or physical check."));
  box.append(el("p", { class: "muted small-print", id: "rpDescriptionPrivacy" }, "Pattern, signal and label names may be confidential: check the text before you share it. It names no port, USB serial or folder of this computer."));
  box.append(el("div", { class: "card-actions" },
    el("button", { class: "btn small primary", type: "button", id: "rpShowFails", disabled: !rows.length, title: rows.length ? "Open the bundle's copy of the STIL in the viewer and mark these fails" : "No fail entries in this result", onclick: () => showFailsInViewer(d) }, "Show fails in the viewer"),
    el("button", { class: "btn small", type: "button", id: "rpSaveText", onclick: () => saveDescriptionDialog(d) }, "Save as text…"),
    el("button", { class: "btn small", type: "button", onclick: () => navigator.clipboard.writeText(d.text).then(() => setStatus("STIL details copied as text"), (e) => setStatus(`copy failed: ${errorText(e)}`, true)) }, "Copy text")));
  return box;
}
function saveDescriptionDialog(d) {
  formDialog("Save STIL details as text", (body) => {
    body.append(field("New file", pathInput("path", joinPath(resultsRoot() || "", `${d.name}-stil-details.txt`), { title: "Save as", label: "a new .txt file outside any bundle" }), "Never overwrites an existing file. The text is made again from the result and its bundle."));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    const r = await post("/api/runner/result/save_text", { result: d.result, bundle: d.bundle_dir, path: f.get("path") });
    setStatus(`saved ${r.path}`);
  }, { okText: "Save" });
}
// The fails of a result on the bundle's own STIL copy in the viewer. The viewer places a mark only where its vector
// at that index is on the STIL line the SDK named; it lists the others as not placed.
async function showFailsInViewer(d) {
  const marks = [];
  for (const g of d.description.segments) g.fail_log.entries.forEach((f, i) => marks.push({ vector: f.source.global_vector_index, line: f.source.stil_line,
    signals: f.signals.map((s) => s.name), text: `segment ${g.index} · fail ${i + 1} · ${stilWhere(f.source)} · ${failedSignals(f) || "no signal mapped"}` }));
  try {
    const opened = await post("/api/open", { paths: [d.stil], pin_map: d.pin_map, replace: false });
    const id = opened.loaded[0];
    const placed = await post("/api/marks", { trace: id, title: `fails of ${d.name}`, marks });
    setMode("view");
    await refreshState(id);
    setStatus(`${placed.placed} of ${placed.total} fails marked${placed.unplaced ? `; ${placed.unplaced} not placed (see Fails)` : ""}`, Boolean(placed.unplaced));
  } catch (e) { setStatus(errorText(e), true); }
}

// Update a bundle for the installed SDK without recompiling (the SDK's rebind_bundle): asked for explicitly.
function rebindDialog() {
  const B = R.bundle;
  formDialog("Update this bundle for the installed SDK", (body) => {
    body.append(notice("warn", "Compiled with an older SDK; later compiler fixes are not included.", [
      "Nothing is compiled. The installed SDK checks this bundle in full, copies its files unchanged into a new bundle beside it and checks that one in full too. No device is contacted.",
      "This bundle stays as it is. The new one records which SDK compiled the patterns (in the bundle and in the project log).",
      "If a check fails (for example a format the installed SDK no longer accepts), no usable bundle is made — a started folder is kept as a failed attempt — and the project needs Build."]));
  }, async () => {
    const job = await post("/api/runner/rebind", { bundle: B.bundle_dir, manifest_sha256: B.manifest_sha256 });
    watchJob("rebind", job, async (done) => {
      if (done.status === "done" && done.result) {
        R.bundle = done.result.bundle;
        if (R.project && !R.project.error) { try { R.project = await post("/api/runner/project/open", { path: R.project.project_path }); } catch (e) { /* keep */ } }
        renderAll();
        loadResults();
      } else renderBuild();
    });
  }, { okText: "Update" });
}

// A segment's terminal STATUS error code with its firmware name, for example "12 (ERR_TIMEOUT)".
const errorCodeText = (s) => s.terminal.error_code ? `${s.terminal.error_code}${s.error_name ? ` (${s.error_name})` : ""}` : "";

// A segment ended in DEVICE_ERROR (from run.json): the STPG answered STATUS and set the code itself. Only the code is
// named; where in the firmware it was set is not known from it. Nothing is retried, reset or asked again.
function deviceErrorNotice(segments) {
  const failed = segments.filter((s) => s.outcome === "DEVICE_ERROR");
  if (!failed.length) return null;
  const lines = failed.map((s) => `Segment ${s.index}: the STPG answered STATUS with state ERROR and error code ${errorCodeText(s)}.`);
  lines.push("The STPG set this code itself and its answer arrived; it is not this program's response deadline. A reply that does not arrive in time is shown under Last STPG exchanges as no complete response.");
  if (failed.some((s) => s.error_name === "ERR_TIMEOUT")) lines.push(
    "ERR_TIMEOUT is a timeout inside the firmware; the code alone does not say where it was set. Firmware without the timer-aware stream watchdog (CAP_STREAM_WAIT_WATCHDOG) can report it during a programmed pause of 250 ms or more in a streamed Rate Vector pattern; with this SDK such a run is refused before RESET on that firmware. See the manual: Runner → Response deadlines.");
  lines.push("This program does not reset the STPG or try again.");
  const box = notice("bad", "The STPG reported an error", lines);
  box.id = "rpDeviceError";
  return box;
}

function runResultView(res) {
  const r = res.run;
  const box = el("div", { class: "form-grid" });
  if (res.overall) box.append(el("div", { class: "badges" }, el("strong", {}, "Overall"),
    badge(`status ${res.overall.exit_code}`, { 0: "ok", 2: "bad", 3: "bad", 4: "warn", 130: "warn" }[res.overall.exit_code] || "muted"),
    el("span", { class: "muted small-print" }, `${res.overall.text} — same rule as the stpg-runner exit code. STPG, GPIO controller and external accessory are judged separately below.`)));
  box.append(el("h3", { class: "sub" }, "STPG"));
  const oKind = { COMPLETED: "info", DEVICE_ERROR: "bad", REJECT: "bad", CANCELLED: "warn" }[r.outcome] || "muted";
  const cKind = { MATCH: "ok", MISMATCH: "bad", NOT_APPLICABLE: "muted", UNOBSERVED: "warn" }[r.comparison] || "muted";
  const cText = {
    MATCH: "device reported 0 compare failures (STATUS counters only)", MISMATCH: "device reported compare failures",
    NOT_APPLICABLE: "the pattern has no compares — nothing was judged", UNOBSERVED: "acquisition incomplete — no comparison result",
  }[r.comparison] || "";
  box.append(el("div", { class: "badges" }, el("strong", {}, "Outcome"), badge(r.outcome, oKind), el("strong", {}, "Comparison"), badge(r.comparison, cKind),
    res.simulated ? badge("SIMULATED — no hardware", "warn") : null));
  box.append(el("p", { class: "muted small-print" }, `${cText}. Acquisition outcome and comparison are separate; neither is a DUT PASS. Firmware validation: not performed.`));
  box.append(table(["completed segments", "release requested", "release command attempted", "release acknowledged", "release sequence complete", "electrical release verified"],
    // without a plan (preparation did not complete) the planned count is unknown, not "null"
    [[`${r.completed_segments}/${r.planned_segments ?? "unknown"}`, !res.completion ? "unknown (preparation did not complete)" : res.completion === "release" ? "yes (completion=release)" : `no (completion=${res.completion})`,
      r.release_command_attempted ? "yes" : "no", r.release_acknowledged ? "yes" : "no", r.release_sequence_complete ? "yes" : "no", "no (not measured)"]]));
  if (res.segments && res.segments.length) {
    box.append(table(["segment", "outcome", "comparison", "events", "samples", "compares", "fails", "fail log", "error"], res.segments.map((s) => [s.index, s.outcome,
      s.comparison || "—", `${s.terminal.run_progress.toLocaleString()}/${s.expected.events.toLocaleString()}`, `${s.terminal.sample_count.toLocaleString()}/${s.expected.samples.toLocaleString()}`,
      s.expected.compares.toLocaleString(), s.terminal.fail_count.toLocaleString(), segmentFailLogText(s.fail_log), errorCodeText(s)]), [3, 4, 5, 6]));
    const deviceError = deviceErrorNotice(res.segments);
    if (deviceError) box.append(deviceError);
  }
  if (res.identity_mismatch) box.append(identityMismatchNotice(res.identity_mismatch));
  if (res.stopped_at) box.append(stopPointNotice(res.stopped_at));
  if (res.fail_log_stop) box.append(failLogStopNotice(res.fail_log_stop));
  if (res.start_check) box.append(startCheckNotice(res.start_check));
  if (res.left_loaded) box.append(leftLoadedNotice(res.left_loaded, res.control));
  if (r.errors && r.errors.length) box.append(notice("bad", "Errors", r.errors));
  if (res.exchanges && r.outcome !== "COMPLETED") box.append(...exchangesView(res.exchanges));
  if (r.stream_open_attempted && r.stream_opened === false) {
    // the open itself failed, so the journal has no exchange; only pyserial's Windows text for error 5 gets its meaning
    const denied = (r.errors || []).some((e) => /could not open port .*PermissionError\(13, .*, None, 5\)/.test(e));
    box.append(notice("warn", "Port open", [
      "The SDK's open of the STPG port failed, so this run wrote no STPG command. This does not release outputs held from an earlier run.",
      ...(denied ? ['Windows error 5 ("access denied") is what Windows returns while another handle has the COM port open, for example a serial terminal or another stpg-view server; it does not name the holder.'] : []),
      "The GUI does not retry the open; resolve the cause, then press Run… again."]));
  }
  if ((r.errors || []).some((e) => /selected port (missing\/duplicated|USB metadata does not match)/.test(e))) {
    const contact = r.stream_open_attempted === false
      ? "This invocation made no STPG serial open attempt; no CDC command was sent by the executor. This does not release previously held outputs."
      : "The error text alone does not prove no STPG contact: an accessory port check can fail after STPG upload. Consult the local lifecycle/transaction evidence; outputs may remain driven.";
    box.append(notice("warn", "Port check", [contact,
      "A selected port or exact USB VID/PID/serial did not match enumeration. Read the SDK's specific cause and local port snapshot; no alternate port, serial-case normalization or retry is used.",
      "Compare the binding with Devices… → Edit → Scan ports (metadata only), reconnect or fix it, then press Run… again; Run… checks the port before the bundle check."]));
  }
  if (r.outcome === "NOT_STARTED") {
    box.append(notice("warn", "STPG not started", [
      "The run stopped before the SDK started the STPG run (its result folder was never reserved), so this run opened no STPG port and sent no STPG command. This does not release outputs held from an earlier run.",
      "The external accessory or GPIO controller section below shows where it stopped."]));
  } else if (!r.result_persisted) box.append(notice("bad", "The final result was not persisted; the result file may hold only a prefix."));
  if (res.result_file) box.append(kvList([["result file", copyable(res.result_file)]]));
  if (res.result_dir && r.planned_segments) box.append(describeButton(res.result_dir, "run"), ...descriptionOf(res.result_dir, "run"));
  if (r.outcome === "CANCELLED") box.append(notice("warn", "Cancelled", ["No STOP or release was sent. Outputs may still be driven and the device may still be acquiring until its own run ends."]));
  if (res.control) box.append(el("h3", { class: "sub" }, "GPIO controller"), gpioResultView(res.control, { runAttached: true }));
  if (res.external) box.append(el("h3", { class: "sub" }, "External accessory"), externalResultView(res.external));
  return box;
}

$("rpRun").addEventListener("click", async () => {
  if (!currentBundleOk() || !R.device) return;
  const body = { bundle: R.bundle.bundle_dir, manifest_sha256: R.bundle.manifest_sha256, device: R.device };
  if (R.external.use) {
    if (!R.external.config) {
      const text = "the external accessory is selected but no config file is chosen (or untick \"use with the next run\")";
      R.runPrepare = { key: runPrepareKey(), preparing: false, error: text };
      setStatus(text, true);
      renderRun();
      return;
    }
    body.external = { config: R.external.config };
  }
  if (R.runController) {
    if (!R.gpioPlan || !R.gpioPlan.actions.length) {
      const text = "a selected GPIO controller needs at least one action (or choose \"no controller\")";
      R.runPrepare = { key: runPrepareKey(), preparing: false, error: text };
      setStatus(text, true);
      renderRun();
      return;
    }
    body.control = { controller: R.runController, plan: R.gpioPlan };
  }
  // The first preparation of a bundle verifies it in full (no device contact); a large bundle takes a while. Show
  // that it is under way with its elapsed time, and put a refusal in this card: the status bar alone is easy to miss.
  const key = runPrepareKey();
  R.runPrepare = { key, preparing: true, error: null, since: performance.now() };
  setStatus("Preparing the run…");
  renderRun();
  const note = $("rpRunPreparing");
  if (note) note.scrollIntoView({ block: "nearest" });  // the card body is often below the fold under the button
  const ticker = setInterval(() => { const t = $("rpRunPreparingTime"); if (t) t.textContent = preparingTime(R.runPrepare); }, 1000);
  let prep = null;
  try { prep = await post("/api/runner/run/prepare", body); setStatus(""); }
  catch (e) { R.runPrepare.error = errorText(e); setStatus(R.runPrepare.error, true); }
  finally { R.runPrepare.preparing = false; clearInterval(ticker); }
  if (prep) {
    try { showConfirm(prep); }
    catch (e) { R.runPrepare.error = `the confirmation could not be shown: ${errorText(e)}`; setStatus(R.runPrepare.error, true); }
  }
  renderRun();
});

function runPrepareKey() { return `${R.bundle ? R.bundle.manifest_sha256 : ""}|${R.device || ""}`; }
function preparingTime(prepare) { return `${Math.floor((performance.now() - prepare.since) / 1000)} s`; }

// How the SDK checked the bundle, as it reported (the server's "check"/"bundle_check": check_mode or the RunPlan's
// verification_mode): a full verification now, or every file re-hashed against its own earlier full verification
// of these bytes in this GUI process (a build, Open or run).
function bundleCheckText(check) {
  if (!check) return null;
  if (check.mode === "semantic") return `verified in full now (${check.seconds} s); later checks of these bytes in this GUI session re-hash every file against this verification`;
  if (check.mode === "process_receipt") return `every file re-hashed against the SDK's full verification of these bytes earlier in this GUI session (${check.seconds} s)`;
  return `${check.mode || "not reported"} (${check.seconds} s)`;
}

function showConfirm(prep) {
  const s = prep.summary;
  const box = $("confirmRunBody");
  box.innerHTML = "";
  box.append(kvList([["bundle", copyable(s.bundle_dir)], ["manifest sha256", copyable(s.manifest_sha256)], ["profile", s.runtime_profile], ["storage", s.storage],
    ["completion", s.completion_text], ["transport", s.transport], ["collection", s.collection], ["result folder", copyable(s.output_dir)]]
    .concat(s.bundle_check ? [["bundle check", `${bundleCheckText(s.bundle_check)}. After Start the SDK checks the bundle again on its own before it opens the port: a re-hash of every file while it still holds its verification of these bytes, otherwise in full (host preparation in the run's progress).`]] : [])));
  box.append(table(["segment", "events", "samples", "compares", "bytes", "pins", "run flags"], s.segments.map((g) => [g.index, g.events.toLocaleString(), g.samples.toLocaleString(),
    g.compares.toLocaleString(), g.size_bytes.toLocaleString(), g.active_pins, `0x${g.run_flags.toString(16)}`]), [1, 2, 3, 4, 5]));
  const d = s.device;
  box.append(kvList([["device", d.name], ["port", d.port], ["USB", `${hex4(d.vid)}:${hex4(d.pid)} serial ${d.serial_number}`], ["firmware", `0x${d.firmware_version.toString(16)}`],
    ["capability", `0x${d.capability.toString(16)} — ${d.capabilities.join(", ")}`]]));
  if (s.role_pins && s.role_pins.length) box.append(notice("bad", "Pins with a special role", [
    ...s.role_pins.map((r) => `${r.signal}: role ${r.role}, bit ${r.bit}${r.logical_pin ? ` (${r.logical_pin})` : ""}`),
    "This bundle's pin map gives these signals a non-signal role (for example a trigger marker added by another tool). The STPG drives them like any other pin: confirm that each one is spare and safe to drive on this bench."]));
  box.append(kvList([["accessories", `GPIO controller: ${s.accessories.controller || "none (no controller I/O)"} · external accessory: ${s.accessories.external || "none (no process is started)"}`]]));
  if (s.external) {
    const x = s.external;
    box.append(el("h3", { class: "sub" }, `External accessory ${x.identifier}`),
      kvList([["config", copyable(x.config)], ["program", el("span", { class: "path" }, x.command.join(" "))], ["program sha256", copyable(x.executable_sha256, short(x.executable_sha256))],
        ["binding", `${x.binding_file} (${short(x.binding_sha256)})`], ["plan", `${x.plan_file} (${short(x.plan_sha256)})`],
        ["device", `${x.device.port} · ${hex4(x.device.vid)}:${hex4(x.device.pid)} serial ${x.device.serial_number}`],
        ["segment", `${x.segment} (pattern ${short(x.target_pattern_sha256)})`], ["mode", x.mode],
        ["open limit", `${x.open_timeout_s ?? x.timeout_s} s`], ["arm/complete limit", `${x.timeout_s} s`],
        ["stdin EOF grace", `${x.close_timeout_s ?? 1} s`]]),
      notice("warn", "What the core checks, and what it does not", x.notes));
  }
  if (s.control) {
    const c = s.control;
    box.append(el("h3", { class: "sub" }, `GPIO controller ${c.controller}`),
      kvList([["port", c.port], ["USB", `${hex4(c.vid)}:${hex4(c.pid)} serial ${c.serial_number}`], ["protocol", c.protocol],
        ["HIGH allowed", c.allow_high.length ? c.allow_high.map((n) => `GPIO${n}`).join(", ") : "none"]].concat(c.protocol === "legacy-3" ? [["legacy pins", c.legacy_pins.join(", ")]] : [])),
      table(["#", "segment", "phase", "pin", "role", "state"], c.actions.map((a, i) => [i + 1, a.segment, a.phase === "before_run" ? "before RUN" : "after DONE/HOLD", a.physical, a.role,
        a.state === "HIGH" ? badge("HIGH (allow-listed)", "warn") : a.state]), [0, 1]),
      notice("warn", "GPIO actions", c.notes));
  }
  if (s.observation && s.observation.response_timeout_s) {
    box.append(el("p", { class: "muted small-print", id: "confirmStatusBudget" }, `After RUN is accepted, each STATUS reply may take up to ${s.observation.response_timeout_s} s; the first one can take several seconds. A missing reply stops the run there: nothing is retried or reset.`));
  }
  const f = s.fail_log_collection;
  if (f && s.segments.some((g) => g.compares > 0)) {
    const held = (s.hold_segments || []).length;
    box.append(notice("warn", "After compare failures: the retained fail log", [
      `When a segment ends (DONE or HOLD) with compare failures, the SDK reads that segment's retained fail log before the next segment${s.completion === "release" ? " or the release" : ""}: up to ${f.max_entries_per_segment} entries in up to ${f.max_pages_per_segment} requests of ${f.page_entries} entries.`,
      `Each reply may take up to ${f.response_timeout_s} s, so the reply waiting can add up to about ${Math.round(f.max_nominal_receive_budget_s)} s per segment (usually much less; this is not a measured time).`,
      (held ? `${held === 1 ? "One segment waits" : `${held} segments wait`} in HOLD for the next one: during the read the held levels stay driven that much longer before the next segment starts. `
        : "No segment of this plan waits in HOLD for a next one. ")
        + (s.completion === "release" ? "After the last segment, the release (RESET) comes that much later." : "After the last segment the final levels stay driven anyway (completion=hold)."),
      `A failed read stops the run there: no further segment${s.completion === "release" ? ", no release" : ""}, no retry, no reset. The compare result stays as the STPG reported it.`]));
    box.lastChild.id = "confirmFailLog";
  }
  box.append(notice("warn", "Limitations", s.limitations));
  const acks = $("confirmRunAcks");
  acks.innerHTML = "";
  for (const [key, text] of Object.entries(prep.acknowledgements)) acks.append(el("label", {}, el("input", { type: "checkbox", name: key }), text));
  const start = $("confirmRunStart");
  const update = () => { start.disabled = [...acks.querySelectorAll("input")].some((i) => !i.checked); };
  acks.querySelectorAll("input").forEach((i) => i.addEventListener("change", update));
  update();
  $("confirmRunError").hidden = true;
  $("confirmRunDialog")._prep = prep;
  $("confirmRunDialog").showModal();
}

$("confirmRunForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const dialog = $("confirmRunDialog");
  const prep = dialog._prep;
  if (!prep) return;
  dialog._prep = null;  // one POST per confirmation; a double click cannot start a second run
  $("confirmRunStart").disabled = true;
  const acknowledgements = {};
  $("confirmRunAcks").querySelectorAll("input").forEach((i) => { acknowledgements[i.name] = i.checked; });
  try {
    const job = await post("/api/runner/run/start", { nonce: prep.nonce, acknowledgements });
    dialog.close();
    watchJob("run", job, async (done) => {
      R.state = await api("/api/runner/state").catch(() => R.state);
      renderRun(); loadResults();
    });
    // what this run was confirmed with: whether a before-RUN step can follow the upload, and the STATUS budget
    R.jobs.run.accessories = (prep.summary && prep.summary.accessories) || null;
    R.jobs.run.statusBudget = prep.summary && prep.summary.observation ? prep.summary.observation.response_timeout_s : null;
    R.jobs.run.failLog = (prep.summary && prep.summary.fail_log_collection) || null;  // the read after a mismatch
    R.jobs.run.holdSegments = (prep.summary && prep.summary.hold_segments) || [];
    R.state = await api("/api/runner/state").catch(() => R.state);
    renderRun();
  } catch (err) { $("confirmRunError").textContent = errorText(err); $("confirmRunError").hidden = false; }
});

$("rpCancel").addEventListener("click", cancelJob("run", "Cancel this run?\n\nThe host stops sending requests and closes the port. No STOP or release is sent: outputs may stay driven and acquisition may continue. A cancelled run is not retried."));

// ---------------------------------------------------------------------------
// results
// ---------------------------------------------------------------------------
function resultsRoot() {
  if (R.project && R.project.results_dir) return R.project.results_dir;
  if (R.bundle && R.bundle.results_dir) return R.bundle.results_dir;
  return R.state ? R.state.results_dir : null;
}
async function loadResults() {
  if (!R.available) return;
  try { R.results = await api(`/api/runner/results?${q({ root: resultsRoot() })}`); } catch (e) { R.results = { error: errorText(e) }; }
  renderResults();
}
$("rpRefreshResults").addEventListener("click", loadResults);

function renderResults() {
  const body = $("rpResultsBody");
  body.innerHTML = "";
  const res = R.results;
  if (!res) { body.append(el("p", { class: "muted" }, "Results of runs and GPIO operations appear here.")); return; }
  if (res.error) { body.append(notice("bad", "Results", [res.error])); return; }
  body.append(kvList([["folder", copyable(res.root)]]));
  if (!res.items.length) { body.append(el("p", { class: "muted" }, "No results yet.")); return; }
  const list = el("ul", { class: "list" });
  for (const item of res.items.slice(0, 30)) {
    const run = item.run;
    const row = el("div", { class: "row" }, el("span", { class: "path" }, item.name),
      el("span", { class: "badges" },
        run ? badge(run.outcome, { COMPLETED: "info", DEVICE_ERROR: "bad", REJECT: "bad", CANCELLED: "warn" }[run.outcome] || "muted") : null,
        run ? badge(run.comparison, { MATCH: "ok", MISMATCH: "bad", NOT_APPLICABLE: "muted", UNOBSERVED: "warn" }[run.comparison] || "muted") : null,
        item.transport_kind === "injected" ? badge("simulated", "warn") : null,
        item.external ? badge(`${item.external.identifier || "accessory"} ${item.external.outcome}${item.external.outcome === "COMPLETED" ? (item.external.adopted ? " · adopted" : " · not adopted") : ""}`,
          item.external.outcome === "COMPLETED" && item.external.adopted ? "info" : "warn") : null,
        item.control ? badge(item.control.kind === "stpg_gpio_control_result" ? `manual GPIO${item.control.pin} ${item.control.pulse_ms ? "reset" : item.control.state} · ${item.control.outcome}` : `GPIO actions ${item.control.outcome}`,
          item.control.outcome === "COMPLETED" && item.control.result_persisted ? "info" : "bad") : null,
        item.external ? el("span", { class: "path muted small-print", title: "The accessory's detailed results are in this folder; open them with that program" }, item.external.path.replace(/[\\/]session\.json$/, "")) : null));
    list.append(el("li", {}, row, ...(run && run.planned_segments ? [describeButton(item.path, "results"), ...descriptionOf(item.path, "results")] : [])));
  }
  body.append(list);
}




// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
(function bootRunner() {
  api("/api/runner/state").then((st) => {
    R.state = st;
    R.available = true;
    if (st.demo) $("modeRun").append(el("span", { class: "dot", title: "demo mode" }));
    let mode = "view";
    try { mode = localStorage.getItem("stpg-view-mode") || "view"; } catch (e) { /* storage unavailable */ }
    if (mode === "run") setMode("run");
  }).catch(() => { $("modeRun").disabled = true; $("modeRun").title = "Runner unavailable on this server"; });
})();

// ---------------------------------------------------------------------------
// optional GPIO controller: run-attached actions (GPIO2+) and separate manual reset / set.
// Nothing here runs by itself: no enumeration, no reset before a run, no release in cleanup.
// ---------------------------------------------------------------------------
function controllerItem(name) { return R.controllers.find((c) => c.name === name && c.config); }
function segmentCount() { return R.plan ? R.plan.segments.length : (R.bundle && R.bundle.patterns ? R.bundle.patterns.length : 1); }
function runActionPins(cfg) { return cfg.protocol === "legacy-3" ? [2] : (R.state.controller.pins || []).filter((n) => n > 1); }  // never RUN (0/1)
function pinRole(n) { return (R.state && R.state.controller && R.state.controller.roles[String(n)]) || "user-defined"; }
function deviceJobActive() { return !!(R.state && R.state.active_device_job); }

function sortActions(actions) {
  // SDK order: segment, then before_run before after_terminal; listed order kept inside one boundary.
  return actions.map((a, i) => [a, i]).sort((x, y) => (x[0].segment - y[0].segment) || ((x[0].phase === "after_terminal") - (y[0].phase === "after_terminal")) || (x[1] - y[1])).map((x) => x[0]);
}

function gpioActionsView() {
  const box = el("div", { class: "form-grid gpio-actions" });
  const options = [["", "no controller (no GPIO I/O)"], ...R.controllers.filter((c) => c.config).map((c) => [c.name, c.simulated ? `${c.name} (demo)` : `${c.name} · ${c.config.port} · ${c.config.protocol}`])];
  const sel = select("run_controller", options, R.runController);
  sel.addEventListener("change", () => {
    R.runController = sel.value;
    if (!R.runController) R.gpioPlan = null;
    else if (!R.gpioPlan || R.gpioPlan.manifest_sha256 !== R.bundle.manifest_sha256) R.gpioPlan = { kind: "stpg_gpio_action_plan", schema_version: 1, manifest_sha256: R.bundle.manifest_sha256, actions: [] };
    renderRun();
  });
  box.append(field("GPIO actions during this run (optional)", sel, "Only GPIO2 and up, only at the listed segment boundaries. RUN (GPIO0/1) is never touched by a run."));
  if (!R.runController) return box;
  const cfg = controllerItem(R.runController);
  if (!cfg) { box.append(notice("bad", "Controller", ["the selected controller binding is missing or invalid; nothing falls back"])); return box; }
  if (R.gpioPlan && R.gpioPlan.manifest_sha256 !== R.bundle.manifest_sha256) R.gpioPlan = { kind: "stpg_gpio_action_plan", schema_version: 1, manifest_sha256: R.bundle.manifest_sha256, actions: [] };
  const actions = R.gpioPlan.actions;
  if (actions.length) {
    box.append(table(["#", "segment", "phase", "pin", "role", "state", ""], actions.map((a, i) => [i + 1, a.segment, a.phase === "before_run" ? "before RUN" : "after DONE/HOLD",
      cfg.config.protocol === "legacy-3" ? `CH${a.pin}` : `GPIO${a.pin}`, pinRole(a.pin), a.state,
      el("button", { class: "btn small", type: "button", onclick: () => { actions.splice(i, 1); renderRun(); } }, "Remove")]), [0, 1]));
  } else box.append(el("p", { class: "muted" }, "No action yet. A selected controller needs at least one action; choose \"no controller\" otherwise."));
  box.append(el("div", { class: "card-actions" },
    el("button", { class: "btn small", type: "button", onclick: () => addActionDialog(cfg.config) }, "Add action…"),
    el("button", { class: "btn small", type: "button", onclick: loadGpioPlanDialog }, "Load plan file…"),
    el("button", { class: "btn small", type: "button", disabled: !actions.length, onclick: () => savePlanDialog({ plan: R.gpioPlan }, "control-plan.json") }, "Save plan file…"),
    el("span", { class: "muted small-print" }, "Same file format as stpg-runner --control-plan; bound to this bundle's manifest SHA-256.")));
  if (R.external.use) box.append(el("p", { class: "muted small-print" }, "With the external accessory: at its segment the GPIO before-RUN actions run first, then the accessory is armed, then RUN."));
  return box;
}

function addActionDialog(cfg) {
  const allowHigh = new Set(cfg.allow_high);
  formDialog("Add a GPIO action", (body) => {
    const pins = runActionPins(cfg);
    const segs = [...Array(segmentCount()).keys()].map((n) => [String(n), `segment ${n}`]);
    const pin = select("pin", pins.map((n) => [String(n), `${cfg.protocol === "legacy-3" ? "CH" : "GPIO"}${n} — ${pinRole(n)}`]), String(pins[0]));
    const state = select("state", [], "");
    const drawStates = () => {
      state.innerHTML = "";
      state.append(el("option", { value: "", disabled: true, selected: true }, "choose a state…"));  // no implicit LOW
      for (const st of ["LOW", "Hi-Z", "HIGH"]) {
        if (st === "HIGH" && !allowHigh.has(Number(pin.value))) continue;
        state.append(el("option", { value: st }, st));
      }
    };
    pin.addEventListener("change", drawStates);
    drawStates();
    body.append(el("div", { class: "field-row" }, field("Segment", select("segment", segs, "0")), field("Phase", select("phase", PHASES, "before_run")), field("Pin", pin), field("State", state)),
      el("p", { class: "muted small-print" }, "HIGH is offered only for pins in this controller binding's allow-list. The level stays until another action or manual set changes it."));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    if (!f.get("state")) throw new Error("choose the GPIO state explicitly");
    R.gpioPlan.actions = sortActions([...R.gpioPlan.actions, { segment: Number(f.get("segment")), phase: f.get("phase"), pin: Number(f.get("pin")), state: f.get("state") }]);
    renderRun();
  }, { okText: "Add" });
}

function loadGpioPlanDialog() {
  formDialog("Load a GPIO action plan", (body) => {
    body.append(field("File", pathInput("path", "", { title: "control-plan.json", label: "stpg_gpio_action_plan JSON" }), "Validated now against this bundle and controller; it is validated again when the run is prepared and started."));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    const r = await post("/api/runner/gpio_plan/load", { path: f.get("path"), bundle: R.bundle.bundle_dir, manifest_sha256: R.bundle.manifest_sha256, controller: R.runController });
    R.gpioPlan = r.plan;
    renderRun();
  }, { okText: "Load" });
}


function savePlanDialog(what, name) {
  formDialog("Save plan file", (body) => {
    body.append(field("New file", pathInput("path", "", { title: name, label: "a new .json file outside any bundle" }), "Never overwrites an existing file."));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    const r = await post("/api/runner/save_plan", { ...what, path: f.get("path") });
    setStatus(`saved ${r.path} (sha256 ${short(r.sha256)})`);
  }, { okText: "Save" });
}

const IDENTITY_MEANING = {
  STARTUP: "a known start-up line (not an identification)",
  MATCH: "identification matched (nonce and the whole pin map)",
  REJECT: "not an accepted identification, start-up or legacy line: refused",
  REJECT_TRAILING_DATA: "the identification matched, but more data followed it: refused",
  LEGACY_STATUS_HEADER: "legacy status header",
  LEGACY_STATUS: "legacy channel status line",
};

// The identification lines the SDK had already read from the controller (control.json). Only what was received is
// shown, as text; why it was refused (another sketch, another device on the port, a reset message…) is not concluded.
function identityLinesView(c) {
  const box = el("div", { id: "rpControlLines" });
  const lines = c.identity_lines || [];
  if (!lines.length) {
    box.append(el("p", { class: "muted small-print", id: "rpControlNoLines" },
      "No received-line diagnostics in this record: it was written by an earlier version, or no complete text line was read "
      + "(a timeout or a non-ASCII reply is kept only as raw reads in control.json). The cause is not known from this record."));
    return box;
  }
  const total = c.identity_lines_total || lines.length;
  box.append(el("p", { class: "small-print" }, `Lines the controller sent during identification${total > lines.length ? ` (the last ${lines.length} of ${total})` : ""}, from control.json:`),
    table(["#", "line received", "the SDK's reading"], lines.map((x, i) => [total - lines.length + i + 1, el("code", {}, x.line), IDENTITY_MEANING[x.disposition] || x.disposition])),
    el("p", { class: "muted small-print" }, "This shows what was received, not why. No other request, retry or reset was sent. "
      + "Shown only here on this computer: a line can contain the controller's identity or token, and it is not added to STIL details or other shared text."));
  return box;
}

function gpioResultView(c, { runAttached }) {
  const box = el("div", { class: "form-grid" });
  const kind = c.outcome === "COMPLETED" ? "info" : c.outcome === "CANCELLED" ? "warn" : "bad";
  box.append(el("div", { class: "badges" }, el("strong", {}, "Controller"), badge(c.outcome, kind),
    c.not_started ? badge("not started", "warn") : null,
    badge(c.result_persisted ? "result saved" : "result NOT saved", c.result_persisted ? "muted" : "bad"),
    c.legacy_uncorrelated ? badge("legacy: untagged replies", "warn") : null, badge("voltage not measured", "muted")));
  for (const u of c.uncertain || []) box.append(notice("bad", "Level uncertain", [u]));
  if (c.requests && c.requests.length) {
    box.append(table(["#", "request", "written bytes", "software ACK"], c.requests.map((r, i) => [i + 1, `GPIO${r.pin} ${r.state}`,
      r.written_bytes === null || r.written_bytes === undefined ? (r.error ? "unknown (write failed; not resent)" : "—") : r.written_bytes,
      r.acknowledged ? "acknowledged" : "none"]), [0, 2]));
  } else if (c.not_started) {
    box.append(el("p", { class: "small-print", id: "rpControlNotStarted" }, `Not started: ${c.not_started}. No GPIO request was sent`
      + (c.open_attempted === false ? " and the controller's port was not opened: the controller was not contacted, so this says nothing about it." : ".")));
  } else box.append(el("p", { class: "muted small-print" }, runAttached ? "No GPIO request was sent (actions of unreached segments are not executed)." : "No GPIO request was sent."));
  if (runAttached && c.planned_actions) box.append(el("p", { class: "muted small-print" }, `${(c.acknowledged_actions || []).length} of ${c.planned_actions.length} planned actions acknowledged.`));
  if (c.open_attempted && c.outcome !== "COMPLETED") box.append(identityLinesView(c));
  const last = Object.entries(c.last_acknowledged_states || {}).map(([k, v]) => `GPIO${k}=${v}`).join(" · ");
  box.append(kvList([["last acknowledged states", last || "—"], ["port closes", `${c.close_successes ?? 0} of ${c.close_attempts ?? 0} attempted`],
    ["result file", c.result_file ? copyable(c.result_file) : "—"]]));
  if (c.errors && c.errors.length) box.append(notice("bad", "Errors", c.errors));
  box.append(el("p", { class: "muted small-print" }, "ACK = the controller's reported software state. A closed port, Hi-Z request or ACK is not a measured release, voltage or target state. Nothing is restored automatically."));
  return box;
}

function renderControl() {
  const body = $("rpControlBody");
  if (!body) return;
  body.innerHTML = "";
  const options = [["", "none"], ...R.controllers.filter((c) => c.config).map((c) => [c.name, c.simulated ? `${c.name} (demo)` : `${c.name} · ${c.config.port}`])];
  const sel = select("controller", options, R.controller);
  sel.addEventListener("change", () => { R.controller = sel.value; renderControl(); });
  body.append(field("Controller", sel, "Separate explicit operations. Nothing here runs before or after a pattern, on error, or by itself."));
  const entry = R.jobs.control;
  const running = entry && entry.job.status === "running";
  const off = !R.controller || running || deviceJobActive() || !(R.state && (R.state.device_run || R.controller === "simulated"));
  ["rpResetStpg", "rpResetLa", "rpSetGpio"].forEach((id) => { $(id).disabled = off; });
  $("rpControlCancel").hidden = !running;
  if (deviceJobActive() && !running) body.append(el("p", { class: "muted small-print" }, "A device job is running; reset/set wait until it ends (one device job at a time)."));
  if (R.controller && controllerItem(R.controller)) {
    const c = controllerItem(R.controller).config;
    body.append(el("p", { class: "muted small-print" }, `${c.protocol} · HIGH allowed: ${c.allow_high.length ? c.allow_high.map((n) => `GPIO${n}`).join(", ") : "none"}. GPIO0 = STPG RUN, GPIO1 = LA RUN (wired on the planned PCB; disconnected on the current debug board).`));
  }
  if (entry) body.append(jobView(entry));
  const res = entry && entry.job.result;
  if (res) body.append(gpioResultView(res.control, { runAttached: false }));
}

function bindingOptions(items) { return items.filter((d) => d.config).map((d) => [d.name, d.simulated ? `${d.name} (demo)` : `${d.name} · ${d.config.port} · serial ${d.config.serial_number}`]); }

function resetDialog(target) {
  const isStpg = target === "stpg";
  formDialog(isStpg ? "Reset the STPG (GPIO0 = STPG RUN)" : "Reset the logic analyzer (GPIO1 = LA RUN)", (body) => {
    body.append(notice("warn", "What this does", [`Sets controller GPIO${isStpg ? 0 : 1} LOW, waits the declared time, then Hi-Z. Nothing else: no run, no re-identify, no retry.`,
      `The ${isStpg ? "STPG" : "analyzer"} binding below is leased (an active owner rejects) but not opened or queried.`]),
      field(isStpg ? "STPG device binding" : "Analyzer binding", select("target_binding", bindingOptions(isStpg ? R.devices : R.analyzers), "", "choose the board wired to this RUN line…")),
      el("div", { class: "field-row two" }, field("LOW pulse (ms)", input("pulse_ms", "", { type: "number", min: 1, max: 5000, placeholder: "required, 1–5000" }), "A host delay, not a calibrated pulse width."),
        field("Settle after Hi-Z (ms)", input("settle_ms", "0", { type: "number", min: 0, max: 10000 }), "Optional, 0–10000. Not an enumeration wait.")));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    const prep = await post("/api/runner/control/prepare", { operation: "reset", controller: R.controller, target, target_binding: f.get("target_binding"),
      pulse_ms: intValue(f.get("pulse_ms")), settle_ms: intValue(f.get("settle_ms")) ?? 0 });
    showControlConfirm(prep);
  }, { okText: "Review…" });
}

function setGpioDialog() {
  const cfg = controllerItem(R.controller).config;
  const pins = cfg.protocol === "legacy-3" ? [0, 1, 2] : R.state.controller.pins;
  formDialog("Set a controller GPIO", (body) => {
    const pin = select("pin", pins.map((n) => [String(n), `${cfg.protocol === "legacy-3" ? "CH" : "GPIO"}${n} — ${pinRole(n)}`]), String(pins.find((n) => n > 1) ?? pins[0]));
    const state = select("state", [], "");
    const extra = el("div");
    const draw = () => {
      const n = Number(pin.value);
      state.innerHTML = "";
      state.append(el("option", { value: "", disabled: true, selected: true }, "choose a state…"));  // no implicit LOW
      for (const st of ["LOW", "Hi-Z", "HIGH"]) if (st !== "HIGH" || cfg.allow_high.includes(n)) state.append(el("option", { value: st }, st));
      extra.innerHTML = "";
      if (n <= 1) {
        extra.append(field(n === 0 ? "STPG device binding (RUN target)" : "Analyzer binding (RUN target)", select("target_binding", bindingOptions(n === 0 ? R.devices : R.analyzers), "", "required for a RUN pin…")));
        if (cfg.allow_high.includes(n)) extra.append(el("label", { class: "check" }, el("input", { type: "checkbox", name: "allow_run_high" }), " Exceptional: allow driving this RUN line HIGH for this one operation"));
      }
    };
    pin.addEventListener("change", draw);
    draw();
    body.append(el("div", { class: "field-row two" }, field("Pin", pin), field("State", state)), extra,
      el("p", { class: "muted small-print" }, "Sends exactly the selected state once. The level stays until changed explicitly."));
  }, async () => {
    const f = new FormData($("formDialogForm"));
    if (!f.get("state")) throw new Error("choose the GPIO state explicitly");
    const prep = await post("/api/runner/control/prepare", { operation: "set", controller: R.controller, pin: Number(f.get("pin")), state: f.get("state"),
      target_binding: f.get("target_binding") || null, allow_run_high: f.get("allow_run_high") === "on" });
    showControlConfirm(prep);
  }, { okText: "Review…" });
}

$("rpResetStpg").addEventListener("click", () => resetDialog("stpg"));
$("rpResetLa").addEventListener("click", () => resetDialog("la"));
$("rpSetGpio").addEventListener("click", () => { if (controllerItem(R.controller)) setGpioDialog(); });
$("rpControlCancel").addEventListener("click", cancelJob("control", "Cancel this GPIO operation?\n\nNo corrective pin command is sent. If the LOW was already applied, RUN can stay LOW (target held in reset)."));

function showControlConfirm(prep) {
  const s = prep.summary;
  $("formDialog").close();
  $("confirmControlTitle").textContent = s.operation === "reset" ? `Confirm reset via GPIO${s.pin}` : `Confirm GPIO${s.pin} → ${s.state}`;
  const box = $("confirmControlBody");
  box.innerHTML = "";
  box.append(kvList([["controller", `${s.controller}${s.simulated ? " (demo, no hardware)" : ""} · ${s.protocol}`], ["pin", `GPIO${s.pin} — ${s.role}`],
    ["target", s.target || "—"], ["result folder", copyable(s.output_dir)]]));
  box.append(el("h3", { class: "sub" }, "Steps, in this order"), el("ol", {}, ...s.steps.map((x) => el("li", {}, x))));
  box.append(notice("bad", "Read before sending", s.warnings));
  const acks = $("confirmControlAcks");
  acks.innerHTML = "";
  for (const [key, text] of Object.entries(prep.acknowledgements)) acks.append(el("label", {}, el("input", { type: "checkbox", name: key }), text));
  const start = $("confirmControlStart");
  const update = () => { start.disabled = [...acks.querySelectorAll("input")].some((i) => !i.checked); };
  acks.querySelectorAll("input").forEach((i) => i.addEventListener("change", update));
  update();
  $("confirmControlError").hidden = true;
  $("confirmControlDialog")._prep = prep;
  $("confirmControlDialog").showModal();
}

$("confirmControlForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const dialog = $("confirmControlDialog");
  const prep = dialog._prep;
  if (!prep) return;
  dialog._prep = null;  // one POST per confirmation
  $("confirmControlStart").disabled = true;
  const acknowledgements = {};
  $("confirmControlAcks").querySelectorAll("input").forEach((i) => { acknowledgements[i.name] = i.checked; });
  try {
    const job = await post("/api/runner/control/start", { nonce: prep.nonce, acknowledgements });
    dialog.close();
    watchJob("control", job, async () => { R.state = await api("/api/runner/state").catch(() => R.state); renderControl(); renderRun(); loadResults(); });
    R.state = await api("/api/runner/state").catch(() => R.state);
    renderControl(); renderRun();
  } catch (err) { $("confirmControlError").textContent = errorText(err); $("confirmControlError").hidden = false; }
});
