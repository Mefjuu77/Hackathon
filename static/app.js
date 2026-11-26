"use strict";

const SAMPLE_RATE = 16000;
const LEAD_SECONDS = 0.2;
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

/** Plays 100 ms stereo PCM16 frames back to back, LEAD_SECONDS ahead of real time. */
class StereoPlayer {
  constructor() {
    this.ctx = new AudioContext();
    this.nextTime = 0;
  }

  reset() {
    this.nextTime = 0;
    return this.ctx.resume();
  }

  push(arrayBuffer) {
    const pcm = new Int16Array(arrayBuffer);
    const frames = pcm.length / 2;
    const buffer = this.ctx.createBuffer(2, frames, SAMPLE_RATE);
    const left = buffer.getChannelData(0);
    const right = buffer.getChannelData(1);
    for (let i = 0; i < frames; i++) {
      left[i] = pcm[2 * i] / 32768;
      right[i] = pcm[2 * i + 1] / 32768;
    }
    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.ctx.destination);
    if (this.nextTime < this.ctx.currentTime) {
      this.nextTime = this.ctx.currentTime + LEAD_SECONDS; // underrun: restart with a lead
    }
    source.start(this.nextTime);
    this.nextTime += buffer.duration;
  }
}

let player = null;

async function preparePlayer() {
  player ??= new StereoPlayer(); // created on a click: browsers require a user gesture
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

// --- Mood (chart arrives in E4) -------------------------------------------

function addMood({ turn, score, reason }) {
  const line = document.createElement("div");
  line.textContent = `wypowiedź ${turn}: ${score}/10 — ${reason}`;
  $("mood").append(line);
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
  $("mood").replaceChildren();
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

if (replayId) initReplay(); else initLive();
