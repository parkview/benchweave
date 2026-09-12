"use strict";

const CHANNEL_NAMES = ["a0", "a1", "a2", "a3", "a4", "a7"];
const COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#f032e6"];
const MAX_POINTS = 300;

let chart = null;
let eventSource = null;

// -- Chart setup ------------------------------------------------------------

function initChart() {
  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: {
      datasets: CHANNEL_NAMES.map((name, i) => ({
        label: name,
        data: [],
        borderColor: COLORS[i],
        backgroundColor: COLORS[i],
        borderWidth: 1,
        pointRadius: 0,
        parsing: false,
      })),
    },
    options: {
      animation: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "linear", title: { display: true, text: "sample counter" } },
        y: { min: 0, max: 4095, title: { display: true, text: "raw (12-bit)" } },
      },
    },
  });
}

function addSample(counter, channels) {
  chart.data.datasets.forEach((ds, i) => {
    ds.data.push({ x: counter, y: channels[i] });
    if (ds.data.length > MAX_POINTS) ds.data.shift();
  });
  chart.update();
}

// -- API helpers ------------------------------------------------------------

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${path} -> ${resp.status} ${text}`);
  }
  return resp.json();
}

function setStatus(msg) {
  document.getElementById("status").textContent = msg;
}

// -- Connect flow -----------------------------------------------------------

async function discover() {
  try {
    const boards = await api("/api/boards");
    const select = document.getElementById("board-select");
    select.innerHTML = "";
    if (boards.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(no board found)";
      select.appendChild(opt);
      setStatus("no board found");
      return;
    }
    boards.forEach((b) => {
      const opt = document.createElement("option");
      opt.value = b.device;
      opt.textContent = `${b.device} — fw ${b.firmware}, ${b.channels}ch ${b.resolution}-bit`;
      select.appendChild(opt);
    });
    if (boards.length === 1) select.value = boards[0].device;
    setStatus(`discovered ${boards.length} board(s)`);
  } catch (e) {
    setStatus("discover error: " + e.message);
  }
}

async function connect() {
  const device = document.getElementById("board-select").value;
  if (!device) return;
  try {
    await api("/api/connect", { method: "POST", body: JSON.stringify({ device }) });
    await refreshStatus();
  } catch (e) {
    setStatus("connect error: " + e.message);
  }
}

async function refreshStatus() {
  const s = await api("/api/status");
  document.getElementById("config").hidden = !s.connected;
  const averaging = document.getElementById("averaging");
  averaging.value = String(s.averaging);
  averaging.disabled = s.streaming;
  document.querySelectorAll("#channels input").forEach((cb) => (cb.disabled = s.streaming));
  setChannelMask(s.channel_mask);
  document.getElementById("stream-toggle").textContent = s.streaming ? "Stop" : "Start";
  setStatus(s.connected ? `connected: ${s.device} (fw ${s.firmware})` : "disconnected");
  return s;
}

// -- Channel checkboxes -----------------------------------------------------

function buildChannelCheckboxes() {
  const container = document.getElementById("channels");
  CHANNEL_NAMES.forEach((name, i) => {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.bit = String(i);
    cb.checked = true;
    cb.addEventListener("change", onChannelChange);
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + name));
    container.appendChild(label);
  });
}

function setChannelMask(mask) {
  CHANNEL_NAMES.forEach((_, i) => {
    const cb = document.querySelector(`#channels input[data-bit="${i}"]`);
    if (cb) cb.checked = (mask & (1 << i)) !== 0;
  });
}

function channelMask() {
  let mask = 0;
  CHANNEL_NAMES.forEach((_, i) => {
    const cb = document.querySelector(`#channels input[data-bit="${i}"]`);
    if (cb && cb.checked) mask |= 1 << i;
  });
  return mask;
}

async function onChannelChange() {
  try {
    await api("/api/channels", {
      method: "POST",
      body: JSON.stringify({ mask: channelMask() }),
    });
  } catch (e) {
    setStatus("channels error: " + e.message);
  }
}

// -- Averaging --------------------------------------------------------------

async function onAveragingChange() {
  const n = Number(document.getElementById("averaging").value);
  try {
    await api("/api/averaging", { method: "POST", body: JSON.stringify({ n }) });
  } catch (e) {
    setStatus("averaging error: " + e.message);
  }
}

// -- Streaming --------------------------------------------------------------

async function onStreamToggle() {
  try {
    const s = await refreshStatus();
    if (s.streaming) {
      await api("/api/stream/stop", { method: "POST" });
      if (eventSource) eventSource.close();
      eventSource = null;
    } else {
      await api("/api/stream/start", { method: "POST" });
      openEventSource();
    }
    await refreshStatus();
  } catch (e) {
    setStatus("stream error: " + e.message);
  }
}

function openEventSource() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource("/api/stream");
  eventSource.onmessage = (ev) => {
    const s = JSON.parse(ev.data);
    addSample(s.counter, s.channels);
  };
  eventSource.onerror = () => {
    // EventSource reconnects automatically; nothing to do here.
  };
}

// -- Wire up ----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initChart();
  buildChannelCheckboxes();
  document.getElementById("discover-btn").addEventListener("click", discover);
  document.getElementById("connect-btn").addEventListener("click", connect);
  document.getElementById("averaging").addEventListener("change", onAveragingChange);
  document.getElementById("stream-toggle").addEventListener("click", onStreamToggle);
  discover();
});
