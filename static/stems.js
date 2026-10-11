"use strict";
/* Parts: the recording split into singing (lead and group), tabla, harmonium and the rest (bhajan/stems.py),
   shifted to your key on the server and mixed live here with Web Audio; and a session of several
   bhajans exported as one MP3. Loaded after app.js and uses its state and helpers directly. app.js
   only announces "bs:song" (a song is opening) and "bs:rec" (the recording card was redrawn). */

const PART_IDS = ["voice", "tabla", "harmonium", "other"];
// What a centre is missing decides the mix: no instrumentalists -> the music; no singers -> the singing; a
// tablist or a harmonium player of its own -> everything but that instrument.
const PART_PRESETS = [
  { label: "Full recording", on: PART_IDS },
  { label: "Music only", on: ["tabla", "harmonium", "other"] },
  { label: "Singing only", on: ["voice"] },
  { label: "Without tabla", on: ["voice", "harmonium", "other"] },
  { label: "Without harmonium", on: ["voice", "tabla", "other"] },
];
const PART_TEMPOS = [0.85, 0.9, 0.95, 1, 1.05, 1.1, 1.15];
const SESSION_KEY = "bhajanStudio.session.v1";

const parts = {
  slug: null, audioI: null,            // the recording the card is for (null: card hidden)
  status: null, seq: 0, pollTimer: 0,  // split status from the server
  gains: Object.fromEntries(PART_IDS.map((p) => [p, 0])), muted: {},   // dB per part; the mix carries over to the next song
  tempo: 1, want: null, loaded: null, renderSeq: 0, renderTimer: 0, mixSeq: 0,
  engaged: false, loading: false,      // nothing is rendered or decoded until the first Play
  at: 0, resume: false,                // where to carry on after a re-render, in seconds of the original
  seeking: false,
};
// One source per part -> its gain -> master gain (undoes the parts' -6 dB) -> delay -> dry -> speakers for the
// original mix, or, for any other mix, master -> limiter -> wet -> speakers (crossfaded, see partsRoute). The
// sources, part gains and master are made for each set of parts; the rest once per context (partsCtx).
const partsAudio = { ctx: null, rate: 0, bufs: null, gains: {}, master: null, limiter: null, delay: null, dry: null, wet: null,
  limited: false, sources: [], tempo: 1, t0: 0, offset: 0, duration: 0, playing: false, run: 0, raf: 0 };
// The limiter is a DynamicsCompressor, which adds makeup gain of (its gain at 0 dBFS)^-0.6: with a -1 dB
// threshold and 20:1 a full-scale peak comes out at -0.95 dB, so +0.57 dB. The wet path takes it back off.
const LIMITER_TRIM = 10 ** (-0.57 / 20);
const PARTS_PAUSED = `<p class="hint" data-paused>Audio was paused by the browser. Press Play again.</p>`;

const partsTime = (t) => { t = Math.max(0, Math.floor(t || 0)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };
const dbText = (g) => `${g > 0 ? "+" : ""}${g} dB`;
const partsLevel = (p) => (parts.muted[p] ? 0 : 10 ** ((parts.gains[p] ?? 0) / 20));
const partsGainsReq = () => Object.fromEntries(PART_IDS.map((p) => [p, parts.muted[p] ? null : parts.gains[p]]));
function partsTarget() { const semis = shiftPlan().semis; return { semis, tempo: parts.tempo, id: `${semis}|${parts.tempo}` }; }
const partsWhere = (t) => `${keyName(state.pc)}${t.tempo !== 1 ? ` at ${Math.round(t.tempo * 100)}% speed` : ""}`;
function partsPresetIndex() {
  return PART_PRESETS.findIndex((pr) => PART_IDS.every((p) => (pr.on.includes(p) ? !parts.muted[p] && parts.gains[p] === 0 : !!parts.muted[p])));
}
function partsPresetLabel() { const i = partsPresetIndex(); return i >= 0 ? PART_PRESETS[i].label : "Custom"; }

/* ---------------------------------------------------------- app hooks */
document.addEventListener("bs:song", () => { $("#partscard").hidden = true; partsReset(null, null); });
document.addEventListener("bs:rec", () => {
  const a = state.analysis, show = !!(state.song?.audio.length && a && !a.error);
  $("#partscard").hidden = !show;
  if (!show) { if (parts.slug) partsReset(null, null); return; }
  if (parts.slug !== state.slug || parts.audioI !== state.audioI) { partsReset(state.slug, state.audioI); partsCheck(); return; }
  partsSync();
});

function partsReset(slug, audioI) {
  partsClose();
  clearTimeout(parts.pollTimer);
  clearTimeout(parts.renderTimer);
  Object.assign(parts, { slug, audioI, status: null, seq: parts.seq + 1, tempo: 1, want: null, loaded: null,
    renderSeq: parts.renderSeq + 1, mixSeq: parts.mixSeq + 1, engaged: false, loading: false, at: 0, resume: false, seeking: false });
  $("#partsbody").innerHTML = "";
}

// Replace the card's content; if the keyboard was in the card, put it on the new primary control.
function partsSwap(html, primary) {
  const had = $("#partscard").contains(document.activeElement);
  $("#partsbody").innerHTML = html;
  if (had) $(primary, $("#partsbody"))?.focus();
}

/* ------------------------------------------------------------- split */
async function partsCheck(method = "GET") {
  const slug = parts.slug, audioI = parts.audioI, seq = ++parts.seq;
  const current = () => parts.slug === slug && parts.audioI === audioI && parts.seq === seq;
  clearTimeout(parts.pollTimer);
  if (!parts.status) $("#partsbody").innerHTML = spinner("Checking for parts…");
  try {
    const s = await api(`/api/song/${encodeURIComponent(slug)}/parts?audio_i=${audioI}`, { method });
    if (!current()) return;
    parts.status = s;
  } catch (e) {
    if (!current()) return;
    parts.status = { state: "failed", error: e.message, method };
  }
  partsRenderStatus();
  const st = parts.status.state;
  if (st === "queued" || st === "running") parts.pollTimer = setTimeout(() => partsCheck(), 1000);
  if (st === "done") partsSync(0);
}

function partsRenderStatus() {
  const s = parts.status;
  if (s.state === "queued" || s.state === "running") { partsProgress(s); return; }
  if (s.state === "done" && s.parts) {
    if (!$("#pmixer")) { partsSwap(partsMixerHtml(s), "#pplay"); partsMixerSync(); partsTransport(); }
    return;
  }
  if (s.state === "error" || s.state === "failed") {
    const what = s.state === "error" ? "The split failed" : "Could not reach the parts";
    partsSwap(`<p class="err" id="perr">${what}: ${esc(s.error)}</p>
      <div class="row"><button class="btn" data-act="${s.state === "error" || s.method === "POST" ? "split" : "check"}" aria-describedby="perr">Retry</button></div>`, "button");
    return;
  }
  if (s.state === "unavailable") { partsSwap(`<p class="hint">${esc(s.msg)}</p>`); return; }
  const official = s.official?.length
    ? `<p class="hint">Sai Rhythms has an official ${esc(s.official.join(" / "))} recording of this bhajan: try it in the recording list above first. Where one exists it sounds cleaner than any split.</p>` : "";
  // none or cancelled (or parts that have since been cleared from the cache)
  partsSwap(`${official}
    <div class="row"><button class="btn" data-act="split">Split into parts</button>${s.state === "cancelled" ? `<span class="hint">Split cancelled.</span>` : ""}</div>
    <p class="hint">The first split downloads PyTorch (about 1 GB) and two separation models (about 1.6 GB), once. After that a bhajan
      takes about 1–3 minutes on an Apple Silicon Mac. Everything stays on this Mac.</p>
    <p class="hint">The parts are separated by AI models from a single recording, so expect some bleed, such as the harmonium faintly in the singing.</p>`, "button");
}

// Updated in place while polling, so the Cancel button stays put under the pointer.
function partsProgress(s) {
  if (!$("#psplit")) {
    partsSwap(`<div id="psplit">
      <p class="pstep" id="pstep" role="status" aria-live="polite"></p>
      <div class="pbar" id="pbar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><span></span></div>
      <div class="row"><span class="hint" id="plabel"></span><button class="btn ghost" data-act="cancel">Cancel</button></div>
    </div>`, '[data-act="cancel"]');
  }
  const waiting = s.state === "queued", n = (s.ahead || 0) + 1;
  const busy = waiting || !s.sub || s.sub === "load";   // no fraction to show yet
  const pct = Math.round((s.frac || 0) * 100);
  const label = s.sub === "download" ? `Downloading model${s.model_mb ? ` (${s.model_mb} MB)` : ""}` : s.sub === "load" ? "Loading model" : s.msg || "Starting";
  const step = waiting ? `Waiting for ${n} other split${n === 1 ? "" : "s"} to finish`
    : s.step ? `Step ${s.step} of ${s.steps} · ${s.msg}` : s.msg || "Starting";
  if ($("#pstep").textContent !== step) $("#pstep").textContent = step;   // a live region: only real changes are read out
  const bar = $("#pbar");
  bar.classList.toggle("busy", busy);
  bar.setAttribute("aria-label", waiting ? "Waiting" : label);
  if (busy) bar.removeAttribute("aria-valuenow"); else bar.setAttribute("aria-valuenow", pct);
  bar.firstElementChild.style.width = busy ? "" : pct + "%";
  $("#plabel").textContent = waiting ? "" : s.sub === "download" ? `${label} · ${pct}%` : s.sub === "load" ? label : busy ? "" : `${pct}%`;
}

/* ------------------------------------------------------------- mixer */
function partsMixerHtml(s) {
  const rows = s.parts.map((p) => `
    <div class="prow" data-p="${esc(p.id)}">
      <span class="pname">${esc(p.label)} <small title="How loud this part is on its own, compared with the whole recording">${p.share_db <= -30 ? "nearly silent" : esc(`${p.share_db} dB`)}</small></span>
      <button class="pmute" data-act="mute" aria-pressed="false" aria-label="Mute ${esc(p.label)}">Mute</button>
      <input type="range" min="-24" max="6" step="1" value="0" aria-label="${esc(p.label)} volume">
      <span class="v"></span>
    </div>`).join("");
  return `<div id="pmixer">
    <div class="row"><div class="seg pseg" role="group" aria-label="Mix">${PART_PRESETS.map((pr, i) => `<button data-act="preset" data-i="${i}">${esc(pr.label)}</button>`).join("")}</div></div>
    <div class="prows">${rows}</div>
    <p class="legend">Separated by AI models from one recording, so a little of each part can be heard in the others.</p>
    <div class="row ptransport">
      <button class="btn" id="pplay" data-act="play">Play</button>
      <span class="ptime" id="ptime">0:00 / 0:00</span>
      <input type="range" id="pseek" min="0" max="0" step="1" value="0" aria-label="Position" disabled>
    </div>
    <div id="pbusy" role="status" aria-live="polite"></div>
    <div class="row">
      <label class="field">Tempo <select id="ptempo">${PART_TEMPOS.map((t) => `<option value="${t}"${t === parts.tempo ? " selected" : ""}>${Math.round(t * 100)}%</option>`).join("")}</select></label>
      <button class="btn ghost" data-act="mix">Download this mix (MP3)</button>
      <button class="btn ghost" data-act="add">Add to session</button>
      <span id="pmixout" role="status" aria-live="polite"></span>
    </div>
  </div>`;
}

function partsMixerSync() {
  document.querySelectorAll("#partsbody .prow").forEach((row) => {
    const p = row.dataset.p, g = parts.gains[p] ?? 0, m = !!parts.muted[p];
    row.classList.toggle("muted", m);
    row.querySelector(".pmute").setAttribute("aria-pressed", m);
    const r = row.querySelector("input");
    if (+r.value !== g) r.value = g;
    r.setAttribute("aria-valuetext", m ? "muted" : dbText(g));
    row.querySelector(".v").textContent = dbText(g);
  });
  const on = partsPresetIndex();
  document.querySelectorAll("#partsbody .pseg button").forEach((b, i) => { b.classList.toggle("on", i === on); b.setAttribute("aria-pressed", i === on); });
}

// Gains and mutes apply live in the browser; only the key and tempo need the server.
function partsMixChanged() {
  const A = partsAudio;
  for (const [p, g] of Object.entries(A.gains)) g.gain.setTargetAtTime(partsLevel(p), A.ctx.currentTime, 0.02);
  partsRoute();
  partsMixStale();
  partsMixerSync();
}
function partsMixStale() {
  parts.mixSeq++;   // an MP3 still being mixed is for the old settings: its answer is dropped
  const o = $("#pmixout"), b = $('#partsbody [data-act="mix"]');
  if (o) o.innerHTML = "";
  b?.removeAttribute("aria-disabled");
}
// The status line under the transport. A focused Retry in it hands the keyboard to Play rather than the page.
function partsBusy(html) {
  const b = $("#pbusy");
  if (!b) return;
  const had = b.contains(document.activeElement);
  b.innerHTML = html;
  if (had) $("#pplay").focus();
}
const partsHint = () => partsBusy(`<p class="hint">Press Play to hear the parts in ${esc(partsWhere(partsTarget()))}.</p>`);

// Every mix goes through the limiter. Even "Full recording" can peak over full scale once shifted (a phase
// vocoder rebuilds the peaks a mastering limiter had pinned: +1.3 dBFS measured at +2 st), and a subset of
// parts can peak higher than the recording. With LIMITER_TRIM the limiter is transparent below -1 dBFS, and the
// dry path (delayed to match it) stays connected so this could switch without a click if ever needed.
function partsRoute() {
  const A = partsAudio, on = true;
  if (!A.dry || A.limited === on) return;
  A.limited = on;
  A.dry.gain.setTargetAtTime(on ? 0 : 1, A.ctx.currentTime, 0.015);
  A.wet.gain.setTargetAtTime(on ? LIMITER_TRIM : 0, A.ctx.currentTime, 0.015);
}

// Re-render on the server when the key or tempo changed (debounced: a few key clicks in a row ask once),
// but only once Play has been pressed for this recording; until then just say what Play will do.
function partsSync(delay = 400) {
  if (!parts.slug || parts.status?.state !== "done" || !parts.status.parts) return;
  const id = partsTarget().id;
  if (id === parts.want) return;
  clearTimeout(parts.renderTimer);
  parts.renderSeq++;   // a render still on its way is for another key or tempo now
  parts.want = id;
  partsMixStale();
  if (id === parts.loaded) { parts.loading = false; partsBusy(""); partsTransport(); return; }
  if (parts.engaged) parts.renderTimer = setTimeout(partsLoad, delay);
  else partsHint();
}

async function partsLoad() {
  clearTimeout(parts.renderTimer);
  if (!state.analysis || state.analysis.error) return;
  const slug = parts.slug, audioI = parts.audioI, t = partsTarget(), seq = ++parts.renderSeq;
  const current = () => parts.slug === slug && parts.audioI === audioI && parts.renderSeq === seq;
  parts.want = t.id;
  parts.loading = true;
  partsTransport();
  partsBusy(spinner(`Moving the parts to ${partsWhere(t)}…`));
  try {
    if (!window.AudioContext) throw new Error("This browser cannot mix audio (no Web Audio).");
    const r = await post(`/api/song/${encodeURIComponent(slug)}/parts/render`, { audio_i: audioI, semitones: t.semis, tempo: t.tempo });
    if (!current()) return;
    partsBusy(spinner("Loading the parts…"));
    const ids = Object.keys(r.urls);
    const files = await Promise.all(ids.map(async (p) => {
      const res = await fetch(r.urls[p]);
      if (!res.ok) throw new Error(`Could not load the ${p} part (${res.status}).`);
      return res.arrayBuffer();
    }));
    if (!current()) return;
    partsDrop(true);   // let the old parts go before decoding: five parts of a long bhajan take a few hundred MB
    const ctx = partsCtx(r.sr);
    const bufs = await Promise.all(files.map((f) => ctx.decodeAudioData(f)));
    if (!current() || partsAudio.ctx !== ctx) return;
    parts.loading = false;
    partsBuild(ids, bufs, r.gain, t.tempo);
    parts.loaded = t.id;
    partsBusy("");
    if (parts.resume) partsPlay();
    parts.at = 0;
    parts.resume = false;
  } catch (e) {
    if (!current() || partsLost(e)) return;
    Object.assign(parts, { loading: false, resume: false });
    partsTransport();
    partsBusy(`<div class="row"><span class="err">${esc(e.message)}</span><button class="btn ghost" data-act="reload">Retry</button></div>`);
  }
}

// The audio context, made at the parts' own rate (decoding at it halves memory for 22.05 kHz recordings),
// with the output stage every set of parts goes through. That stage lives as long as the context: a new
// DynamicsCompressor starts out attenuating and swells in over ~200 ms, which a re-render would make audible.
function partsCtx(sr) {
  const A = partsAudio;
  if (A.ctx && (!sr || A.rate === sr)) return A.ctx;
  partsClose();
  let ctx;
  try { ctx = new AudioContext({ sampleRate: sr }); }
  catch (_) { ctx = new AudioContext(); }
  // The browser can suspend audio (another app took the device, a phone call, a tab policy): stop cleanly.
  ctx.onstatechange = () => {
    if (partsAudio.ctx !== ctx || !partsAudio.playing || ctx.state === "running") return;
    partsPause();
    partsBusy(PARTS_PAUSED);
  };
  const limiter = ctx.createDynamicsCompressor();   // brick-wall safety limiter for every mix but the original
  limiter.threshold.value = -1;
  limiter.knee.value = 0;
  limiter.ratio.value = 20;
  limiter.attack.value = 0.003;
  limiter.release.value = 0.25;
  // The compressor outputs its input late by a fixed look-ahead of 6 ms in whole frames (Chromium, WebKit and
  // Firefox share the code; it is not exposed). Delay the dry path by the same, so while partsRoute crossfades
  // the two copies are sample-aligned; a 6 ms offset between them would comb-filter for the length of the fade.
  const delay = ctx.createDelay(0.1);
  delay.delayTime.value = Math.floor(0.006 * ctx.sampleRate) / ctx.sampleRate;
  const dry = ctx.createGain(), wet = ctx.createGain();
  A.limited = true;                     // see partsRoute: every mix is limited
  dry.gain.value = A.limited ? 0 : 1;
  wet.gain.value = A.limited ? LIMITER_TRIM : 0;
  delay.connect(dry).connect(ctx.destination);
  limiter.connect(wet).connect(ctx.destination);
  Object.assign(A, { ctx, rate: sr, limiter, delay, dry, wet });
  return ctx;
}

function partsBuild(ids, bufs, gain, tempo) {
  const A = partsAudio, ctx = A.ctx;
  const master = ctx.createGain();
  master.gain.value = gain;
  master.connect(A.delay);
  master.connect(A.limiter);
  A.gains = {};
  A.bufs = {};
  ids.forEach((p, i) => {
    const g = ctx.createGain();
    g.gain.value = partsLevel(p);
    g.connect(master);
    A.gains[p] = g;
    A.bufs[p] = bufs[i];
  });
  Object.assign(A, { master, tempo, sources: [], playing: false, duration: Math.max(...bufs.map((b) => b.duration)) });
  A.offset = Math.min(parts.at / tempo, A.duration);
  partsTransport();
}

// Stop and let go of the decoded parts (the context is kept for the next ones).
function partsDrop(keepPlace) {
  const A = partsAudio;
  if (A.bufs && keepPlace) { parts.at = partsPos() * A.tempo; parts.resume = A.playing; }
  partsStopSources();
  cancelAnimationFrame(A.raf);
  A.master?.disconnect();
  Object.assign(A, { bufs: null, gains: {}, master: null, playing: false, offset: 0, duration: 0 });
  parts.loaded = null;
  partsTransport();
}
function partsClose() {
  partsDrop(false);
  partsAudio.ctx?.close().catch(() => {});
  Object.assign(partsAudio, { ctx: null, rate: 0, limiter: null, delay: null, dry: null, wet: null });
}

// server.py's 409 for /parts/render and /parts/mix: the split is gone (cleared from the cache, or made by an
// older version). Go back to the status, which offers Split again, instead of a Retry that cannot work.
function partsLost(e) {
  if (e.message !== "Split the recording into parts first.") return false;
  const had = $("#partscard").contains(document.activeElement);
  partsReset(parts.slug, parts.audioI);
  partsCheck().then(() => { if (had && document.activeElement === document.body) $("#partsbody button")?.focus(); });
  return true;
}

/* ---------------------------------------------------------- transport */
function partsPos() {
  const A = partsAudio;
  if (!A.bufs) return 0;
  return A.playing ? Math.min(A.duration, A.offset + Math.max(0, A.ctx.currentTime - A.t0)) : A.offset;
}

// Play, Pause, or (before anything is loaded) render and decode the parts, then play.
function partsToggle() {
  const A = partsAudio;
  if (A.playing) { partsPause(); return; }
  // WebKit lets audio start only from inside the click: make (or wake) the context now, before anything is awaited.
  if (window.AudioContext) partsCtx(parts.status.sr).resume().catch(() => {});
  if (A.bufs) partsPlay();
  else if (parts.loading) { parts.resume = !parts.resume; partsTransport(); }   // start (or not) once they are in
  else { Object.assign(parts, { engaged: true, resume: true }); partsLoad(); }
}

async function partsPlay() {
  const A = partsAudio, ctx = A.ctx;
  if (!A.bufs || A.playing) return;
  if (ctx.state !== "running") {
    // Start the clock first (from the click, as browsers require) so every part starts on it together.
    // resume() can stay pending without a click, so give up after a second and ask for one.
    await Promise.race([ctx.resume().catch(() => {}), new Promise((ok) => setTimeout(ok, 1000))]);
    if (partsAudio.ctx !== ctx || !A.bufs || A.playing) return;   // moved on meanwhile
    if (ctx.state !== "running") { partsBusy(PARTS_PAUSED); return; }
  }
  if ($("#pbusy [data-paused]")) partsBusy("");
  if (A.offset >= A.duration - 0.05) A.offset = 0;
  // One shared start time and offset for every part keeps them sample-aligned.
  const run = ++A.run, t0 = ctx.currentTime + 0.05;
  let longest = null;
  A.sources = Object.keys(A.bufs).map((p) => {
    const src = ctx.createBufferSource();
    src.buffer = A.bufs[p];
    src.connect(A.gains[p]);
    src.start(t0, Math.min(A.offset, src.buffer.duration));
    if (!longest || src.buffer.duration > longest.buffer.duration) longest = src;
    return src;
  });
  longest.onended = () => {
    if (A.run !== run) return;   // stopped by pause or seek, not the end
    A.sources = [];
    A.playing = false;
    A.offset = 0;
    partsTransport();
  };
  Object.assign(A, { t0, playing: true });
  partsTransport();
  cancelAnimationFrame(A.raf);
  const step = () => { partsTransport(); if (A.playing) A.raf = requestAnimationFrame(step); };
  A.raf = requestAnimationFrame(step);
}

function partsStopSources() {
  const A = partsAudio;
  A.run++;
  for (const s of A.sources) { try { s.stop(); } catch (_) {} s.disconnect(); }
  A.sources = [];
}

function partsPause() {
  const A = partsAudio;
  if (!A.playing) return;
  A.offset = partsPos();
  partsStopSources();
  A.playing = false;
  partsTransport();
}

function partsSeek(t) {
  const A = partsAudio, was = A.playing;
  if (!A.bufs) return;
  if (was) { partsStopSources(); A.playing = false; }
  A.offset = Math.max(0, Math.min(t, A.duration));
  if (was) partsPlay(); else partsTransport();
}

function partsTransport() {
  const A = partsAudio, btn = $("#pplay"), seek = $("#pseek");
  if (!btn) return;
  btn.textContent = A.playing || (!A.bufs && parts.loading && parts.resume) ? "Pause" : "Play";
  const pos = parts.seeking ? +seek.value : partsPos();
  $("#ptime").textContent = `${partsTime(pos)} / ${partsTime(A.duration)}`;
  seek.disabled = !A.bufs;
  seek.max = A.duration || 0;
  if (!parts.seeking) seek.value = pos;
  const vt = `${partsTime(pos)} of ${partsTime(A.duration)}`;
  if (seek.getAttribute("aria-valuetext") !== vt) seek.setAttribute("aria-valuetext", vt);
}

/* ------------------------------------------------------------ download */
async function partsDownload(btn) {
  const slug = parts.slug, t = partsTarget(), seq = parts.mixSeq;
  const name = `${slug}-${KEY_NAMES[state.pc]}-${partsPresetLabel().toLowerCase().replace(/[^a-z0-9]+/g, "-")}.mp3`;
  const current = () => parts.slug === slug && parts.mixSeq === seq;
  btn.setAttribute("aria-disabled", "true");   // not disabled, which would drop the keyboard to the page
  $("#pmixout").innerHTML = spinner("Mixing the MP3…");
  try {
    const r = await post(`/api/song/${encodeURIComponent(slug)}/parts/mix`, { audio_i: parts.audioI, semitones: t.semis, tempo: t.tempo, gains: partsGainsReq() });
    if (!current()) return;
    $("#pmixout").innerHTML = `<a class="dl" href="${esc(r.audio_url)}" download="${esc(name)}">Save ${esc(name)}</a>`;
  } catch (e) {
    if (!current() || partsLost(e)) return;
    $("#pmixout").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  } finally { if (current()) btn.removeAttribute("aria-disabled"); }   // a changed mix re-enabled it already (partsMixStale)
}

$("#partsbody").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b || b.disabled || b.getAttribute("aria-disabled") === "true") return;
  const act = b.dataset.act;
  if (act === "split") partsCheck("POST");
  else if (act === "cancel") partsCheck("DELETE");
  else if (act === "check") partsCheck();
  else if (act === "reload") partsLoad();
  else if (act === "play") partsToggle();
  else if (act === "preset") {
    const pr = PART_PRESETS[+b.dataset.i];
    for (const p of PART_IDS) { parts.muted[p] = !pr.on.includes(p); if (!parts.muted[p]) parts.gains[p] = 0; }
    partsMixChanged();
  } else if (act === "mute") {
    const p = b.closest(".prow").dataset.p;
    parts.muted[p] = !parts.muted[p];
    partsMixChanged();
  } else if (act === "mix") partsDownload(b);
  else if (act === "add") sessionAdd();
});
$("#partsbody").addEventListener("input", (e) => {
  const t = e.target, row = t.closest(".prow");
  if (t.id === "pseek") { parts.seeking = true; partsTransport(); }
  else if (row) { parts.gains[row.dataset.p] = +t.value; partsMixChanged(); }
});
$("#partsbody").addEventListener("change", (e) => {
  const t = e.target;
  if (t.id === "pseek") { parts.seeking = false; partsSeek(+t.value); }
  else if (t.id === "ptempo") { parts.tempo = +t.value; partsSync(); }
});

/* ------------------------------------------------------------- session */
// Kept in localStorage, which every tab of the app shares: read again before each change, and redrawn
// when another tab saves it.
// Parts the server knows, from a stored item. Sessions saved when lead and group were separate parts map onto
// the singing: it is muted only if both were.
function sessionGains(g) {
  if (!g || typeof g !== "object") return {};
  const out = Object.fromEntries(PART_IDS.filter((p) => p in g).map((p) => [p, g[p] === null ? null : +g[p] || 0]));
  if (!("voice" in out) && ("lead" in g || "group" in g)) {
    const v = [g.lead, g.group].filter((x) => x !== undefined);
    out.voice = v.every((x) => x === null) ? null : Math.max(...v.filter((x) => x !== null).map((x) => +x || 0));
  }
  return out;
}
const sessionItem = (it) => ({
  slug: it.slug, audio_i: it.audio_i, title: String(it.title || it.slug), semitones: +it.semitones || 0, tempo: +it.tempo || 1,
  gains: sessionGains(it.gains), keyLabel: String(it.keyLabel || ""), preset: String(it.preset || "Custom"),
  recLabel: String(it.recLabel || "") });   // which recording, when the bhajan has more than one
function sessionRead() {   // the stored session, or null if there is none (or no storage)
  try {
    const s = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
    if (!s || !Array.isArray(s.items)) return null;
    return { name: typeof s.name === "string" ? s.name.slice(0, 120) : "Bhajan session", gap: [2, 4, 6, 8].includes(s.gap) ? s.gap : 4,
      items: s.items.filter((it) => it && typeof it.slug === "string" && Number.isInteger(it.audio_i)).slice(0, 40).map(sessionItem) };
  } catch (_) { return null; }
}
const session = { name: "Bhajan session", gap: 4, items: [], ...sessionRead() };
const sessionReload = () => Object.assign(session, sessionRead());   // keeps what we have if nothing is stored
let sessionSeq = 0, sessionJob = null;   // the id of the export being waited for
// Fire and forget: the server stops an export nobody waits for before its next bhajan, so the next can start.
const sessionCancel = (id) => fetch(`/api/session/export/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});

function sessionSave() { try { localStorage.setItem(SESSION_KEY, JSON.stringify(session)); } catch (_) {} }

function sessionRender() {
  const n = session.items.length;
  $("#sessioncard").hidden = !n;
  $("#sesslist").innerHTML = session.items.map((it, i) => {
    const t = esc(it.title + (it.recLabel ? ` (${it.recLabel})` : ""));
    return `
    <li><span class="t">${i + 1}. ${t} · ${esc(it.keyLabel)} · ${esc(it.preset)} · ${Math.round(it.tempo * 100)}%</span>
      <button class="ibtn" data-act="up" data-i="${i}" aria-label="Move ${t} up"${i === 0 ? " disabled" : ""}>↑</button>
      <button class="ibtn" data-act="down" data-i="${i}" aria-label="Move ${t} down"${i === n - 1 ? " disabled" : ""}>↓</button>
      <button class="ibtn" data-act="remove" data-i="${i}" aria-label="Remove ${t}">✕</button></li>`;
  }).join("");
}

// Any change to the list or the gap makes an exported file out of date.
function sessionStale() {
  sessionSeq++;
  if (sessionJob) sessionCancel(sessionJob);
  sessionJob = null;
  $("#sessout").innerHTML = "";
  $("#sessexport").removeAttribute("aria-disabled");
}
function sessionChanged() {
  sessionSave();
  sessionRender();
  sessionStale();
}

function sessionAdd() {
  if (PART_IDS.every((p) => parts.muted[p])) { toast("Every part is muted. Unmute at least one first."); return; }
  sessionReload();
  if (session.items.length >= 40) { toast("A session can hold up to 40 bhajans."); return; }
  const t = partsTarget(), audio = state.song.audio;
  session.items.push(sessionItem({ slug: state.slug, audio_i: state.audioI, title: state.song.title, semitones: t.semis, tempo: t.tempo,
    gains: partsGainsReq(), keyLabel: keyName(state.pc), preset: partsPresetLabel(), recLabel: audio.length > 1 ? audio[state.audioI]?.name : "" }));
  sessionChanged();
  const n = session.items.length;
  toast(`Added to the session (${n} bhajan${n === 1 ? "" : "s"}).`);
}

async function sessionExport() {
  const seq = ++sessionSeq, btn = $("#sessexport"), out = $("#sessout");
  const current = () => sessionSeq === seq;
  const name = session.name.trim() || "Bhajan session";
  const items = session.items.map(({ slug, audio_i, title, semitones, tempo, gains, recLabel }) =>
    ({ slug, audio_i, title: (recLabel ? `${title} (${recLabel})` : title).slice(0, 200), semitones, tempo, gains }));
  const say = (text) => { if (out.textContent !== text) out.innerHTML = spinner(text); };   // a live region: only real changes
  btn.setAttribute("aria-disabled", "true");   // not disabled, which would drop the keyboard to the page
  say("Starting the export…");
  try {
    let job = await post("/api/session/export", { name, gap: session.gap, items }), polls = 0;
    if (!current()) {   // changed meanwhile; a newer export of the same session may share this job: leave that one be
      if ((job.state === "queued" || job.state === "running") && job.id !== sessionJob) sessionCancel(job.id);
      return;
    }
    sessionJob = job.id;
    while (job.state === "queued" || job.state === "running") {
      say(job.state === "queued" ? (polls++ ? "Waiting for another export to finish…" : "Starting the export…")
        : job.done < job.total ? `Rendering ${job.done + 1} of ${job.total}: ${items[job.done]?.title || job.msg}` : "Encoding the MP3…");
      await new Promise((ok) => setTimeout(ok, 1000));
      if (!current()) return;
      job = await api(`/api/session/export/${encodeURIComponent(job.id)}`);
      if (!current()) return;
    }
    if (!current()) return;
    if (job.state !== "done") throw new Error(job.error || (job.state === "cancelled" ? "The export was stopped. Export again." : "The export failed."));
    const file = name.replace(/[\\/:*?"<>|]+/g, "-") + ".mp3";
    out.innerHTML = `
      <div class="player"><div class="lbl"><span>${esc(name)} · ${partsTime(job.seconds)}</span><a class="dl" href="${esc(job.audio_url)}" download="${esc(file)}">Download ${esc(file)}</a></div>
        <audio controls preload="metadata" src="${esc(job.audio_url)}"></audio></div>
      <ol class="sess-tracks">${job.tracks.map((tr) => `<li><button class="tstart" data-t="${tr.start}" title="Play from here">${partsTime(tr.start)}</button> ${esc(tr.title)}</li>`).join("")}</ol>`;
  } catch (e) {
    if (!current()) return;
    out.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  } finally {
    if (current()) { sessionJob = null; btn.removeAttribute("aria-disabled"); }
  }
}

function sessionInit() {
  $("#sessionbody").innerHTML = `
    <ol class="sess-list" id="sesslist"></ol>
    <div class="row">
      <label class="field">Name <input type="text" id="sessname" maxlength="120"></label>
      <label class="field">Gap <select id="sessgap">${[2, 4, 6, 8].map((g) => `<option value="${g}">${g} s</option>`).join("")}</select></label>
      <button class="btn" id="sessexport">Export session (one MP3)</button>
    </div>
    <div id="sessout" role="status" aria-live="polite"></div>`;
  $("#sessname").value = session.name;
  $("#sessgap").value = String(session.gap);
  $("#sessname").addEventListener("input", (e) => { sessionReload(); session.name = e.target.value; sessionSave(); sessionRender(); });
  $("#sessgap").addEventListener("change", (e) => { sessionReload(); session.gap = +e.target.value; sessionChanged(); });
  $("#sessexport").addEventListener("click", (e) => { if (e.currentTarget.getAttribute("aria-disabled") !== "true") sessionExport(); });
  $("#sesslist").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const shown = JSON.stringify(session.items[+b.dataset.i]), act = b.dataset.act;
    sessionReload();
    // Another tab may have changed the list since it was drawn: act on the same bhajan if it is still there.
    const its = session.items, i = JSON.stringify(its[+b.dataset.i]) === shown ? +b.dataset.i : its.findIndex((it) => JSON.stringify(it) === shown);
    if (i < 0) { sessionChanged(); return; }
    const j = act === "up" ? i - 1 : act === "down" ? i + 1 : i;
    if (act === "remove") its.splice(i, 1);
    else if (j >= 0 && j < its.length) [its[i], its[j]] = [its[j], its[i]];
    sessionChanged();
    const li = $("#sesslist").children[Math.min(j, its.length - 1)];   // keep the keyboard on the moved item
    const next = li?.querySelector(`[data-act="${act}"]:not(:disabled)`) || li?.querySelector("button:not(:disabled)")
      || $('#partsbody [data-act="add"]') || $("#q");   // the list is gone: somewhere that stays
    next?.focus();
  });
  $("#sessout").addEventListener("click", (e) => {
    const b = e.target.closest(".tstart"), au = $("#sessout audio");
    if (!b || !au) return;
    au.currentTime = +b.dataset.t;
    au.play().catch(() => {});
  });
  window.addEventListener("storage", (e) => {
    if (e.key !== SESSION_KEY && e.key !== null) return;
    const was = JSON.stringify([session.items, session.gap]);
    sessionReload();
    if (document.activeElement !== $("#sessname")) $("#sessname").value = session.name;
    $("#sessgap").value = String(session.gap);
    sessionRender();
    if (JSON.stringify([session.items, session.gap]) !== was) sessionStale();
  });
  // Leaving the page (reload, close) stops the export it was waiting for, so it does not hold up the next one.
  window.addEventListener("pagehide", () => {
    if (sessionJob) fetch(`/api/session/export/${encodeURIComponent(sessionJob)}`, { method: "DELETE", keepalive: true }).catch(() => {});
  });
  sessionRender();
}
sessionInit();
