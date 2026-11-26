"use strict";

const SAMPLE_RATE = 16000;
const LEAD_SECONDS = 0.3;
const ROLE_LABELS = { customer: "👤 Klient", helpdesk: "🎧 Konsultant" };
const END_REASONS = {
  customer_end_call: "Klient zakończył rozmowę",
  helpdesk_end_call: "Konsultant zakończył rozmowę",
  customer_closed: "Połączenie klienta zamknięte",
  helpdesk_closed: "Połączenie konsultanta zamknięte",
  timeout: "Limit czasu",
  stopped: "Zatrzymano",
  error: "Błąd",
};

const $ = (id) => document.getElementById(id);
const replayMatch = location.pathname.match(/^\/replay\/([^/]+)$/);
const replayId = replayMatch ? decodeURIComponent(replayMatch[1]) : null;

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

// --- Transcript ----------------------------------------------------------

const utterances = new Map(); // utterance_id -> <li>
const lastByRole = {};

/** Text with [audio tags] rendered as small labels; agent text never goes through innerHTML. */
function renderText(container, text) {
  container.replaceChildren();
  if (!text.trim()) {
    const empty = document.createElement("span");
    empty.className = "empty";
    empty.textContent = "(przerwane przed wypowiedzią)";
    container.append(empty);
    return;
  }
  text.split(/\[([^\]]+)\]/).forEach((part, i) => {
    if (i % 2 === 1) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = part;
      container.append(tag);
    } else if (part.trim()) {
      container.append(part.trim() + " ");
    }
  });
}

function addUtterance({ utterance_id, role, text, t }) {
  const li = document.createElement("li");
  li.className = role;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = `${ROLE_LABELS[role]}: `;
  const body = document.createElement("span");
  body.className = "text";
  renderText(body, text);
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = `${t.toFixed(1)} s`;
  li.append(who, body, time);
  utterances.set(utterance_id, li);
  lastByRole[role] = li;
  const list = $("transcript");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.append(li);
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function correctUtterance({ utterance_id, text }) {
  const li = utterances.get(utterance_id);
  if (li) renderText(li.querySelector(".text"), text);
}

function markInterrupted({ role }) {
  const li = lastByRole[role];
  if (!li || li.querySelector(".interrupted")) return;
  const mark = document.createElement("span");
  mark.className = "interrupted";
  mark.textContent = "⤳ przerwano";
  li.append(mark);
}

// --- Mood chart ----------------------------------------------------------

let moodChart = null;

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

function initMoodChart() {
  if (!window.Chart) {
    $("mood").textContent = "Brak pliku static/vendor/chart.umd.min.js — wykres nastroju niedostępny.";
    return;
  }
  moodChart = new Chart($("mood-chart"), {
    type: "line",
    data: { datasets: [{ data: [], borderColor: "#2563eb", backgroundColor: "#2563eb", pointRadius: 4, pointHoverRadius: 6, clip: false }] },
    options: {
      maintainAspectRatio: false,
      parsing: false,
      scales: {
        x: { type: "linear", min: 0, suggestedMax: 5, ticks: { stepSize: 1, precision: 0 }, title: { display: true, text: "wypowiedź konsultanta" } },
        y: { min: 1, max: 10, ticks: { stepSize: 1 }, title: { display: true, text: "nastrój" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            title: ([item]) => (item.raw.x === 0 ? "Start" : `Wypowiedź ${item.raw.x}`),
            label: (item) => [`${item.raw.y}/10`, ...(item.raw.x === 0 ? [] : wrapLines(item.raw.reason))],
          },
        },
      },
    },
  });
}

/** Several reports after the same helpdesk utterance: the latest one wins (summary.json keeps all). */
function addMood({ turn, score, reason }) {
  if (!moodChart) return;
  const points = moodChart.data.datasets[0].data;
  const point = { x: turn, y: score, reason };
  const index = points.findIndex((p) => p.x === turn);
  if (index >= 0) points[index] = point; else points.push(point);
  moodChart.update();
}

function resetMood() {
  if (!moodChart) return;
  moodChart.data.datasets[0].data = [];
  moodChart.update();
}

// --- Summary bar ---------------------------------------------------------

function formatDuration(seconds) {
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function showSummary({ end_reason, summary, files }) {
  if (!summary) return;
  const box = $("summary");
  box.replaceChildren();
  const add = (className, text) => {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    box.append(span);
    return span;
  };
  const isError = end_reason === "error";
  add(isError ? "reason failed" : "reason", isError ? `Błąd: ${summary.error ?? "nieznany"}` : END_REASONS[end_reason] ?? end_reason);
  add("item", `⏱ ${formatDuration(summary.duration_s)}`);
  add("item", `tury: klient ${summary.turns.customer} / konsultant ${summary.turns.helpdesk}`);
  add("item", `przerwania: klient ${summary.interruptions.customer} / konsultant ${summary.interruptions.helpdesk}`);
  const { start, end, delta } = summary.mood;
  const sign = delta > 0 ? `+${delta}` : delta < 0 ? `−${-delta}` : "±0";
  add(`item mood ${delta > 0 ? "up" : delta < 0 ? "down" : ""}`, `nastrój ${start} → ${end} (${sign})`);
  const links = document.createElement("span");
  links.className = "links";
  for (const [key, label] of [["audio", "⬇ audio.wav"], ["transcript", "⬇ transcript.json"]]) {
    if (!files?.[key]) continue;
    const link = document.createElement("a");
    link.href = files[key];
    link.download = "";
    link.textContent = label;
    links.append(link);
  }
  box.append(links);
  box.hidden = false;
}

// --- Run state -----------------------------------------------------------

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = !message;
}

function setRunning(running) {
  for (const id of ["nps", "cx", "scenario", "start"]) $(id).disabled = running;
  $("stop").disabled = !running;
}

function resetView() {
  runStarted = false;
  utterances.clear();
  for (const role in lastByRole) delete lastByRole[role];
  $("transcript").replaceChildren();
  resetMood();
  $("summary").hidden = true;
  $("run-info").textContent = "";
  showError("");
}

let runStarted = false;

function handleEvent(event) {
  switch (event.type) {
    case "run_started":
      runStarted = true;
      $("run-info").textContent =
        `${event.scenario_title} · ${event.customer_name} (NPS ${event.nps_level}) ↔ ${event.agent_name} (CX ${event.cx_level})`;
      $("status").textContent = "— rozmowa trwa";
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
      showError(`Błąd: ${event.message}`);
      break;
    case "run_ended":
      $("status").textContent = `— ${END_REASONS[event.end_reason] ?? event.end_reason}`;
      showSummary(event);
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
  setRunning(false);
}

async function startLive() {
  resetView();
  setRunning(true);
  $("status").textContent = "— łączenie…";
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
      $("status").textContent = "";
      finishLive();
    }
  });
  liveSocket.addEventListener("close", () => { liveSocket = null; });
}

async function initLive() {
  for (const id of ["nps", "cx"]) {
    $(id).addEventListener("input", () => { $(`${id}-value`).textContent = $(id).value; });
  }
  $("start").addEventListener("click", startLive);
  $("stop").addEventListener("click", () => liveSocket?.send(JSON.stringify({ type: "stop" })));
  try {
    const scenarios = await (await fetch("/api/scenarios")).json();
    for (const { id, title } of scenarios) {
      $("scenario").append(new Option(`${id} — ${title}`, id));
    }
  } catch {
    showError("Nie udało się pobrać listy scenariuszy.");
  }
}

// --- Replay mode ---------------------------------------------------------

function initReplay() {
  $("controls").hidden = true;
  $("replay-controls").hidden = false;
  $("replay-id").textContent = replayId;
  document.title = `Replay ${replayId} — Klient vs Help Desk`;
  $("replay-start").addEventListener("click", async () => {
    $("replay-start").disabled = true;
    resetView();
    await preparePlayer();
    connect(`/ws/replay/${encodeURIComponent(replayId)}`, null, () => { $("replay-start").disabled = false; });
  });
}

initMoodChart();
if (replayId) initReplay(); else initLive();
