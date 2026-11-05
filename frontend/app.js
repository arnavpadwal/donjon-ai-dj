/* DonJon — a simple shared music player with a pro DJ behind the curtain. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const fmt = (s) => {
  s = Math.max(0, Number(s) || 0);
  return `${Math.floor(s / 60)}:${Math.floor(s % 60).toString().padStart(2, "0")}`;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const icon = (name, cls = "") => `<svg class="icon ${cls}"><use href="#i-${name}"></use></svg>`;

const isLocalHost = ["localhost", "127.0.0.1", "::1"].includes(location.hostname);
const hostParam = new URLSearchParams(location.search).get("host");
const S = {
  data: null, cid: null,
  host: hostParam === null ? isLocalHost : ["1", "true", "yes"].includes(hostParam.toLowerCase()),
  name: localStorage.getItem("donjon_name") || "",
  joined: false,
  roomCode: "------",
  page: "home",
  library: [],
  libraryQuery: "",
  librarySource: "all",
  searchTab: "youtube",
  searchQuery: "",
  searchBusy: false,
  searchResults: { library: [], youtube: [], spotify: [] },
  spotify: null,
  ytPreview: null,
  spPreview: null,
  importTab: "youtube",
  queueSig: "",
  jobsSeen: {},
  roomWs: null,
  audioWs: null,
  audioCtx: null,
  gain: null,
  audioEnabled: false,
  baseTs: null,
  baseCtx: null,
  lastFrameTs: null,
  lastScheduledEnd: null,
  lastFrameSeq: -1,
  sources: [],
  clockOffset: 0,
  rtt: 0,
  bestRtt: 1e9,
  deviceOffset: Number(localStorage.getItem("donjon_delay") || 0),
  vol: Number(localStorage.getItem("donjon_vol") || 82) / 100,
  lastStateAt: performance.now(),
  reconnectTimer: null,
  audioReconnectTimer: null,
  pingTimer: null,
};

/* ------------------------------------------------------------------ basics */
function toast(message, isError = false) {
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", !!isError);
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 3400);
}
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
  return data;
}
function setConn(text, ok) {
  $("#ai-pill").classList.toggle("off", !ok);
  const label = $("#ai-pill span:last-child");
  if (label) label.textContent = ok ? "AI DJ" : "Offline";
  $("#ai-pill").title = text;
}
function copyText(text) {
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("Copied")).catch(() => toast(text));
  else toast(text);
}
function sourceLabel(source) {
  return ({ youtube: "YouTube Music", spotify: "Spotify", import: "Local", local: "Local", demo: "Demo" })[source] || (source || "Local");
}
function artHtml(art, cls = "", iconName = "music") {
  return art
    ? `<img class="${cls}" src="${esc(art)}" alt="" loading="lazy" onerror="this.style.display='none';this.parentElement.classList.add('art-missing')">`
    : `<span class="art-fallback">${icon(iconName)}</span>`;
}

/* --------------------------------------------------------------- room socket */
function connectRoom() {
  if (!S.joined) return;
  if (S.roomWs && [WebSocket.OPEN, WebSocket.CONNECTING].includes(S.roomWs.readyState)) return;
  if (!S.roomCode || S.roomCode === "------") { setTimeout(connectRoom, 500); return; }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/room/${encodeURIComponent(S.roomCode)}` +
    `?name=${encodeURIComponent(S.name || "Guest")}&host=${S.host ? 1 : 0}&code=${encodeURIComponent(S.roomCode)}`;
  const ws = new WebSocket(url);
  S.roomWs = ws;
  setConn("connecting", false);
  ws.onopen = () => {
    setConn("live", true);
    clearInterval(S.pingTimer);
    S.pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "PING", t0: performance.now() }));
    }, 3000);
    ws.send(JSON.stringify({ type: "PING", t0: performance.now() }));
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "HELLO") {
      S.cid = msg.cid;
      S.host = !!msg.host;
      if (msg.state) applyState(msg.state);
      renderAll();
    } else if (msg.type === "STATE") {
      applyState(msg.state);
    } else if (msg.type === "PONG") {
      const t1 = performance.now();
      const rtt = Math.max(0, t1 - Number(msg.t0 || t1));
      const offset = Number(msg.server_ts) + rtt / 2 - t1;
      S.rtt = rtt;
      if (rtt <= S.bestRtt + 4) { S.bestRtt = Math.min(S.bestRtt, rtt); S.clockOffset = offset; }
      renderSync();
    } else if (msg.type === "DENIED") {
      toast(msg.message || "Host has locked that control", true);
    } else if (msg.type === "ERROR") {
      toast(msg.message || "Room error", true);
    }
  };
  ws.onclose = () => {
    setConn("offline", false);
    clearInterval(S.pingTimer);
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = setTimeout(connectRoom, 2200);
  };
  ws.onerror = () => setConn("offline", false);
}

function sendAction(action, extra = {}) {
  if (!S.roomWs || S.roomWs.readyState !== WebSocket.OPEN) {
    toast("Connecting to the room…", true);
    return false;
  }
  S.roomWs.send(JSON.stringify({ type: "ACTION", action, ...extra }));
  return true;
}

/* -------------------------------------------------------------------- state */
function applyState(data) {
  if (!data) return;
  S.data = data;
  S.roomCode = String(data.room || S.roomCode);
  $("#room-code").textContent = S.roomCode;
  $("#join-code").textContent = S.roomCode;
  $("#drawer-room-code").textContent = S.roomCode;
  $("#side-room-code").textContent = S.roomCode;
  S.lastStateAt = performance.now();
  if (S.joined) renderAll();
}

function renderAll() {
  if (!S.data) return;
  renderMode();
  renderHeader();
  renderQueue();
  renderNow(S.data.engine || {});
  renderSync();
  renderJobs(S.data.jobs || []);
  updatePlayingRows();
}

function renderMode() {
  const ready = !!S.data?.engine?.ready;
  const playing = !!S.data?.engine?.decks?.[String(S.data?.engine?.current_deck || 1)]?.playing;
  setConn(ready ? "live" : (S.data?.engine?.error || "booting"), ready);
  const label = $("#ai-pill span:last-child");
  if (label) label.textContent = ready ? (playing ? "AI DJ live" : "AI DJ") : "AI DJ booting";
}

function renderHeader() {
  const d = S.data;
  if (!d) return;
  $("#side-room-meta").textContent = `${(d.members || []).length} listening · ${(d.queue || []).length} in queue`;
  $("#home-listener-count").textContent = (d.members || []).length || 1;
  $("#home-library-count").textContent = S.library.length || 0;
  $("#home-queue-count").textContent = (d.queue || []).length || 0;
  $("#home-sync").textContent = S.audioEnabled ? `synced · ${d.audio?.clients || 0} device${(d.audio?.clients || 0) === 1 ? "" : "s"}` : "audio off";
  $("#home-sync").classList.toggle("off", !S.audioEnabled);
  if (d.network?.url) $("#drawer-url").textContent = d.network.url;
  renderMembers(d.members || []);
  $("#host-lock-switch").classList.toggle("on", !!d.host_lock);
  $("#host-lock-switch").style.opacity = S.host ? "1" : "0.55";
  $("#side-queue-count").textContent = (d.queue || []).length || "";
}

function renderMembers(members) {
  $("#member-count").textContent = `(${members.length})`;
  $("#member-list").innerHTML = members.length ? members.map((m) => `
    <div class="member-row"><span>${esc(m.name)} ${m.id === S.cid ? '<span class="muted">(you)</span>' : ""}</span>${m.host ? '<span class="host-badge">HOST</span>' : ""}</div>`).join("") : '<div class="empty" style="padding:12px">Waiting for friends…</div>';
}

/* -------------------------------------------------------------------- queue */
function queueItem(q, i) {
  return `<div class="queue-item ${q.priority ? "priority" : ""}">
    <div class="queue-art">${artHtml(q.track.artwork, "queue-img")}<span class="queue-num">${i + 1}</span></div>
    <div class="queue-main">
      <div class="queue-title">${esc(q.track.title)}${q.priority ? '<span class="queue-priority">NEXT</span>' : ""}</div>
      <div class="queue-sub">${esc(q.track.artist || "Unknown")}</div>
    </div>
    <div class="queue-actions">
      <button class="queue-action" data-qpriority="${esc(q.uid)}" title="Play next">${icon("arrow-up")}</button>
      <button class="queue-action" data-qmove="-1" data-uid="${esc(q.uid)}" title="Move up">${icon("up")}</button>
      <button class="queue-action" data-qmove="1" data-uid="${esc(q.uid)}" title="Move down">${icon("down")}</button>
      <button class="queue-action danger" data-qremove="${esc(q.uid)}" title="Remove">${icon("trash")}</button>
    </div>
  </div>`;
}

function queueEmpty(text = "The queue is empty.") {
  return `<div class="empty queue-empty"><strong>${text}</strong>Add songs or import a playlist and the AI DJ will mix them.<button class="primary-btn" data-open-import="youtube">Import a playlist</button></div>`;
}

function renderQueue() {
  const queue = S.data?.queue || [];
  const sig = queue.map((q) => `${q.uid}:${q.priority}:${q.track.id}`).join("|");
  $("#home-queue-count").textContent = queue.length || 0;
  $("#side-queue-count").textContent = queue.length || "";
  $("#queue-count").textContent = queue.length ? `(${queue.length})` : "";
  $("#queue-sub").textContent = queue.length
    ? `${queue.length} track${queue.length === 1 ? "" : "s"} waiting. The AI DJ follows this order.`
    : "Add songs and the AI DJ will mix them into one continuous set.";
  $("#nav-queue-dot").classList.toggle("show", queue.length > 0);
  if (sig === S.queueSig) return;
  S.queueSig = sig;
  const empty = queueEmpty();
  $("#queue-list").innerHTML = queue.length ? queue.slice(0, 40).map(queueItem).join("") : empty;
  $("#home-queue").innerHTML = queue.length ? queue.slice(0, 5).map(queueItem).join("") : empty;
  $("#queue-full").innerHTML = queue.length ? queue.map(queueItem).join("") : empty;
}

/* -------------------------------------------------------------------- songs */
function songRow(t, i, scope = "library") {
  const playing = isPlaying(t.id);
  return `<div class="song-row ${playing ? "playing" : ""}" data-track="${t.id}">
    <div class="song-art">${artHtml(t.artwork)}<span class="song-playing-mark">${icon("play")}</span></div>
    <div class="song-main"><div class="song-title">${esc(t.title)}</div><div class="song-artist">${esc(t.artist || "Unknown artist")}</div></div>
    <div class="song-meta"><span>${esc(sourceLabel(t.source))}</span></div>
    <div class="song-dur">${fmt(t.duration)}</div>
    <div class="song-actions">
      <button class="song-action accent" data-play="${t.id}" title="Play now">${icon("play")}</button>
      <button class="song-action" data-qnext="${t.id}" title="Play next">${icon("arrow-up")}</button>
      <button class="song-action" data-queue="${t.id}" title="Add to queue">${icon("plus")}</button>
    </div>
  </div>`;
}

function isPlaying(id) {
  const e = S.data?.engine;
  if (!e) return false;
  return [1, 2].some((n) => {
    const d = e.decks?.[String(n)];
    return d?.track?.id === id && d.playing;
  });
}

function filteredLibrary() {
  let out = S.library;
  if (S.librarySource !== "all") out = out.filter((t) => t.source === S.librarySource || (S.librarySource === "local" && ["local", "import"].includes(t.source)));
  if (S.libraryQuery) {
    const q = S.libraryQuery.toLowerCase();
    out = out.filter((t) => `${t.title} ${t.artist} ${t.album} ${t.camelot}`.toLowerCase().includes(q));
  }
  return out;
}

function renderLibrary() {
  const tracks = filteredLibrary();
  $("#library-sub").textContent = `${S.library.length} songs · ready to mix`;
  $("#library-list").innerHTML = tracks.length ? tracks.map((t, i) => songRow(t, i)).join("") : `<div class="empty"><strong>Your library is empty</strong>Import a playlist or search YouTube Music to add songs.<button class="primary-btn" data-open-import="youtube">Import a playlist</button></div>`;
  $("#recent-list").innerHTML = S.library.slice(0, 6).map((t, i) => songRow(t, i)).join("") || `<div class="empty" style="padding:16px">New songs will appear here.</div>`;
  renderQuickPicks();
  updatePlayingRows();
}

function renderQuickPicks() {
  const picks = S.library.slice(0, 8);
  $("#quick-picks").innerHTML = picks.length ? picks.map((t) => `
    <button class="pick-card" data-play="${t.id}">
      <div class="pick-art">${artHtml(t.artwork)}</div>
      <div class="pick-title">${esc(t.title)}</div>
      <div class="pick-sub">${esc(t.artist || "Unknown")}</div>
    </button>`).join("") : `<div class="empty" style="grid-column:1/-1"><strong>No songs yet</strong>Import a YouTube Music or Spotify playlist to start the party.<button class="primary-btn" data-open-import="youtube">Import a playlist</button></div>`;
}

async function loadLibrary() {
  try {
    const data = await api("/api/library?limit=1000");
    S.library = data.tracks || [];
    renderLibrary();
    renderAll();
  } catch (e) { toast(`Library: ${e.message}`, true); }
}

function updatePlayingRows() {
  $$("[data-track]").forEach((row) => {
    const playing = isPlaying(Number(row.dataset.track));
    row.classList.toggle("playing", playing);
    const mark = row.querySelector(".song-playing-mark");
    if (mark) mark.innerHTML = playing ? icon("play") : "";
  });
}

/* -------------------------------------------------------------------- player */
function currentDeck(engine) {
  const n = engine?.current_deck || 1;
  return { n, d: engine?.decks?.[String(n)] || { track: null, playing: false, position: 0, duration: 0 } };
}

function renderNow(engine) {
  const { d } = currentDeck(engine);
  const t = d.track;
  if (!t) {
    $("#now-title").textContent = "Nothing playing yet";
    $("#now-sub").textContent = "Import a playlist or add songs to the queue";
    $("#now-art").innerHTML = icon("music");
    $("#play-btn").innerHTML = icon("play");
    $("#progress-dur").textContent = "0:00";
    return;
  }
  $("#now-title").textContent = t.title || "Untitled";
  $("#now-sub").textContent = t.artist || "Unknown";
  $("#now-art").innerHTML = artHtml(t.artwork);
  $("#play-btn").innerHTML = icon(d.playing ? "pause" : "play");
  $("#progress-dur").textContent = fmt(d.duration);
}

function renderSync() {
  const audio = S.data?.audio || {};
  $("#clock-offset").textContent = S.clockOffset ? "locked" : "—";
  $("#clock-rtt").textContent = S.rtt ? `${Math.round(S.rtt)} ms` : "—";
  $("#delay-stat").textContent = `${S.deviceOffset} ms`;
  $("#audio-clients").textContent = S.audioEnabled ? `${audio.clients || 0}` : "off";
  $("#home-sync").textContent = S.audioEnabled ? `synced · ${audio.clients || 0} device${(audio.clients || 0) === 1 ? "" : "s"}` : "audio off";
  $("#home-sync").classList.toggle("off", !S.audioEnabled);
  $("#sync-badge").classList.toggle("ok", S.audioEnabled && audio.ready);
  $("#sync-text").textContent = S.audioEnabled ? (audio.ready ? `synced · ${audio.clients || 0}` : "waiting") : "audio off";
  $("#sound-btn").classList.toggle("off", !S.audioEnabled);
  $("#sound-btn").innerHTML = `${icon(S.audioEnabled ? "volume" : "mute")}<span>${S.audioEnabled ? "Sound on" : "Sound"}</span>`;
  $("#top-sound-btn").innerHTML = icon(S.audioEnabled ? "volume" : "mute");
  $("#enable-audio-btn").innerHTML = `${icon("volume")} ${S.audioEnabled ? "Sound is on" : "Sound on this device"}`;
}

function tick() {
  const engine = S.data?.engine;
  if (!engine) return;
  const { d } = currentDeck(engine);
  let pos = Number(d.position || 0);
  if (d.playing) pos += Math.max(0, (performance.now() - S.lastStateAt) / 1000);
  const dur = Number(d.duration || 0);
  pos = Math.min(pos, dur || pos);
  $("#progress-fill").style.width = `${dur ? Math.min(100, (pos / dur) * 100) : 0}%`;
  $("#progress-cur").textContent = fmt(pos);
  $("#progress-dur").textContent = fmt(dur);
}

/* -------------------------------------------------------------------- audio */
async function enableAudio() {
  try {
    if (!S.audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) throw new Error("Web Audio is not available in this browser");
      S.audioCtx = new Ctx({ latencyHint: "playback" });
      S.gain = S.audioCtx.createGain();
      S.gain.gain.value = S.vol;
      S.gain.connect(S.audioCtx.destination);
    }
    if (S.audioCtx.state !== "running") await S.audioCtx.resume();
    if (S.audioCtx.state !== "running") throw new Error("Tap Sound to enable audio");
    S.audioEnabled = true;
    connectAudio();
    renderSync();
    return true;
  } catch (e) {
    S.audioEnabled = false;
    renderSync();
    return false;
  }
}

function connectAudio() {
  if (!S.audioEnabled || !S.joined) return;
  if (S.audioWs && [WebSocket.OPEN, WebSocket.CONNECTING].includes(S.audioWs.readyState)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/audio/${encodeURIComponent(S.roomCode)}`);
  ws.binaryType = "arraybuffer";
  S.audioWs = ws;
  ws.onopen = () => {
    S.baseTs = null; S.baseCtx = null; S.lastFrameTs = null;
    S.lastScheduledEnd = null; S.lastFrameSeq = -1;
  };
  ws.onmessage = (ev) => { if (ev.data instanceof ArrayBuffer) schedulePcm(ev.data); };
  ws.onclose = () => {
    clearTimeout(S.audioReconnectTimer);
    if (S.audioEnabled) S.audioReconnectTimer = setTimeout(connectAudio, 1500);
  };
  ws.onerror = () => {};
}

function schedulePcm(buffer) {
  if (!S.audioCtx || buffer.byteLength < 20) return;
  const dv = new DataView(buffer);
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== "DONJ") return;
  const seq = dv.getUint32(4, true);
  const ts = dv.getFloat64(8, true);
  if (seq === S.lastFrameSeq) return;
  S.lastFrameSeq = seq;
  const frames = (buffer.byteLength - 16) / 4;
  if (frames <= 0) return;
  const pcm = new Int16Array(buffer, 16, frames * 2);
  const ab = S.audioCtx.createBuffer(2, frames, 48000);
  const L = ab.getChannelData(0);
  const R = ab.getChannelData(1);
  for (let i = 0; i < frames; i++) {
    L[i] = pcm[i * 2] / 32768;
    R[i] = pcm[i * 2 + 1] / 32768;
  }
  const ctxNow0 = S.audioCtx.currentTime;
  if (S.lastFrameTs !== null && (ts < S.lastFrameTs - 100 || ts - S.lastFrameTs > 5000)) {
    S.baseTs = null; S.lastScheduledEnd = null;
  }
  S.lastFrameTs = ts;
  if (S.baseTs === null) {
    const now = performance.now();
    const lead = S.clockOffset ? Math.max(0.35, Math.min(1.3, (ts + 800 - (now + S.clockOffset)) / 1000)) : 0.8;
    S.baseTs = ts;
    S.baseCtx = ctxNow0 + lead;
  }
  let playAt = S.baseCtx + (ts - S.baseTs) / 1000 + S.deviceOffset / 1000;
  if (S.lastScheduledEnd !== null && playAt < S.lastScheduledEnd - 0.002) playAt = S.lastScheduledEnd;
  if (playAt < ctxNow0 - 0.4) {
    S.baseTs = ts; S.baseCtx = ctxNow0 + 0.55;
    S.lastScheduledEnd = null;
    playAt = S.baseCtx + S.deviceOffset / 1000;
  }
  if (playAt > ctxNow0 + 2.5) return;
  const src = S.audioCtx.createBufferSource();
  src.buffer = ab;
  src.connect(S.gain);
  src.start(playAt);
  S.lastScheduledEnd = playAt + ab.duration;
  S.sources.push(src);
  src.onended = () => {
    const i = S.sources.indexOf(src);
    if (i >= 0) S.sources.splice(i, 1);
  };
  if (S.sources.length > 300) S.sources.splice(0, S.sources.length - 300);
}

function resyncAudio() {
  for (const src of S.sources) { try { src.stop(); } catch { /* ended */ } }
  S.sources = [];
  S.baseTs = null; S.baseCtx = null; S.lastFrameTs = null; S.lastScheduledEnd = null;
}

/* -------------------------------------------------------------------- search */
function resultRow(item, source) {
  const play = source === "library" ? `<button class="primary-btn" data-result="now" data-source="library" data-id="${item.id}" data-title="${esc(item.title)}" data-artist="${esc(item.artist || "")}" data-artwork="${esc(item.artwork || "")}">${icon("play")} Play</button>` : `<button class="primary-btn" data-result="now" data-source="${source}" data-id="${esc(item.id || "")}" data-title="${esc(item.title)}" data-artist="${esc(item.artist || "")}" data-artwork="${esc(item.artwork || "")}">${icon("play")} Play</button>`;
  return `<div class="result-row">
    <div class="result-art">${artHtml(item.artwork)}</div>
    <div><div class="result-title">${esc(item.title)}<span class="result-source ${source}">${source === "library" ? sourceLabel(item.source) : sourceLabel(source)}</span></div><div class="result-sub">${esc(item.artist || "Unknown")}${item.duration ? " · " + fmt(item.duration) : ""}</div></div>
    <div class="result-actions">${play}<button class="ghost-btn" data-result="next" data-source="${source}" data-id="${esc(item.id || "")}" data-title="${esc(item.title)}" data-artist="${esc(item.artist || "")}" data-artwork="${esc(item.artwork || "")}">${icon("arrow-up")} Next</button><button class="ghost-btn" data-result="queue" data-source="${source}" data-id="${esc(item.id || "")}" data-title="${esc(item.title)}" data-artist="${esc(item.artist || "")}" data-artwork="${esc(item.artwork || "")}">${icon("plus")} Queue</button></div>
  </div>`;
}

function renderSearch() {
  const box = $("#search-results");
  if (S.searchBusy) {
    box.innerHTML = `<div class="search-note"><span class="spinner"></span><strong>Searching…</strong></div>`;
    return;
  }
  const q = S.searchQuery;
  if (!q) {
    box.innerHTML = `<div class="search-note"><strong>Find your next song</strong>Search YouTube Music, Spotify or your library. Every result can play now, play next or join the queue.</div>`;
    return;
  }
  let rows = [];
  if (S.searchTab === "youtube") rows = S.searchResults.youtube.map((x) => resultRow(x, "youtube"));
  else if (S.searchTab === "spotify") rows = S.searchResults.spotify.map((x) => resultRow(x, "spotify"));
  else rows = S.searchResults.library.map((x) => resultRow(x, "library"));
  if (!S.spotify?.configured && S.searchTab === "spotify") {
    box.innerHTML = `<div class="search-note"><strong>Spotify is not connected</strong>Add your free developer credentials once; they persist across refreshes.<button class="primary-btn" data-open-import="spotify" style="margin-top:10px">Connect Spotify</button></div>`;
    return;
  }
  box.innerHTML = rows.length ? `<div class="result-group"><h3>${S.searchTab === "library" ? "Your library" : sourceLabel(S.searchTab)}</h3>${rows.join("")}</div>` : `<div class="search-note"><strong>No results</strong>Try another search or paste a playlist link into Import.</div>`;
}

async function doSearch() {
  const q = $("#search-input").value.trim();
  S.searchQuery = q;
  if (!q) { renderSearch(); return; }
  S.searchBusy = true;
  renderSearch();
  try {
    if (S.searchTab === "youtube") {
      const d = await api("/api/youtube/search", { method: "POST", body: JSON.stringify({ query: q }) });
      S.searchResults.youtube = d.results || [];
    } else if (S.searchTab === "spotify") {
      if (!S.spotify?.configured) { S.searchResults.spotify = []; }
      else {
        const d = await api("/api/spotify/search", { method: "POST", body: JSON.stringify({ query: q }) });
        S.searchResults.spotify = d.results || [];
      }
    } else {
      const lq = q.toLowerCase();
      S.searchResults.library = S.library.filter((t) => `${t.title} ${t.artist} ${t.album} ${t.camelot}`.toLowerCase().includes(lq));
    }
  } catch (e) { toast(e.message, true); }
  S.searchBusy = false;
  renderSearch();
}

async function onlineAction(item, action, btn) {
  if (item.source === "library") {
    const id = Number(item.id);
    if (action === "now") playTrack(id);
    else queueTrack(id, action === "next");
    return;
  }
  const row = btn?.closest(".result-row");
  if (row) row.classList.add("busy");
  try {
    let res;
    if (item.source === "spotify") {
      res = await api("/api/spotify/track", { method: "POST", body: JSON.stringify({ title: item.title, artist: item.artist || "", artwork: item.artwork || "", action }) });
    } else {
      res = await api(`/api/youtube/track?action=${encodeURIComponent(action)}`, { method: "POST", body: JSON.stringify({ id: item.id, title: item.title, artist: item.artist || "", artwork: item.artwork || "" }) });
    }
    if (res.job_id) S.jobsSeen[res.job_id] = "running";
    toast(action === "now" ? "Downloading — the AI DJ will mix it in" : "Downloading — it will join the queue");
  } catch (e) { toast(e.message, true); if (row) row.classList.remove("busy"); }
}

/* -------------------------------------------------------------------- import */
function openImport(tab = "youtube") {
  setImportTab(tab);
  closeDrawer();
  $("#import-modal").classList.add("open");
  $("#overlay").classList.add("open");
}
function closeModals() {
  $$(".modal").forEach((m) => m.classList.remove("open"));
  $("#drawer").classList.remove("open");
  $("#overlay").classList.remove("open");
}
function closeDrawer() { $("#drawer").classList.remove("open"); }
function setImportTab(tab) {
  S.importTab = tab === "spotify" ? "spotify" : "youtube";
  $$("#import-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.importTab === S.importTab));
  $$(".import-pane").forEach((p) => p.classList.toggle("active", p.id === `import-pane-${S.importTab}`));
  if (S.importTab === "spotify") renderSpotify();
}

function renderPreview(el, data, label) {
  if (!data) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="preview-head">${data.artwork ? `<img src="${esc(data.artwork)}" alt="">` : `<div class="result-art">${icon(label === "spotify" ? "spotify" : "youtube")}</div>`}<div><b>${esc(data.name || "Playlist")}</b><span>${data.count || 0} tracks · ${label === "spotify" ? "Spotify" : "YouTube Music"}</span></div></div><div class="preview-list">${(data.tracks || []).slice(0, 6).map((t) => `<div class="preview-track">${esc(t.title)} <span class="muted">· ${esc(t.artist || "")}</span></div>`).join("")}</div>`;
}

async function previewYt() {
  const url = $("#yt-link").value.trim();
  if (!url) return toast("Paste a YouTube Music playlist link", true);
  $("#yt-preview").innerHTML = `<div class="search-note"><span class="spinner"></span> Reading the playlist…</div>`;
  try {
    const data = await api("/api/youtube/playlist", { method: "POST", body: JSON.stringify({ url, limit: Number($("#yt-limit").value), playlist_name: "", queue_after: $("#yt-queue").checked }) });
    S.ytPreview = { url, data };
    renderPreview($("#yt-preview"), data, "youtube");
  } catch (e) { S.ytPreview = null; $("#yt-preview").innerHTML = `<div class="search-note">${esc(e.message)}</div>`; }
}

async function importYt() {
  const url = $("#yt-link").value.trim();
  if (!url) return toast("Paste a YouTube Music playlist link", true);
  if (!S.ytPreview || S.ytPreview.url !== url) await previewYt();
  if (!S.ytPreview) return;
  $("#yt-import-btn").disabled = true;
  try {
    const res = await api("/api/youtube/playlist/import", { method: "POST", body: JSON.stringify({ url, limit: Number($("#yt-limit").value), playlist_name: "", queue_after: $("#yt-queue").checked }) });
    if (res.job_id) S.jobsSeen[res.job_id] = "running";
    toast("Import started — tracks will appear in the queue");
  } catch (e) { toast(e.message, true); }
  setTimeout(() => { $("#yt-import-btn").disabled = false; }, 1800);
}

async function previewSp() {
  if (!S.spotify?.configured) { setImportTab("spotify"); return toast("Connect Spotify first", true); }
  const url = $("#sp-link").value.trim();
  if (!url) return toast("Paste a Spotify playlist link", true);
  $("#sp-preview").innerHTML = `<div class="search-note"><span class="spinner"></span> Reading the playlist…</div>`;
  try {
    const data = await api("/api/spotify/playlist", { method: "POST", body: JSON.stringify({ url, limit: Number($("#sp-limit").value), queue_after: $("#sp-queue").checked }) });
    S.spPreview = { url, data };
    renderPreview($("#sp-preview"), data, "spotify");
  } catch (e) { S.spPreview = null; $("#sp-preview").innerHTML = `<div class="search-note">${esc(e.message)}</div>`; }
}

async function importSp() {
  if (!S.spotify?.configured) { setImportTab("spotify"); return toast("Connect Spotify first", true); }
  const url = $("#sp-link").value.trim();
  if (!url) return toast("Paste a Spotify playlist link", true);
  if (!S.spPreview || S.spPreview.url !== url) await previewSp();
  if (!S.spPreview) return;
  $("#sp-import-btn").disabled = true;
  try {
    const res = await api("/api/spotify/import", { method: "POST", body: JSON.stringify({ url, limit: Number($("#sp-limit").value), queue_after: $("#sp-queue").checked }) });
    if (res.job_id) S.jobsSeen[res.job_id] = "running";
    toast("Import started — Spotify tracks will join the queue");
  } catch (e) { toast(e.message, true); }
  setTimeout(() => { $("#sp-import-btn").disabled = false; }, 1800);
}

async function loadSpotifyStatus() {
  try { S.spotify = await api("/api/spotify/status"); }
  catch { S.spotify = { configured: false, ok: false, saved: false, error: "" }; }
  renderSpotify();
}

function renderSpotify() {
  const st = S.spotify || { configured: false };
  const connected = !!st.configured;
  $("#spotify-connect").style.display = connected ? "none" : "block";
  $("#spotify-connected").style.display = connected ? "block" : "none";
  if (connected) {
    $("#spotify-connected-text").textContent = st.ok
      ? `Spotify connected · ${st.client_id_hint || "saved"}`
      : `Spotify saved · ${st.client_id_hint || ""} · ${st.error || "check credentials"}`;
  }
  $("#spotify-status").textContent = st.ok ? "Connected" : (st.error || "Not connected");
}

async function saveSpotify() {
  const id = $("#spotify-id").value.trim();
  const secret = $("#spotify-secret").value.trim();
  if (!id || !secret) return toast("Enter both Client ID and Secret", true);
  $("#spotify-save").disabled = true;
  try {
    S.spotify = await api("/api/spotify/config", { method: "POST", body: JSON.stringify({ client_id: id, client_secret: secret }) });
    if (S.spotify.ok) toast("Spotify connected — credentials saved");
    else toast(`Credentials saved, but Spotify rejected them: ${S.spotify.error || "check them"}`, true);
    renderSpotify();
  } catch (e) { toast(e.message, true); }
  $("#spotify-save").disabled = false;
}

async function forgetSpotify() {
  try { S.spotify = await api("/api/spotify/clear", { method: "POST" }); toast("Spotify credentials removed"); renderSpotify(); }
  catch (e) { toast(e.message, true); }
}

/* -------------------------------------------------------------------- jobs */
function renderJobs(jobs) {
  const now = Date.now() / 1000;
  const active = jobs.filter((j) => j.status === "running" || now - (j.created_at || 0) < 40);
  $("#jobs-panel").innerHTML = active.slice(-3).map((j) => {
    const pct = j.total ? Math.min(100, (j.done / j.total) * 100) : 14;
    return `<div class="job ${j.status}"><div class="job-head"><span>${esc(j.title)}</span>${j.status === "running" ? '<span class="spinner"></span>' : j.status === "done" ? icon("check") : icon("close")}</div><div class="job-msg">${esc(j.message || (j.status === "running" ? "Working…" : ""))}</div><div class="job-bar"><i style="width:${pct}%"></i></div></div>`;
  }).join("");
  for (const j of jobs) {
    const prev = S.jobsSeen[j.id];
    if (prev && prev !== j.status && (j.status === "done" || j.status === "error")) {
      toast(j.message || (j.status === "done" ? "Import complete" : "Import failed"), j.status === "error");
      loadLibrary();
      loadSpotifyStatus();
    }
    S.jobsSeen[j.id] = j.status;
  }
}

/* -------------------------------------------------------------------- actions */
function playTrack(id) {
  if (!S.audioEnabled) enableAudio();
  sendAction("play_track", { track_id: Number(id) });
}
function queueTrack(id, priority = false) {
  sendAction(priority ? "queue_play_next" : "queue_add", { track_id: Number(id) });
  toast(priority ? "Moved to the front of the queue" : "Added to the queue");
}
function navigate(page) {
  if (!["home", "search", "library", "queue"].includes(page)) return;
  S.page = page;
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  $$(".side-item[data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $$("#mobile-nav button[data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $(".content")?.scrollTo({ top: 0, behavior: "smooth" });
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (page === "search") setTimeout(() => $("#search-input")?.focus(), 100);
}

function openRoom() {
  closeModals();
  $("#drawer").classList.add("open");
  $("#overlay").classList.add("open");
}

function joinRoom(withSound) {
  S.name = ($("#join-name").value || "").trim() || "Guest";
  localStorage.setItem("donjon_name", S.name);
  localStorage.setItem("donjon_joined", "1");
  S.joined = true;
  $("#join-screen").classList.add("hidden");
  $("#name-input").value = S.name;
  connectRoom();
  renderAll();
  loadLibrary();
  if (withSound) enableAudio().then((ok) => { if (!ok) toast("Sound needs a tap — use the Sound button", true); });
}

function autoJoin() {
  S.name = localStorage.getItem("donjon_name") || "";
  if (!S.name) return false;
  S.joined = true;
  $("#join-screen").classList.add("hidden");
  $("#name-input").value = S.name;
  connectRoom();
  renderAll();
  // A previous tap may let the browser resume automatically; if not, the
  // Sound button stays visible and usable.
  enableAudio().then((ok) => {
    if (!ok) toast("Tap Sound to hear the room on this device");
  });
  return true;
}

async function refreshState() {
  try {
    const data = await api("/api/state");
    applyState(data);
  } catch { /* server starting */ }
}

/* -------------------------------------------------------------------- bind */
function bind() {
  $("#join-btn").addEventListener("click", () => joinRoom(true));
  $("#join-browse").addEventListener("click", () => joinRoom(false));
  $("#join-name").addEventListener("keydown", (e) => { if (e.key === "Enter") joinRoom(true); });
  $("#room-btn").addEventListener("click", openRoom);
  $("#side-room-btn").addEventListener("click", openRoom);
  $("#close-drawer").addEventListener("click", () => { closeDrawer(); $("#overlay").classList.remove("open"); });
  $("#overlay").addEventListener("click", closeModals);
  $("#copy-room-btn").addEventListener("click", () => copyText(S.roomCode));
  $("#copy-code-btn").addEventListener("click", () => copyText(S.roomCode));
  $("#copy-url-btn").addEventListener("click", () => copyText(`${S.data?.network?.url || location.href} · room ${S.roomCode}`));
  $("#top-sound-btn").addEventListener("click", () => { if (S.audioEnabled) { S.audioEnabled = false; if (S.audioWs) S.audioWs.close(); resyncAudio(); renderSync(); } else enableAudio(); });
  $("#sound-btn").addEventListener("click", () => { if (S.audioEnabled) { S.audioEnabled = false; if (S.audioWs) S.audioWs.close(); resyncAudio(); renderSync(); } else enableAudio(); });
  $("#enable-audio-btn").addEventListener("click", enableAudio);
  $("#beep-btn").addEventListener("click", async () => { try { await fetch("/api/calibrate", { method: "POST" }); toast("Beep sent to every speaker"); } catch (e) { toast(e.message, true); } });

  $("#home-import-btn").addEventListener("click", () => openImport("youtube"));
  $("#import-side-btn").addEventListener("click", () => openImport("youtube"));
  $("#import-search-btn").addEventListener("click", () => openImport("youtube"));
  $("#import-library-btn").addEventListener("click", () => openImport("youtube"));
  $("#queue-add-btn").addEventListener("click", () => { navigate("search"); });
  $("#queue-clear-btn").addEventListener("click", () => {
    if (!S.host) return toast("Only the host can clear the whole queue", true);
    if (confirm("Clear the room queue?")) sendAction("clear_queue");
  });

  $("#top-search").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = e.target.value.trim();
    if (!q) return;
    navigate("search");
    $("#search-input").value = q;
    S.searchTab = "youtube";
    $$("#search-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === "youtube"));
    doSearch();
  });
  $("#search-btn").addEventListener("click", doSearch);
  $("#search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $$("#search-tabs button").forEach((b) => b.addEventListener("click", () => {
    S.searchTab = b.dataset.tab;
    $$("#search-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    if (S.searchQuery) doSearch(); else renderSearch();
  }));
  $("#library-search").addEventListener("input", (e) => { S.libraryQuery = e.target.value.trim(); renderLibrary(); });
  $$("#library-filters .chip").forEach((b) => b.addEventListener("click", () => {
    S.librarySource = b.dataset.source;
    $$("#library-filters .chip").forEach((x) => x.classList.toggle("active", x === b));
    renderLibrary();
  }));

  $("#play-btn").addEventListener("click", () => { if (!S.audioEnabled) enableAudio(); sendAction("toggle"); });
  $("#next-btn").addEventListener("click", () => sendAction("next"));
  $("#prev-btn").addEventListener("click", () => sendAction("restart"));
  $("#vol").value = Math.round(S.vol * 100);
  $("#vol").addEventListener("input", (e) => { S.vol = Number(e.target.value) / 100; if (S.gain) S.gain.gain.value = S.vol; localStorage.setItem("donjon_vol", e.target.value); });

  $("#import-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-import-tab]"); if (b) setImportTab(b.dataset.importTab); });
  $$("[data-close-modal]").forEach((b) => b.addEventListener("click", closeModals));
  $("#yt-preview-btn").addEventListener("click", previewYt);
  $("#yt-import-btn").addEventListener("click", importYt);
  $("#sp-preview-btn").addEventListener("click", previewSp);
  $("#sp-import-btn").addEventListener("click", importSp);
  $("#spotify-save").addEventListener("click", saveSpotify);
  $("#spotify-forget").addEventListener("click", forgetSpotify);
  $("#spotify-replace").addEventListener("click", () => { $("#spotify-connect").style.display = "block"; $("#spotify-connected").style.display = "none"; $("#spotify-id").value = ""; $("#spotify-secret").value = ""; });

  $("#name-input").value = S.name;
  $("#name-input").addEventListener("change", (e) => {
    S.name = e.target.value.trim() || "Guest";
    localStorage.setItem("donjon_name", S.name);
    if (S.roomWs) S.roomWs.close();
    setTimeout(connectRoom, 300);
  });
  $("#device-delay").value = S.deviceOffset;
  $("#device-delay-val").textContent = `${S.deviceOffset} ms`;
  $("#device-delay").addEventListener("input", (e) => {
    S.deviceOffset = Number(e.target.value);
    $("#device-delay-val").textContent = `${S.deviceOffset} ms`;
    localStorage.setItem("donjon_delay", String(S.deviceOffset));
    resyncAudio(); renderSync();
  });
  $("#host-lock-switch").addEventListener("click", () => {
    if (!S.host) return toast("Only the host machine can lock the room", true);
    sendAction("host_lock", { value: !S.data?.host_lock });
  });

  document.addEventListener("click", async (e) => {
    const page = e.target.closest("[data-page]");
    if (page) { navigate(page.dataset.page); return; }
    const openImportBtns = e.target.closest("[data-open-import]");
    if (openImportBtns) { openImport(openImportBtns.dataset.openImport); return; }
    const play = e.target.closest("[data-play]");
    if (play) { playTrack(play.dataset.play); return; }
    const qnext = e.target.closest("[data-qnext]");
    if (qnext) { queueTrack(qnext.dataset.qnext, true); return; }
    const queue = e.target.closest("[data-queue]");
    if (queue) { queueTrack(queue.dataset.queue, false); return; }
    const result = e.target.closest("[data-result]");
    if (result) {
      const source = result.dataset.source;
      const item = source === "library"
        ? { source: "library", id: Number(result.dataset.id), title: result.dataset.title, artist: result.dataset.artist }
        : { source, id: result.dataset.id, title: result.dataset.title, artist: result.dataset.artist, artwork: result.dataset.artwork };
      await onlineAction(item, result.dataset.result, result);
      return;
    }
    const priority = e.target.closest("[data-qpriority]");
    if (priority) { sendAction("queue_priority", { uid: priority.dataset.qpriority }); return; }
    const move = e.target.closest("[data-qmove]");
    if (move) { sendAction("queue_move", { uid: move.dataset.uid, delta: Number(move.dataset.qmove) }); return; }
    const remove = e.target.closest("[data-qremove]");
    if (remove) { sendAction("queue_remove", { uid: remove.dataset.qremove }); return; }
    const importTab = e.target.closest("[data-import-tab]");
    if (importTab) { setImportTab(importTab.dataset.importTab); return; }
  });

  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (e.code === "Space") { e.preventDefault(); $("#play-btn").click(); }
    if (e.key === "/") { e.preventDefault(); navigate("search"); }
    if (e.key === "Escape") closeModals();
  });
}

async function init() {
  bind();
  $("#join-name").value = S.name || (S.host ? "Host" : "");
  await refreshState();
  await Promise.allSettled([loadLibrary(), loadSpotifyStatus()]);
  const remembered = localStorage.getItem("donjon_joined") === "1" || !!localStorage.getItem("donjon_name");
  if (remembered && S.name) autoJoin();
  else {
    $("#join-code").textContent = S.roomCode;
    $("#join-screen").classList.remove("hidden");
  }
  renderSpotify();
  renderSearch();
  setInterval(tick, 250);
  setInterval(refreshState, 10000);
}

window.addEventListener("DOMContentLoaded", init);
