"use strict";

const SAMPLE_RATE = 16000;
const LEAD_SECONDS = 0.3;
const ROLE_LABELS = { customer: "Customer", helpdesk: "Agent" };
// scenario_id -> English title from /api/scenarios (saved recordings may carry old titles)
const scenarioTitles = {};
const NEW_ITEM_MS = 600;
// Transcript text arrives as whole utterances; it is revealed word by word, chatbot style.
const REVEAL_MS_PER_WORD = 70;

const $ = (id) => document.getElementById(id);
const replayMatch = location.pathname.match(/^\/replay\/([^/]+)$/);
const linkedRunId = replayMatch ? decodeURIComponent(replayMatch[1]) : null;

/** Level 1 (red, angry / incompetent) → amber → level 10 (green, happy / expert). */
function levelColor(level) {
  const clamped = Math.min(10, Math.max(1, Number(level) || 1));
  const hue = Math.round(((clamped - 1) / 9) * 130);
  return `hsl(${hue} 75% 45%)`;
}

// --- Audio ---------------------------------------------------------------

/**
 * Streams 100 ms stereo PCM16 frames into one continuous worklet output.
 * The context runs at 16 kHz, so the browser resamples a single stream, not each frame.
 */
class StereoPlayer {
  static async create() {
    const ctx = new AudioContext({ sampleRate: SAMPLE_RATE, latencyHint: "playback" });
    await ctx.audioWorklet.addModule("/static/player-worklet.js");
    const node = new AudioWorkletNode(ctx, "stereo-stream", {
      numberOfInputs: 0,
      outputChannelCount: [2],
      processorOptions: { leadSamples: Math.round(LEAD_SECONDS * SAMPLE_RATE) },
    });
    node.connect(ctx.destination);
    return new StereoPlayer(ctx, node);
  }

  constructor(ctx, node) {
    this.ctx = ctx;
    this.node = node;
    this.underruns = 0; // inspect in the console when audio stutters
    node.port.onmessage = ({ data }) => { this.underruns = data.underruns; };
  }

  reset() {
    this.underruns = 0;
    this.node.port.postMessage({ type: "reset" });
    return this.ctx.resume();
  }

  push(arrayBuffer) {
    const pcm = new Int16Array(arrayBuffer);
    const frames = pcm.length / 2;
    const left = new Float32Array(frames);
    const right = new Float32Array(frames);
    for (let i = 0; i < frames; i++) {
      left[i] = pcm[2 * i] / 32768;
      right[i] = pcm[2 * i + 1] / 32768;
    }
    this.node.port.postMessage({ left, right }, [left.buffer, right.buffer]);
  }
}

let player = null;

async function preparePlayer() {
  player ??= await StereoPlayer.create(); // created on a click: browsers require a user gesture
  window.player = player;
  await player.reset();
}

// --- Level sliders -------------------------------------------------------

function paintSlider(id) {
  const input = $(id);
  const level = Number(input.value);
  const wrapper = input.closest(".slider") ?? input.parentElement;
  wrapper.style.setProperty("--level-color", levelColor(level));
  wrapper.style.setProperty("--level-pct", `${((level - 1) / 9) * 100}%`);
  wrapper.dataset.level = String(level);
  $(`${id}-value`).textContent = String(level);
}

function initSliders() {
  for (const id of ["nps", "cx"]) {
    $(id).addEventListener("input", () => paintSlider(id));
    paintSlider(id);
  }
}

// --- Transcript ----------------------------------------------------------

const utterances = new Map(); // utterance_id -> <li>
const lastByRole = {};
const reveals = new Map(); // <li> -> animation frame id of its running reveal
let followTranscript = true; // false once the user scrolls up; true again at the bottom

/** Words and [audio tags] of an utterance, in order. */
function tokenize(text) {
  const tokens = [];
  text.split(/\[([^\]]+)\]/).forEach((part, i) => {
    if (i % 2 === 1) tokens.push({ tag: true, text: part.trim() });
    else for (const word of part.split(/\s+/)) if (word) tokens.push({ tag: false, text: word });
  });
  return tokens;
}

/** One token as DOM; agent text never goes through innerHTML. */
function tokenNode({ tag, text }) {
  const node = document.createElement("span");
  node.className = tag ? "tag w" : "w";
  node.textContent = text;
  return node;
}

function renderEmpty(container) {
  const empty = document.createElement("span");
  empty.className = "empty";
  empty.textContent = "(interrupted before speaking)";
  container.replaceChildren(empty);
}

function scrollTranscriptToEnd() {
  if (!followTranscript) return;
  const list = $("transcript");
  list.scrollTop = list.scrollHeight;
}

function stopReveal(li) {
  cancelAnimationFrame(reveals.get(li));
  reveals.delete(li);
  li.classList.remove("typing");
}

function stopAllReveals() {
  for (const li of [...reveals.keys()]) stopReveal(li);
}

/** Renders the whole text at once (corrections, finished reveals). */
function renderText(li, text) {
  stopReveal(li);
  const container = li.querySelector(".text");
  const tokens = tokenize(text);
  if (!tokens.length) return renderEmpty(container);
  container.replaceChildren();
  for (const token of tokens) container.append(tokenNode(token), " ");
  container.querySelectorAll(".w").forEach((w) => w.classList.add("shown"));
}

/** Appends tokens at a steady pace, like a chatbot streaming its answer. Catches up after a hidden tab. */
function revealText(li, text) {
  stopReveal(li);
  const container = li.querySelector(".text");
  const tokens = tokenize(text);
  if (!tokens.length) return renderEmpty(container);
  container.replaceChildren();
  li.classList.add("typing");
  const startedAt = performance.now();
  let shown = 0;
  const step = (now) => {
    const target = Math.min(tokens.length, 1 + Math.floor((now - startedAt) / REVEAL_MS_PER_WORD));
    while (shown < target) container.append(tokenNode(tokens[shown++]), " ");
    scrollTranscriptToEnd();
    if (shown < tokens.length) {
      reveals.set(li, requestAnimationFrame(step));
    } else {
      reveals.delete(li);
      li.classList.remove("typing");
    }
  };
  reveals.set(li, requestAnimationFrame(step));
}

function addUtterance({ utterance_id, role, text, t }) {
  const li = document.createElement("li");
  li.className = `${role} new`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = ROLE_LABELS[role] ?? role;
  const body = document.createElement("span");
  body.className = "text";
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = `${t.toFixed(1)} s`;
  li.append(who, body, time);
  utterances.set(utterance_id, li);
  lastByRole[role] = li;
  $("transcript").append(li);
  revealText(li, text);
  scrollTranscriptToEnd();
  setTimeout(() => li.classList.remove("new"), NEW_ITEM_MS);
}

function correctUtterance({ utterance_id, text }) {
  const li = utterances.get(utterance_id);
  if (li) renderText(li, text);
}

function markInterrupted({ role }) {
  const li = lastByRole[role];
  if (!li || li.querySelector(".interrupted")) return;
  const mark = document.createElement("span");
  mark.className = "interrupted";
  mark.textContent = "interrupted";
  li.append(mark);
}

function initTranscript() {
  const list = $("transcript");
  list.addEventListener("scroll", () => {
    followTranscript = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  });
}

// --- Mood chart ----------------------------------------------------------

let moodChart = null;
// Mood bands drawn behind the line: same red → amber → green meaning as the sliders.
const MOOD_ZONES = [
  { from: 1, to: 4, level: 1, label: "unhappy" },
  { from: 4, to: 7, level: 5, label: "neutral" },
  { from: 7, to: 10, level: 10, label: "happy" },
];

/** Splits a long reason into tooltip lines (Chart.js does not wrap text). */
function wrapLines(text, width = 48) {
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    if (line && line.length + word.length + 1 > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

/** Chart colours come from CSS custom properties on #mood, so the stylesheet themes the chart. */
function chartTheme() {
  const style = getComputedStyle($("mood"));
  const read = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    text: read("--chart-text", "#6b7280"),
    grid: read("--chart-grid", "rgba(0, 0, 0, 0.08)"),
    line: read("--chart-line", "#2563eb"),
    up: read("--chart-up", "#34d399"),
    down: read("--chart-down", "#f87171"),
    surface: read("--chart-surface", "#ffffff"),
    font: read("--chart-font", getComputedStyle(document.body).fontFamily),
  };
}

function signed(delta) {
  return delta > 0 ? `+${delta}` : delta < 0 ? `−${-delta}` : "±0";
}

/** Coloured mood bands with labels, plus a dashed line at the starting mood. */
function zonesPlugin(theme) {
  return {
    id: "moodZones",
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea: area, scales: { y } } = chart;
      ctx.save();
      for (const zone of MOOD_ZONES) {
        const top = y.getPixelForValue(zone.to);
        const bottom = y.getPixelForValue(zone.from);
        ctx.fillStyle = levelColor(zone.level).replace(")", " / 0.09)");
        ctx.fillRect(area.left, top, area.right - area.left, bottom - top);
        // Label in the right gutter, rotated, so it never covers a data point.
        ctx.save();
        ctx.translate(area.right + 12, (top + bottom) / 2);
        ctx.rotate(-Math.PI / 2);
        ctx.fillStyle = levelColor(zone.level).replace(")", " / 0.8)");
        ctx.font = `600 9px ${theme.font}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(zone.label.toUpperCase(), 0, 0);
        ctx.restore();
      }
      const start = chart.data.datasets[0].data[0];
      if (start) {
        const py = y.getPixelForValue(start.y);
        ctx.strokeStyle = theme.text;
        ctx.globalAlpha = 0.55;
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(area.left, py);
        ctx.lineTo(area.right, py);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
        ctx.fillStyle = theme.text;
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        ctx.fillText(`START ${start.y}`, area.left + 6, py - 3);
      }
      ctx.restore();
    },
  };
}

/** "+2" / "−1" above each point where the mood changed. */
function deltaLabelsPlugin(theme) {
  return {
    id: "moodDeltas",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      const points = chart.data.datasets[0].data;
      const { ctx } = chart;
      ctx.save();
      ctx.font = `700 11px ${theme.font}`;
      ctx.textAlign = "center";
      for (let i = 1; i < points.length; i++) {
        const delta = points[i].y - points[i - 1].y;
        const element = meta.data[i];
        if (!delta || !element) continue;
        const { x, y } = element.getProps(["x", "y"], true);
        // Above rising points, below falling ones — flipped when that would hit the axis or the top edge.
        let above = delta > 0;
        if (above && y - 24 < chart.chartArea.top) above = false;
        else if (!above && y + 24 > chart.chartArea.bottom) above = true;
        ctx.fillStyle = delta > 0 ? theme.up : theme.down;
        ctx.textBaseline = above ? "bottom" : "top";
        ctx.fillText(signed(delta), x, above ? y - 10 : y + 10);
      }
      ctx.restore();
    },
  };
}

function initMoodChart() {
  if (!window.Chart) {
    $("mood").textContent = "Missing static/vendor/chart.umd.min.js — mood chart unavailable.";
    return;
  }
  const theme = chartTheme();
  Chart.defaults.font.family = theme.font;
  Chart.defaults.color = theme.text;
  const pointColor = (context) => levelColor(context.raw?.y);
  const isLast = (context) => context.dataIndex === context.dataset.data.length - 1;
  const segmentColor = ({ p0, p1 }) => (p1.parsed.y > p0.parsed.y ? theme.up : p1.parsed.y < p0.parsed.y ? theme.down : theme.line);
  moodChart = new Chart($("mood-chart"), {
    type: "line",
    data: {
      datasets: [{
        data: [],
        borderColor: theme.line,
        borderWidth: 3,
        tension: 0.25,
        segment: { borderColor: segmentColor },
        pointBackgroundColor: pointColor,
        pointBorderColor: (context) => (isLast(context) ? theme.surface : pointColor(context)),
        pointBorderWidth: (context) => (isLast(context) ? 3 : 1),
        pointRadius: (context) => (isLast(context) ? 8 : 5),
        pointHoverRadius: 9,
        clip: false,
      }],
    },
    options: {
      maintainAspectRatio: false,
      parsing: false,
      layout: { padding: { top: 18, right: 24 } },
      animation: { duration: 600, easing: "easeOutCubic" },
      scales: {
        x: {
          type: "linear",
          min: 0,
          suggestedMax: 6,
          grid: { display: false },
          border: { color: theme.grid },
          ticks: { stepSize: 1, precision: 0, color: theme.text },
          title: { display: true, text: "agent turn", color: theme.text },
        },
        y: {
          min: 1,
          max: 10,
          grid: { color: theme.grid },
          border: { display: false },
          ticks: { stepSize: 1, color: theme.text },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          padding: 10,
          callbacks: {
            title: ([item]) => (item.raw.x === 0 ? "Call start" : `After agent turn ${item.raw.x}`),
            label: (item) => {
              const points = item.dataset.data;
              const previous = points[item.dataIndex - 1];
              const change = previous ? ` (${signed(item.raw.y - previous.y)})` : "";
              return [`Mood ${item.raw.y}/10${change}`, ...(item.raw.x === 0 ? [] : wrapLines(item.raw.reason))];
            },
          },
        },
      },
    },
    plugins: [zonesPlugin(theme), deltaLabelsPlugin(theme)],
  });
}

/** "2 → 7  +5" next to the chart title. */
function showMoodDelta(points) {
  const box = $("mood-delta");
  if (!box) return;
  box.replaceChildren();
  box.className = "mood-delta";
  if (!points.length) return;
  const first = points[0].y;
  const last = points.at(-1).y;
  const delta = last - first;
  const value = span("mood-delta-value", `${first} → ${last}`);
  value.style.setProperty("--level-color", levelColor(last));
  box.append(value, span("mood-delta-change", signed(delta)));
  box.classList.add(delta > 0 ? "up" : delta < 0 ? "down" : "flat");
}

/** Several reports after the same helpdesk utterance: the latest one wins (summary.json keeps all). */
function addMood({ turn, score, reason }) {
  if (!moodChart) return;
  const points = moodChart.data.datasets[0].data;
  const point = { x: turn, y: score, reason };
  const index = points.findIndex((p) => p.x === turn);
  if (index >= 0) points[index] = point; else points.push(point);
  points.sort((a, b) => a.x - b.x);
  moodChart.update();
  showMoodDelta(points);
}

function resetMood() {
  showMoodDelta([]);
  if (!moodChart) return;
  moodChart.data.datasets[0].data = [];
  moodChart.update();
}

// --- Formatting ----------------------------------------------------------

function formatDuration(seconds) {
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

// --- Run state -----------------------------------------------------------

/** "idle" | "live" | "replay"; designs can style on body[data-mode]. */
let mode = "idle";
let runStarted = false;

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = !message;
}

function setMode(next) {
  mode = next;
  document.body.dataset.mode = next;
  for (const id of ["nps", "cx", "scenario", "start"]) $(id).disabled = next !== "idle";
  $("stop").disabled = next === "idle";
  // Replays can be switched mid-play; a live run locks the library.
  for (const button of document.querySelectorAll("#runs .run-play")) button.disabled = next === "live";
}

function resetView() {
  runStarted = false;
  utterances.clear();
  for (const role in lastByRole) delete lastByRole[role];
  stopAllReveals();
  followTranscript = true;
  $("transcript").replaceChildren();
  resetMood();
  $("run-info").textContent = "";
  showError("");
}

function handleEvent(event) {
  switch (event.type) {
    case "run_started":
      runStarted = true;
      $("run-info").textContent =
        `${scenarioTitles[event.scenario_id] ?? event.scenario_title} · ${event.customer_name} (NPS ${event.nps_level}) ↔ ${event.agent_name} (CX ${event.cx_level})`;
      addMood({ turn: 0, score: event.nps_level, reason: "start" });
      break;
    case "transcript":
      addUtterance(event);
      break;
    case "transcript_correction":
      correctUtterance(event);
      break;
    case "interruption":
      markInterrupted(event);
      break;
    case "mood":
      addMood(event);
      break;
    case "error":
      showError(`Error: ${event.message}`);
      break;
  }
}

/** Opens a socket that feeds audio to the player and JSON events to the view. */
function connect(path, onOpen, onEnded) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`);
  ws.binaryType = "arraybuffer";
  let ended = false;
  ws.onopen = () => onOpen?.(ws);
  ws.onmessage = (message) => {
    if (message.data instanceof ArrayBuffer) {
      player.push(message.data);
      return;
    }
    const event = JSON.parse(message.data);
    handleEvent(event);
    if (event.type === "run_ended") {
      ended = true;
      onEnded();
    }
  };
  ws.onclose = () => {
    if (!ended) onEnded();
  };
  return ws;
}

// --- Live mode -----------------------------------------------------------

let liveSocket = null;

function finishLive() {
  setMode("idle");
  loadRuns(); // the finished run is now in the library
}

async function startLive() {
  stopReplay();
  resetView();
  setMode("live");
  await preparePlayer();
  const command = {
    type: "start",
    nps_level: Number($("nps").value),
    cx_level: Number($("cx").value),
    scenario_id: $("scenario").value,
  };
  if (liveSocket?.readyState === WebSocket.OPEN) {
    liveSocket.send(JSON.stringify(command));
    return;
  }
  liveSocket = connect("/ws", (ws) => ws.send(JSON.stringify(command)), finishLive);
  liveSocket.addEventListener("message", (message) => {
    // A start rejected up front (bad input, "already running") gets no run_ended.
    if (typeof message.data === "string" && !runStarted && JSON.parse(message.data).type === "error") {
      setMode("idle");
    }
  });
  liveSocket.addEventListener("close", () => { liveSocket = null; });
}

// --- Replay library ------------------------------------------------------

let replaySocket = null;
let playingRunId = null;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatRunDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${MONTHS[date.getMonth()]} ${date.getDate()} · ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function span(className, text) {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  return element;
}

function levelChip(label, level) {
  const chip = span("lvl", `${label} ${level}`);
  chip.style.setProperty("--level-color", levelColor(level));
  return chip;
}

function renderRunItem(run) {
  const li = document.createElement("li");
  li.className = "run-item";
  li.dataset.runId = run.run_id;
  li.classList.toggle("playing", run.run_id === playingRunId);
  li.classList.toggle("selected", run.run_id === linkedRunId);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "run-play";
  button.disabled = mode === "live";
  button.title = `Play ${run.run_id}`;
  const title = run.scenario_title ? `${run.scenario_id} · ${run.scenario_title}` : run.scenario_id ?? run.run_id;
  const meta = span("run-meta", "");
  meta.append(span("run-date", formatRunDate(run.started_at) || run.run_id));
  if (run.nps_level != null) meta.append(levelChip("NPS", run.nps_level));
  if (run.cx_level != null) meta.append(levelChip("CX", run.cx_level));
  if (run.duration_s != null) meta.append(span("run-duration", formatDuration(run.duration_s)));
  if (run.mood) {
    const { start, end, delta } = run.mood;
    meta.append(span(`run-mood ${delta > 0 ? "up" : delta < 0 ? "down" : "flat"}`, `${start} → ${end}`));
  }
  button.append(span("run-title", title), meta);
  button.addEventListener("click", () => startReplay(run.run_id));
  li.append(button);
  return li;
}

async function loadRuns() {
  const list = $("runs");
  if (!list) return;
  const showEmpty = (text) => {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = text;
    list.replaceChildren(empty);
  };
  let runs;
  try {
    const response = await fetch("/api/runs");
    if (!response.ok) throw new Error(String(response.status));
    runs = await response.json();
  } catch {
    showEmpty("Could not load the recordings list.");
    return;
  }
  const count = $("runs-count");
  if (count) count.textContent = String(runs.length);
  if (!runs.length) {
    showEmpty("No saved calls yet");
    return;
  }
  list.replaceChildren(...runs.map(renderRunItem));
  if (linkedRunId && mode === "idle" && !playingRunId) {
    list.querySelector(".run-item.selected")?.scrollIntoView({ block: "nearest" });
  }
}

function markPlaying(runId) {
  playingRunId = runId;
  for (const item of document.querySelectorAll("#runs .run-item")) {
    item.classList.toggle("playing", item.dataset.runId === runId);
  }
}

/** Closes the current replay (if any) and silences what is already buffered. */
function stopReplay() {
  if (!replaySocket) return;
  const socket = replaySocket;
  replaySocket = null; // its close handler sees it is no longer current
  socket.close();
  player?.reset();
  markPlaying(null);
  if (mode === "replay") setMode("idle");
}

async function startReplay(runId) {
  if (mode === "live") return;
  stopReplay();
  resetView();
  setMode("replay");
  markPlaying(runId);
  await preparePlayer();
  const socket = connect(`/ws/replay/${encodeURIComponent(runId)}`, null, () => {
    if (replaySocket !== socket) return; // superseded by another replay or stopped
    replaySocket = null;
    markPlaying(null);
    setMode("idle");
  });
  replaySocket = socket;
}

// --- Init ----------------------------------------------------------------

async function loadScenarios() {
  try {
    const scenarios = await (await fetch("/api/scenarios")).json();
    for (const { id, title } of scenarios) {
      scenarioTitles[id] = title;
      $("scenario").append(new Option(`${id} — ${title}`, id));
    }
  } catch {
    showError("Could not load the scenario list.");
  }
}

function init() {
  initSliders();
  initTranscript();
  initMoodChart();
  resetMood();
  setMode("idle");
  $("start").addEventListener("click", startLive);
  $("stop").addEventListener("click", () => {
    if (mode === "replay") {
      stopReplay();
    } else {
      liveSocket?.send(JSON.stringify({ type: "stop" }));
    }
  });
  if (linkedRunId) document.title = `Recording ${linkedRunId} — Customer vs Help Desk`;
  loadScenarios();
  loadRuns();
}

init();
