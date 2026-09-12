"use strict";

const CHANNEL_NAMES = ["a0", "a1", "a2", "a3", "a4", "a7"];
const CHANNEL_KEYS = ["A0", "A1", "A2", "A3", "A4", "A7"];
const COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#f032e6"];
const MAX_POINTS = 300;

let chart = null;
let eventSource = null;

// -- Chart setup ------------------------------------------------------------

function initChart() {
  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: {
      datasets: CHANNEL_KEYS.map((key, i) => ({
        label: key,
        data: [],
        borderColor: COLORS[i],
        backgroundColor: COLORS[i],
        borderWidth: 1,
        pointRadius: 0,
        parsing: false,
        yAxisID: "y",
      })),
    },
    options: {
      animation: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "linear", title: { display: true, text: "sample counter" } },
        y: {
          type: "linear",
          beginAtZero: true,
          title: { display: true, text: "voltage (V)" },
        },
        y2: {
          type: "linear",
          position: "right",
          beginAtZero: true,
          grid: { drawOnChartArea: false },
          title: { display: true, text: "current (A)" },
        },
      },
    },
  });
}

function updateChart() {
  if (!chart || !currentConfig) return;
  const profile = currentConfig.profiles[currentConfig.active_profile];
  CHANNEL_KEYS.forEach((key, i) => {
    const ch = profile.channels[key];
    chart.data.datasets[i].label = ch.name;
    chart.data.datasets[i].yAxisID = ch.unit === "A" ? "y2" : "y";
  });
  chart.update();
}

function addSample(counter, channels) {
  chart.data.datasets.forEach((ds, i) => {
    ds.data.push({ x: counter, y: channels[i].value });
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
  document.getElementById("control").hidden = !s.connected;
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
      const record = document.getElementById("record").checked;
      await api("/api/stream/start", {
        method: "POST",
        body: JSON.stringify({ record }),
      });
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

// -- Configuration tab ------------------------------------------------------

let currentConfig = null;

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.getElementById("tab-control").hidden = name !== "control";
  document.getElementById("tab-setup").hidden = name !== "setup";
  if (name === "setup") loadConfig();
}

async function loadConfig() {
  try {
    currentConfig = await api("/api/config");
    renderProfileSelect();
    renderChannelTable();
    updateChart();
    document.getElementById("config-status").textContent = "";
  } catch (e) {
    document.getElementById("config-status").textContent = "load error: " + e.message;
  }
}

function renderProfileSelect() {
  const select = document.getElementById("profile-select");
  select.innerHTML = "";
  Object.keys(currentConfig.profiles).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    select.appendChild(opt);
  });
  select.value = currentConfig.active_profile;
}

function renderChannelTable() {
  const tbody = document.querySelector("#channel-table tbody");
  tbody.innerHTML = "";
  const profile = currentConfig.profiles[currentConfig.active_profile];
  CHANNEL_KEYS.forEach((key) => {
    const ch = profile.channels[key];
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${key}</td>` +
      `<td><input type="text" data-key="${key}" data-field="name" value="${ch.name}"></td>` +
      `<td><input type="text" data-key="${key}" data-field="unit" value="${ch.unit}"></td>` +
      `<td><input type="number" step="any" data-key="${key}" data-field="gain" value="${ch.gain}"></td>` +
      `<td><input type="number" step="any" data-key="${key}" data-field="offset" value="${ch.offset}"></td>`;
    tbody.appendChild(tr);
  });
}

function onProfileChange() {
  currentConfig.active_profile = document.getElementById("profile-select").value;
  renderChannelTable();
}

async function onSaveConfig() {
  const profile = currentConfig.profiles[currentConfig.active_profile];
  CHANNEL_KEYS.forEach((key) => {
    const ch = profile.channels[key];
    ch.name = document.querySelector(`input[data-key="${key}"][data-field="name"]`).value;
    ch.unit = document.querySelector(`input[data-key="${key}"][data-field="unit"]`).value;
    ch.gain = parseFloat(
      document.querySelector(`input[data-key="${key}"][data-field="gain"]`).value
    );
    ch.offset = parseFloat(
      document.querySelector(`input[data-key="${key}"][data-field="offset"]`).value
    );
  });
  try {
    currentConfig = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify(currentConfig),
    });
    document.getElementById("config-status").textContent = "saved";
    renderProfileSelect();
    renderChannelTable();
    updateChart();
  } catch (e) {
    document.getElementById("config-status").textContent = "save error: " + e.message;
  }
}

// -- Wire up ----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initChart();
  loadConfig();
  buildChannelCheckboxes();
  document.getElementById("discover-btn").addEventListener("click", discover);
  document.getElementById("connect-btn").addEventListener("click", connect);
  document.getElementById("averaging").addEventListener("change", onAveragingChange);
  document.getElementById("stream-toggle").addEventListener("click", onStreamToggle);
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab))
  );
  document.getElementById("profile-select").addEventListener("change", onProfileChange);
  document.getElementById("save-config").addEventListener("click", onSaveConfig);
  discover();
});
