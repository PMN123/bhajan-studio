"use strict";

const KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"];   // Sai Samarpan's spelling in C
const SHARPS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const FLATS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"];
const FLAT_KEYS = new Set([1, 3, 5, 8, 10]);   // Sa = Db, Eb, F, Ab, Bb: the keys abcjs writes with flats
const KATTAI = ["1", "1½", "2", "2½", "3", "4", "4½", "5", "5½", "6", "6½", "7"];
const SARGAM = ["S", "R1", "R2", "G1", "G2", "M1", "M2", "P", "D1", "D2", "N1", "N2"];
const SPELL = [["C", ""], ["C", "^"], ["D", ""], ["E", "_"], ["E", ""], ["F", ""], ["F", "^"], ["G", ""], ["G", "^"], ["A", ""], ["B", "_"], ["B", ""]];
const TEMPO_WORDS = { slow: 70, "medium slow": 80, medium: 92, "medium fast": 105, fast: 120 };

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  song: null, slug: null, pc: 0, octave: 0,
  analysis: null, recSa: null, audioI: 0, dirAlt: false, snap: true, formant: true, shifted: {},
  score: null, sheetI: 0, view: "sargam", tempo: 90, instrument: "flute", drone: false, droneKind: "pa", click: false,
  timings: null, rendered: null, cur: -1, gridEls: [], staffEls: [], boxEls: [], raf: 0,
};

/* ---------------------------------------------------------------- utils */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const d = (await r.json()).detail;
      msg = Array.isArray(d) ? d.map((x) => x.msg || String(x)).join("; ") : d || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 4200);
}
const spinner = (text) => `<div class="status"><div class="spinner"></div><span>${esc(text)}</span></div>`;
const mod12 = (n) => ((n % 12) + 12) % 12;
function saMidi() { return 60 + mod12(state.pc + 5) - 5 + 12 * state.octave; }
function noteName(m, pc = state.pc) {
  const names = pc === 0 ? NOTE_NAMES : FLAT_KEYS.has(pc) ? FLATS : SHARPS;
  return names[mod12(m)] + (Math.floor(m / 12) - 1);
}
// Key names in text: black keys show both spellings ("C#/Db"), the flat one being how the staff writes it.
function keyName(pc) { return KEY_NAMES[pc] === FLATS[pc] || pc === 6 ? KEY_NAMES[pc] : `${KEY_NAMES[pc]}/${FLATS[pc]}`; }
function keyLabel(pc) { return `${keyName(pc)} (${KATTAI[pc]} kattai)`; }

/* -------------------------------------------------------------- search */
let searchTimer, searchSeq = 0;
async function doSearch(q) {
  const seq = ++searchSeq;
  try {
    const items = await api(`/api/search?q=${encodeURIComponent(q)}&limit=60`);
    if (seq !== searchSeq) return;
    const box = $("#results");
    if (!items.length) { box.innerHTML = `<div class="none">No bhajans match “${esc(q)}”.</div>`; return; }
    box.innerHTML = items.map((s) => `
      <button class="result${s.slug === state.slug ? " active" : ""}" data-slug="${esc(s.slug)}">
        <div class="t">${esc(s.title)}<span class="badges">${s.sheet ? '<span class="badge sheet">Sheet</span>' : ""}${s.audio ? '<span class="badge audio">Audio</span>' : ""}</span></div>
        <div class="s">${esc([s.raga.replace(/^~\s*/, ""), s.beat.split("/")[0]].filter(Boolean).join(" · "))}</div>
      </button>`).join("");
  } catch (e) { toast("Search failed: " + e.message); }
}
$("#q").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => doSearch(e.target.value), 160); });
$("#results").addEventListener("click", (e) => {
  const b = e.target.closest(".result");
  if (!b || b.dataset.slug === state.slug) return;
  document.querySelectorAll(".result.active").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  openSong(b.dataset.slug);
});

/* ---------------------------------------------------------------- song */
async function openSong(slug) {
  stopHighlight();
  Object.assign(state, { slug, song: null, analysis: null, recWanted: false, recSa: null, audioI: 0, dirAlt: false, shifted: {}, score: null, sheetI: 0, timings: null, rendered: null, cur: -1, octave: 0 });
  history.replaceState(null, "", "#" + encodeURIComponent(slug));
  $("#welcome").hidden = true;
  $("#song").hidden = false;
  $("#title").textContent = "Loading…";
  $("#meta").textContent = "";
  $("#lyrics").innerHTML = "";
  $("#keyboard").innerHTML = "";
  $("#refs").innerHTML = "";
  $("#keyhint").textContent = "";
  $("#recbody").innerHTML = spinner("Loading song details from Sai Rhythms…");
  $("#sheetbody").innerHTML = "";
  let song;
  try { song = await api(`/api/song/${encodeURIComponent(slug)}`); }
  catch (e) {
    if (state.slug !== slug) return;
    state.slug = null;   // so clicking the same result again retries
    $("#title").textContent = "Could not load this bhajan";
    $("#recbody").innerHTML = `<p class="err">${esc(e.message)}</p>`;
    return;
  }
  if (state.slug !== slug) return;
  state.song = song;
  state.pc = song.gents_pitch?.pc ?? 0;
  state.droneKind = /madhyam/i.test(song.gents_pitch?.shruti || "") ? "ma" : "pa";
  state.tempo = TEMPO_WORDS[(song.tempo || "").toLowerCase()] || 90;
  renderHead();
  renderKeyboard();
  renderRec();
  renderSheet();
  if (song.sheets.length) loadScore();
}

function renderHead() {
  const s = state.song;
  $("#title").textContent = s.title;
  const bits = [];
  if (s.raga) bits.push(`Raga <b>${esc(s.raga.replace(/^~\s*/, ""))}</b>`);
  if (s.beat) bits.push(esc(s.beat));
  if (s.tempo) bits.push(esc(s.tempo));
  if (s.raga_aroha.length) bits.push(`<span title="Arohana / avarohana">${esc(s.raga_aroha.join(" "))} · ${esc(s.raga_avaroha.join(" "))}</span>`);
  $("#meta").innerHTML = bits.join(" · ");
  $("#srlink").href = s.url;
  $("#lyrics").innerHTML = `<div class="lines">${s.lyrics.map(esc).join("<br>")}</div>${s.meaning ? `<div class="meaning">${esc(s.meaning)}</div>` : ""}`;
}

/* ------------------------------------------------------------ keyboard */
function renderKeyboard() {
  const s = state.song;
  if (!s) return;
  const kb = $("#keyboard");
  const whites = [0, 2, 4, 5, 7, 9, 11];
  const blackPos = { 1: 1, 3: 2, 6: 4, 8: 5, 10: 6 };
  const recKnown = state.analysis && !state.analysis.error && Number.isInteger(state.recSa);
  const pcOk = (p) => p && Number.isInteger(p.pc);
  const tags = (pc) => {
    const t = [];
    if (pcOk(s.gents_pitch) && s.gents_pitch.pc === pc) t.push('<span class="tag" title="Sai Rhythms reference pitch for gents">Gents</span>');
    if (pcOk(s.ladies_pitch) && s.ladies_pitch.pc === pc) t.push('<span class="tag" title="Sai Rhythms reference pitch for ladies">Ladies</span>');
    if (recKnown && state.recSa === pc) t.push('<span class="tag rec" title="Key of the recording">Rec</span>');
    return t.length ? `<span class="tags">${t.join("")}</span>` : "";
  };
  let html = whites.map((pc) => `<div class="key white${pc === state.pc ? " sel" : ""}" role="radio" aria-checked="${pc === state.pc}" tabindex="0" data-pc="${pc}">${tags(pc)}<span class="n">${KEY_NAMES[pc]}</span><span class="k">${KATTAI[pc]}</span></div>`).join("");
  html += Object.entries(blackPos).map(([pc, slot]) => {
    pc = +pc;
    return `<div class="key black${pc === state.pc ? " sel" : ""}" role="radio" aria-checked="${pc === state.pc}" tabindex="0" data-pc="${pc}" style="left:calc(${(slot / 7) * 100}% - 4.5%)">${tags(pc)}<span class="n">${KEY_NAMES[pc]}</span><span class="k">${KATTAI[pc]}</span></div>`;
  }).join("");
  kb.innerHTML = html;
  $("#keyhint").textContent = `Sa = ${keyLabel(state.pc)}. Click a key, or use a reference pitch below.`;
  const refs = [];
  if (pcOk(s.gents_pitch)) refs.push(`<button class="chip" data-pc="${s.gents_pitch.pc}">Gents reference: <b>${s.gents_pitch.kattai} ${esc(s.gents_pitch.shruti)} / ${esc(s.gents_pitch.letter)}</b></button>`);
  if (pcOk(s.ladies_pitch)) refs.push(`<button class="chip" data-pc="${s.ladies_pitch.pc}">Ladies reference: <b>${s.ladies_pitch.kattai} ${esc(s.ladies_pitch.shruti)} / ${esc(s.ladies_pitch.letter)}</b></button>`);
  if (recKnown) refs.push(`<button class="chip" data-pc="${state.recSa}">Recording's key: <b>${keyName(state.recSa)}</b></button>`);
  if (s.range?.low) refs.push(`<span class="chip" style="cursor:default">Range: <b>${esc(s.range.low)} – ${esc(s.range.high)}</b></span>`);
  $("#refs").innerHTML = refs.join("");
}
function pickKey(pc) {
  if (!state.song || !Number.isInteger(pc) || pc < 0 || pc > 11 || pc === state.pc) return;
  state.pc = pc;
  renderKeyboard();
  renderRec();
  updateSheetForKey();
}
$("#keyboard").addEventListener("click", (e) => { const k = e.target.closest(".key"); if (k) pickKey(+k.dataset.pc); });
$("#keyboard").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { const k = e.target.closest(".key"); if (k) { e.preventDefault(); pickKey(+k.dataset.pc); } }
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); pickKey(mod12(state.pc + (e.key === "ArrowRight" ? 1 : -1))); $(`.key[data-pc="${state.pc}"]`)?.focus(); }
});
$("#refs").addEventListener("click", (e) => { const c = e.target.closest("button.chip"); if (c) pickKey(+c.dataset.pc); });

/* ----------------------------------------------------------- recording */
async function analyze() {
  const slug = state.slug, audioI = state.audioI;
  const current = () => state.slug === slug && state.audioI === audioI;
  try {
    const a = await post(`/api/song/${encodeURIComponent(slug)}/analyze?audio_i=${audioI}`);
    if (!current()) return;
    state.analysis = a;
    state.recSa = a.sa_pc;
    renderKeyboard();
    renderRec();
  } catch (e) {
    if (!current()) return;
    state.analysis = { error: e.message };
    renderKeyboard();
    renderRec();
  }
}

function shiftPlan() {
  const a = state.analysis;
  const near = (() => { let d = mod12(state.pc - state.recSa); return d > 6 ? d - 12 : d; })();
  const alt = near > 0 ? near - 12 : near + 12;
  const steps = state.dirAlt && near !== 0 ? alt : near;
  const fine = state.snap ? -(a.tuning_cents || 0) / 100 : 0;
  return { near, alt, steps, semis: Math.round((steps + fine) * 1000) / 1000 };
}
const stepsText = (n) => n === 0 ? "no change" : `${n > 0 ? "up" : "down"} ${Math.abs(n)} semitone${Math.abs(n) === 1 ? "" : "s"}`;

function renderRec() {
  const s = state.song, body = $("#recbody");
  if (!s) return;
  if (!s.audio.length) { body.innerHTML = `<p class="hint">Sai Rhythms has no recording for this bhajan, so only the sheet-music version is available.</p>`; return; }
  const recSel = s.audio.length > 1 ? `<label class="field">Recording <select id="recsel">${s.audio.map((a, i) => `<option value="${i}"${i === state.audioI ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</select></label>` : "";
  const orig = `/api/song/${encodeURIComponent(s.slug)}/audio/${state.audioI}`;
  const a = state.analysis;
  const keep = [...body.querySelectorAll("audio[data-k]")];
  if (!a && !state.recWanted) {
    const n = s.audio.length;
    setRecBody(body, `<div class="row"><button class="btn" id="userec">Use the recording${n > 1 ? "s" : ""}</button>
      <span class="hint">Sai Rhythms has ${n} recording${n > 1 ? "s" : ""} of this bhajan. The mp3 (about 3 MB) is downloaded once, when you ask for it.</span></div>`, keep);
    return;
  }
  if (!a) { setRecBody(body, `<div class="row">${recSel}</div>${spinner("Downloading the recording and listening for its Sa…")}`, keep); return; }
  if (a.error) {
    setRecBody(body, `<div class="row">${recSel}</div><p class="err">${esc(a.error)}</p><div class="player"><div class="lbl">Original</div><audio controls preload="none" data-k="orig" src="${orig}"></audio></div>`, keep);
    return;
  }
  const p = shiftPlan();
  const conf = { high: "ok", medium: "warn", low: "err" }[a.confidence] || "";
  const key = `${state.audioI}|${p.semis}|${state.formant}`;
  const done = state.shifted[key];
  setRecBody(body, `
    <div class="row">${recSel}
      <span class="big">Sung in ${keyName(state.recSa)}</span>
      <span class="hint">${KATTAI[state.recSa]} kattai · ${a.tuning_cents >= 0 ? "+" : ""}${a.tuning_cents}¢ from A440 ·
        <span class="${conf}">${a.confidence} confidence</span> (${esc(a.basis)})</span>
    </div>
    <div class="row">
      <label class="field">Not right? Recording's Sa is
        <select id="recsa">${KEY_NAMES.map((_, i) => `<option value="${i}"${i === state.recSa ? " selected" : ""}>${keyName(i)}${i === a.sa_pc ? " (detected)" : ""}</option>`).join("")}</select></label>
    </div>
    <div class="row">
      <span class="field">To reach ${keyName(state.pc)}:</span>
      ${p.near === 0 ? `<span class="ok">already in your key${state.snap && Math.abs(a.tuning_cents) > 3 ? " (only fine-tuning)" : ""}</span>` : `
      <div class="seg" role="group" aria-label="Direction">
        <button data-alt="0" class="${!state.dirAlt ? "on" : ""}">${stepsText(p.near)}</button>
        <button data-alt="1" class="${state.dirAlt ? "on" : ""}">${stepsText(p.alt)}</button>
      </div>`}
      <label class="field"><input type="checkbox" id="snap" ${state.snap ? "checked" : ""}> Tune to A440</label>
      <label class="field"><input type="checkbox" id="formant" ${state.formant ? "checked" : ""}> Keep natural voice timbre</label>
    </div>
    <div class="row">
      <button class="btn" id="mkshift" ${done ? "hidden" : ""}>Shift recording to ${keyName(state.pc)}</button>
      <span id="shiftstatus"></span>
    </div>
    <div class="players">
      <div class="player"><div class="lbl"><span>Original · ${keyName(state.recSa)}</span><a class="dl" href="${orig}" download>mp3</a></div><audio controls preload="none" data-k="orig" src="${orig}"></audio></div>
      <div class="player" id="shiftplayer" ${done ? "" : "hidden"}><div class="lbl"><span>Shifted · ${keyName(state.pc)} (${p.semis > 0 ? "+" : ""}${p.semis} st)</span>${done ? `<a class="dl" href="${done}" download="${esc(s.slug)}-${KEY_NAMES[state.pc]}.mp3">Download mp3</a>` : ""}</div><audio controls preload="none" data-k="shift" ${done ? `src="${done}"` : ""}></audio></div>
    </div>`, keep);
}

// Rebuild the recording card, but keep any player whose file is unchanged: re-inserting the same <audio>
// element in the same task keeps it playing at its position instead of stopping it.
function setRecBody(body, html, keep) {
  body.innerHTML = html;
  for (const old of keep) {
    const nu = body.querySelector(`audio[data-k="${old.dataset.k}"]`);
    if (nu && old.getAttribute("src") && nu.getAttribute("src") === old.getAttribute("src")) nu.replaceWith(old);
  }
  bindRec();
}

function bindRec() {
  $("#userec")?.addEventListener("click", () => { state.recWanted = true; renderRec(); analyze(); });
  $("#recsel")?.addEventListener("change", (e) => { state.audioI = +e.target.value; state.analysis = null; renderRec(); analyze(); });
  $("#recsa")?.addEventListener("change", (e) => { state.recSa = +e.target.value; state.dirAlt = false; renderKeyboard(); renderRec(); });
  document.querySelectorAll("#recbody .seg button").forEach((b) => b.addEventListener("click", () => { state.dirAlt = b.dataset.alt === "1"; renderRec(); }));
  $("#snap")?.addEventListener("change", (e) => { state.snap = e.target.checked; renderRec(); });
  $("#formant")?.addEventListener("change", (e) => { state.formant = e.target.checked; renderRec(); });
  $("#mkshift")?.addEventListener("click", makeShift);
}

async function makeShift() {
  const p = shiftPlan(), slug = state.slug;
  const key = `${state.audioI}|${p.semis}|${state.formant}`;
  const btn = $("#mkshift");
  btn.disabled = true;
  $("#shiftstatus").innerHTML = spinner("Pitch-shifting with Rubber Band (about 15 s for a 5-minute recording)…");
  try {
    const r = await post(`/api/song/${encodeURIComponent(slug)}/shift`, { audio_i: state.audioI, semitones: p.semis, formant: state.formant });
    if (state.slug !== slug) return;
    state.shifted[key] = r.audio_url;
    renderRec();
    $("#shiftplayer audio")?.play().catch(() => {});
  } catch (e) {
    toast("Shift failed: " + e.message);
    btn.disabled = false;
    $("#shiftstatus").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

/* --------------------------------------------------------------- sheet */
function renderSheet() {
  const s = state.song, body = $("#sheetbody");
  if (!s.sheets.length) { body.innerHTML = `<p class="hint">This bhajan has no sheet music on Sai Rhythms${s.audio.length ? ", but the recording above can still be shifted to your key" : ""}.</p>`; return; }
  body.innerHTML = spinner("Reading the sheet music (first time takes 5–15 s)…");
}

async function loadScore() {
  const slug = state.slug;
  try {
    const sc = await post(`/api/song/${encodeURIComponent(slug)}/score?sheet_i=${state.sheetI}`);
    if (state.slug !== slug) return;
    state.score = sc;
    if (sc.tempo) state.tempo = sc.tempo;
    renderScore();
  } catch (e) {
    if (state.slug !== slug) return;
    $("#sheetbody").innerHTML = `<p class="err">Could not read the sheet music: ${esc(e.message)}</p>`;
  }
}

function qualityLine(sc) {
  const st = sc.stats, bits = [];
  bits.push(`Read ${st.notes} notes in ${st.measures} bars`);
  bits.push(st.inconsistent_measures.length ? `<span class="warn">${st.inconsistent_measures.length} bar(s) don't add up; check the highlighted notes</span>` : `<span class="ok">every bar adds up</span>`);
  if (st.labelled) bits.push(st.label_agreement === 1 ? `<span class="ok">all ${st.labelled} printed sargam labels that OCR could read match</span>` : `<span class="warn">${Math.round(st.label_agreement * st.labelled)}/${st.labelled} printed labels match</span>`);
  return bits.join(" · ");
}

function renderScore() {
  const sc = state.score, s = state.song;
  const sheetSel = s.sheets.length > 1 ? `<label class="field">Sheet <select id="sheetsel">${s.sheets.map((_, i) => `<option value="${i}"${i === state.sheetI ? " selected" : ""}>Version ${i + 1}</option>`).join("")}</select></label>` : "";
  $("#sheetbody").innerHTML = `
    <div class="quality">${qualityLine(sc)}</div>
    <div class="row" id="rangeline"></div>
    <div class="row">
      ${sheetSel}
      <label class="field">Tempo <input type="range" id="tempo" min="40" max="220" step="1" value="${Math.round(state.tempo)}"><span class="v" id="tempov">${Math.round(state.tempo)}</span> bpm</label>
      <label class="field">Instrument <select id="instr"><option value="flute" selected>Flute</option><option value="piano">Piano</option><option value="harmonium">Harmonium</option></select></label>
      <label class="field">Octave <select id="oct"><option value="-1">Lower</option><option value="0" selected>Middle</option><option value="1">Higher</option></select></label>
      <label class="field" title="A soft Sa-Pa drone underneath, like a tanpura or shruti box"><input type="checkbox" id="drone" ${state.drone ? "checked" : ""}> Add tanpura drone
        <select id="dronek"><option value="pa">Sa-Pa</option><option value="ma">Sa-Ma</option></select></label>
      <label class="field"><input type="checkbox" id="click" ${state.click ? "checked" : ""}> Click</label>
    </div>
    <div class="row">
      <button class="btn" id="play">Play in ${keyName(state.pc)}</button>
      <span id="renderstatus"></span>
      <a class="dl" id="dlwav" hidden download>Download WAV</a>
      <a class="dl" id="dlmidi" download>Download MIDI</a>
    </div>
    <div class="player" id="synthplayer" hidden><div class="lbl"><span id="synthlbl"></span></div><audio controls id="synth"></audio></div>
    <div class="views">
      <div class="seg" role="tablist">
        <button data-v="sargam" class="${state.view === "sargam" ? "on" : ""}">Sargam</button>
        <button data-v="staff" class="${state.view === "staff" ? "on" : ""}">Staff notation</button>
        <button data-v="sheet" class="${state.view === "sheet" ? "on" : ""}">Original sheet</button>
      </div>
      <div class="view" id="v-sargam"></div>
      <div class="view" id="v-staff" hidden><div id="staff"></div><p class="legend">Transposed staff notation (sargam above each note, lyrics below).</p></div>
      <div class="view" id="v-sheet" hidden></div>
    </div>`;
  $("#instr").value = state.instrument;
  $("#oct").value = String(state.octave);
  $("#dronek").value = state.droneKind;
  $("#sheetsel")?.addEventListener("change", (e) => { state.sheetI = +e.target.value; state.score = null; renderSheet(); loadScore(); });
  $("#tempo").addEventListener("input", (e) => { state.tempo = +e.target.value; $("#tempov").textContent = e.target.value; synthDirty(); updateMidiLink(); });
  $("#instr").addEventListener("change", (e) => { state.instrument = e.target.value; synthDirty(); updateMidiLink(); });
  $("#oct").addEventListener("change", (e) => { state.octave = +e.target.value; updateSheetForKey(); });
  $("#drone").addEventListener("change", (e) => { state.drone = e.target.checked; synthDirty(); });
  $("#dronek").addEventListener("change", (e) => { state.droneKind = e.target.value; synthDirty(); });
  $("#click").addEventListener("change", (e) => { state.click = e.target.checked; synthDirty(); });
  $("#play").addEventListener("click", playSynth);
  document.querySelectorAll(".views .seg button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.v)));
  const au = $("#synth");
  au.addEventListener("play", startHighlight);
  au.addEventListener("pause", stopHighlight);
  au.addEventListener("ended", () => { stopHighlight(); setCur(-1); });
  state.rendered = null;
  state.timings = null;
  buildSheetView();
  updateSheetForKey();
  setView(state.view);
}

function renderReq() {
  return { sheet_i: state.sheetI, sa_midi: saMidi(), tempo: state.tempo, instrument: state.instrument,
    drone: state.drone, drone_kind: state.droneKind, click: state.click };
}
// The Play button says when the player holds audio made with other settings than the current ones.
function synthDirty() {
  const btn = $("#play");
  if (!btn) return;
  const changed = state.rendered && JSON.stringify(state.rendered) !== JSON.stringify(renderReq());
  btn.textContent = `Play in ${keyName(state.pc)}${changed ? " (settings changed)" : ""}`;
}

function updateMidiLink() {
  const a = $("#dlmidi");
  if (!a || !state.score) return;
  a.href = `/api/song/${encodeURIComponent(state.slug)}/midi?sheet_i=${state.sheetI}&sa_midi=${saMidi()}&tempo=${state.tempo}&instrument=${encodeURIComponent(state.instrument)}`;
  a.setAttribute("download", `${state.slug}-${KEY_NAMES[state.pc]}.mid`);
}

function updateSheetForKey() {
  if (!state.score) return;
  const sa = saMidi();
  const notes = state.score.events.filter((e) => e.kind === "note");
  if (notes.length) {
    const lo = notes.reduce((a, e) => (e.midi < a.midi ? e : a)), hi = notes.reduce((a, e) => (e.midi > a.midi ? e : a));
    $("#rangeline").innerHTML = `<span class="field">Melody range in ${keyName(state.pc)}: <b class="v">${noteName(lo.midi - 60 + sa)}</b> (${esc(lo.sargam)}) to <b class="v">${noteName(hi.midi - 60 + sa)}</b> (${esc(hi.sargam)}); Sa = ${noteName(sa)}</span>`;
  }
  synthDirty();
  updateMidiLink();
  buildSargam();
  if (state.view === "staff") buildStaff();
  else state.staffBuiltFor = null;
}

function setView(v) {
  state.view = v;
  document.querySelectorAll(".views .seg button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  for (const id of ["sargam", "staff", "sheet"]) $("#v-" + id).hidden = id !== v;
  if (v === "staff" && state.staffBuiltFor !== `${state.pc}|${state.octave}`) buildStaff();
  if (state.cur >= 0) setCur(state.cur, true);
}

function buildSargam() {
  const sa = saMidi(), evs = state.score.events;
  const lines = new Map();
  evs.forEach((e, i) => {
    if (!lines.has(e.staff)) lines.set(e.staff, new Map());
    const ms = lines.get(e.staff);
    if (!ms.has(e.measure)) ms.set(e.measure, []);
    ms.get(e.measure).push(i);
  });
  let html = "";
  for (const ms of lines.values()) {
    html += `<div class="sline">`;
    for (const idxs of ms.values()) {
      const total = idxs.reduce((a, i) => a + evs[i].dur, 0);
      html += `<div class="meas" style="flex-grow:${total}">`;
      for (const i of idxs) {
        const e = evs[i];
        const cls = ["ev", e.kind === "rest" ? "rest" : "", e.tie_next ? "tie" : "", e.conf < 1 ? "flag" : ""].join(" ");
        const title = e.kind === "note" ? `${e.sargam} · ${noteName(e.midi - 60 + sa)} · ${e.dur} beat${e.dur === 1 ? "" : "s"}` : `rest · ${e.dur} beat${e.dur === 1 ? "" : "s"}`;
        html += `<div class="${cls}" data-i="${i}" style="flex-grow:${e.dur}" title="${esc(title)}">
          <div class="sw">${e.kind === "note" ? esc(e.sargam) : "–"}</div>
          <div class="wl">${e.kind === "note" ? esc(noteName(e.midi - 60 + sa)) : "&nbsp;"}</div>
          <div class="ly">${esc(e.lyric) || "&nbsp;"}</div></div>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
  }
  $("#v-sargam").innerHTML = `<div class="sargam">${html}</div><p class="legend">Sargam as printed (lowercase = lower octave, + = upper octave). Under each swara: the actual note in ${keyName(state.pc)}, then the lyric. Underlined cells were uncertain.</p>`;
  state.gridEls = [...document.querySelectorAll("#v-sargam .ev")];
  if (state.cur >= 0) setCur(state.cur, true);
}

function abcFor(score) {
  const bar = score.bar || 4;
  const meter = Number.isInteger(bar * 2) && (bar * 2) % 2 === 1 ? `${bar * 2}/8` : `${Math.round(bar)}/4`;
  let abc = `X:1\nM:${meter}\nL:1/32\nQ:1/4=${Math.round(state.tempo)}\nK:C\n`;
  // inside an annotation abcjs still reads % as a comment, a backslash as an escape and " as its end
  const clean = (t) => String(t || "").replace(/["%\\]/g, "");
  let staff = null, meas = null, acc = {}, line = "";
  const flush = (end) => { if (line) abc += line + end + "\n"; line = ""; };
  score.events.forEach((e) => {
    if (e.staff !== staff) { if (staff !== null) flush(" |"); staff = e.staff; meas = e.measure; acc = {}; }
    else if (e.measure !== meas) { line += " |"; meas = e.measure; acc = {}; }
    const len = Math.max(1, Math.round(e.dur * 8));
    const L = len === 1 ? "" : String(len);
    if (e.kind === "rest") { line += ` z${L}`; return; }
    const m = e.midi;
    let [letter, a] = SPELL[mod12(m - 60)];
    const wl = (e.wlabel || "").match(/^([A-G])([b#]?)$/);
    if (wl) {
      const pc = ({ C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 }[wl[1]] + (wl[2] === "#" ? 1 : wl[2] === "b" ? -1 : 0));
      if (mod12(pc) === mod12(m)) { letter = wl[1]; a = wl[2] === "#" ? "^" : wl[2] === "b" ? "_" : ""; }
    }
    const letterPc = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 }[letter];
    const accN = a === "^" ? 1 : a === "_" ? -1 : 0;
    const oct = Math.round((m - accN - letterPc) / 12) - 1;
    let name = oct >= 5 ? letter.toLowerCase() + "'".repeat(oct - 5) : letter + ",".repeat(Math.max(0, 4 - oct));
    const k = letter + oct;
    const prev = acc[k] ?? "";
    let accTxt = "";
    if (a !== prev) { accTxt = a === "" ? "=" : a; acc[k] = a; }
    const lyr = clean(e.lyric);
    line += ` "^${clean(e.sargam)}"${lyr ? `"_${lyr}"` : ""}${accTxt}${name}${L}${e.tie_next ? "-" : ""}`;
  });
  flush(" |]");
  return abc;
}

function buildStaff() {
  const el = $("#staff");
  if (!window.ABCJS) { el.innerHTML = `<p class="err">Staff renderer failed to load.</p>`; return; }
  const shift = saMidi() - 60;
  ABCJS.renderAbc(el, abcFor(state.score), { visualTranspose: shift, add_classes: true, responsive: "resize", staffwidth: 900, paddingleft: 0, paddingright: 0 });
  state.staffEls = [...el.querySelectorAll(".abcjs-note, .abcjs-rest")];
  if (state.staffEls.length !== state.score.events.length) state.staffEls = [];
  state.staffBuiltFor = `${state.pc}|${state.octave}`;
  if (state.cur >= 0) setCur(state.cur, true);
}

function buildSheetView() {
  const sc = state.score;
  const pages = sc.page_urls.map((url, p) => {
    const info = sc.pages[p];
    const boxes = sc.events.map((e, i) => {
      if (e.page !== p || !e.bbox?.length) return "";
      const [x0, y0, x1, y1] = e.bbox;
      const pad = 4;
      return `<div class="box ${e.kind}" data-i="${i}" style="left:${((x0 - pad) / info.w) * 100}%;top:${((y0 - pad) / info.h) * 100}%;width:${((x1 - x0 + 2 * pad) / info.w) * 100}%;height:${((y1 - y0 + 2 * pad) / info.h) * 100}%"></div>`;
    }).join("");
    return `<div class="page"><img src="${url}" alt="Sheet music page ${p + 1}" loading="lazy">${boxes}</div>`;
  }).join("");
  $("#v-sheet").innerHTML = `<p class="legend">The original Sai Samarpan sheet with every note and rest the reader detected outlined, so you can check it.</p><div class="pages">${pages}</div>`;
  state.boxEls = [];
  document.querySelectorAll("#v-sheet .box").forEach((b) => { state.boxEls[+b.dataset.i] = b; });
}

async function playSynth() {
  const btn = $("#play"), slug = state.slug, req = renderReq(), pc = state.pc, title = state.song.title;
  const current = () => state.slug === slug && state.sheetI === req.sheet_i && state.score;
  btn.disabled = true;
  $("#renderstatus").innerHTML = spinner("Rendering audio…");
  try {
    const r = await post(`/api/song/${encodeURIComponent(slug)}/render`, req);
    if (!current()) return;
    state.timings = r.timings;
    state.rendered = req;
    const au = $("#synth");
    au.src = r.audio_url;
    $("#synthplayer").hidden = false;
    $("#synthlbl").textContent = `${title} · Sa = ${noteName(req.sa_midi, pc)} · ${Math.round(req.tempo)} bpm · ${req.instrument}`;
    const dl = $("#dlwav");
    dl.href = r.audio_url;
    dl.download = `${slug}-${KEY_NAMES[pc]}.wav`;
    dl.hidden = false;
    $("#renderstatus").innerHTML = "";
    synthDirty();
    au.play().catch(() => { $("#renderstatus").innerHTML = `<span class="hint">Ready. Press ▶ on the player below.</span>`; });
  } catch (e) {
    if (!current()) return;
    $("#renderstatus").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  } finally { btn.disabled = false; }
}

/* ---------------------------------------------------------- highlight */
function setCur(i, force) {
  if (i === state.cur && !force) return;
  const prev = state.cur;
  if (prev >= 0) {
    state.gridEls[prev]?.classList.remove("cur");
    state.staffEls[prev]?.classList.remove("hl");
    state.boxEls[prev]?.classList.remove("cur");
  }
  state.cur = i;
  if (i < 0) return;
  const g = state.gridEls[i];
  g?.classList.add("cur");
  state.staffEls[i]?.classList.add("hl");
  const b = state.boxEls[i];
  b?.classList.add("cur");
  const target = state.view === "sargam" ? g : state.view === "sheet" ? b : state.staffEls[i];
  if (target && !force) {
    const scroller = state.view === "sargam" ? $(".sargam") : null;
    if (scroller) {
      const r = target.getBoundingClientRect(), sr = scroller.getBoundingClientRect();
      if (r.top < sr.top || r.bottom > sr.bottom || r.left < sr.left || r.right > sr.right) target.scrollIntoView({ block: "nearest", inline: "nearest" });
    } else {
      const r = target.getBoundingClientRect();
      if (r.top < 80 || r.bottom > innerHeight - 20) target.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}
function tick() {
  const au = $("#synth"), tm = state.timings;
  if (!au || !tm) return;
  const t = au.currentTime;
  let lo = 0, hi = tm.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (tm[mid][0] <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  setCur(idx);
  state.raf = requestAnimationFrame(tick);
}
function startHighlight() { cancelAnimationFrame(state.raf); state.raf = requestAnimationFrame(tick); }
function stopHighlight() { cancelAnimationFrame(state.raf); }

/* ------------------------------------------------------------------ boot */
doSearch("");
const initial = decodeURIComponent(location.hash.slice(1));
if (initial) openSong(initial);
addEventListener("hashchange", () => {
  const slug = decodeURIComponent(location.hash.slice(1));
  if (slug && slug !== state.slug) openSong(slug);
});
