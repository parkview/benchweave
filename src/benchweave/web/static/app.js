"use strict";

const CHANNEL_NAMES = ["a0", "a1", "a2", "a3", "a4", "a7"];
const CHANNEL_KEYS = ["A0", "A1", "A2", "A3", "A4", "A7"];
const COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#f032e6"];
const POINTS_PER_PERCENT = 5; // ~500 points at full width
let WINDOW_POINTS = 300; // window size, updated from config

let chart = null;
let eventSource = null;
let lastCounter = null;
let lastCounterAt = 0;
let heldCurrentMax = null; // held peak of the current (right) axis; null = auto

// -- Chart setup ------------------------------------------------------------

function initChart() {
  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      maintainAspectRatio: false,
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
  const shownPhysical = CHANNEL_KEYS.filter((key) => profile.channels[key].show !== false);
  const shownComputed = (profile.computed || []).filter((c) => c.show !== false);
  const total = shownPhysical.length + shownComputed.length;

  while (chart.data.datasets.length < total) {
    const i = chart.data.datasets.length;
    chart.data.datasets.push({
      label: "",
      data: [],
      borderColor: COLORS[i % COLORS.length],
      backgroundColor: COLORS[i % COLORS.length],
      borderWidth: 1,
      pointRadius: 0,
      parsing: false,
      yAxisID: "y",
    });
  }
  chart.data.datasets.length = total;

  shownPhysical.forEach((key, i) => {
    const ch = profile.channels[key];
    const color = ch.color || COLORS[i % COLORS.length];
    chart.data.datasets[i].label = ch.name;
    chart.data.datasets[i].borderColor = color;
    chart.data.datasets[i].backgroundColor = color;
    chart.data.datasets[i].yAxisID = ch.unit === "A" ? "y2" : "y";
  });
  shownComputed.forEach((comp, i) => {
    const ds = chart.data.datasets[shownPhysical.length + i];
    const color = comp.color || COLORS[(shownPhysical.length + i) % COLORS.length];
    ds.label = comp.name;
    ds.borderColor = color;
    ds.backgroundColor = color;
    ds.yAxisID = comp.unit === "A" ? "y2" : "y";
  });
  chart.update();
}

function addSample(counter, channels) {
  const windowMode = currentGraphMode() === "window";
  channels.forEach((ch, i) => {
    const ds = chart.data.datasets[i];
    if (!ds || ch.value === null) return;
    ds.data.push({ x: counter, y: ch.value });
    if (windowMode && ds.data.length > WINDOW_POINTS) ds.data.shift();
  });
  updatePeakHold(channels);
  chart.update();

  // Live incoming rate, derived from the board's sample counter delta.
  const now = performance.now();
  if (lastCounter !== null && now > lastCounterAt) {
    const rate = (counter - lastCounter) / ((now - lastCounterAt) / 1000);
    document.getElementById("throughput").textContent =
      Math.round(rate).toLocaleString() + " SPS";
  }
  lastCounter = counter;
  lastCounterAt = now;
}

function resetChart() {
  chart.data.datasets.forEach((ds) => {
    ds.data.length = 0;
  });
  lastCounter = null;
  lastCounterAt = 0;
  document.getElementById("throughput").textContent = "";
  clearPeakHoldState();
  chart.update();
}

function currentGraphMode() {
  const checked = document.querySelector('input[name="graph-mode"]:checked');
  return checked ? checked.value : "window";
}

function syncGraphMode() {
  const mode = currentGraphMode();
  if (mode === "window") {
    chart.data.datasets.forEach((ds) => {
      if (ds.data.length > WINDOW_POINTS) ds.data.splice(0, ds.data.length - WINDOW_POINTS);
    });
  }
  chart.update();
}

// -- Current-axis peak hold --------------------------------------------------

function currentPeakOfSample(channels) {
  let peak = null;
  for (const ch of channels) {
    if (ch.unit !== "A" || ch.value === null) continue;
    if (peak === null || ch.value > peak) peak = ch.value;
  }
  return peak;
}

function niceCeiling(v) {
  if (!isFinite(v) || v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const norm = v / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return nice * mag;
}

function updatePeakHold(channels) {
  const peak = currentPeakOfSample(channels);
  if (peak === null || peak <= 0) return;
  if (heldCurrentMax === null || peak > heldCurrentMax) {
    heldCurrentMax = peak;
    chart.options.scales.y2.max = niceCeiling(heldCurrentMax);
    document.getElementById("peak-current").textContent =
      "Peak: " + Number(heldCurrentMax.toPrecision(4)) + " A";
    document.getElementById("reset-peak").hidden = false;
  }
}

function clearPeakHoldState() {
  heldCurrentMax = null;
  delete chart.options.scales.y2.max;
  document.getElementById("peak-current").textContent = "";
  document.getElementById("reset-peak").hidden = true;
}

function resetPeakHold() {
  clearPeakHoldState();
  chart.update();
}

async function onSavePng() {
  if (!chart) return;
  const dataUrl = chart.toBase64Image("image/png", 1.0);
  const image = dataUrl.slice(dataUrl.indexOf(",") + 1);
  try {
    const res = await api("/api/graph/export", {
      method: "POST",
      body: JSON.stringify({ image }),
    });
    document.getElementById("graph-status").textContent = "saved";
    const reveal = document.getElementById("reveal-png");
    reveal.textContent = res.name;
    reveal.hidden = false;
  } catch (e) {
    document.getElementById("graph-status").textContent = "save error: " + e.message;
  }
}

async function onRevealPng() {
  try {
    await api("/api/graph/reveal", { method: "POST" });
  } catch (e) {
    document.getElementById("graph-status").textContent = "open error: " + e.message;
  }
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
      const note = document.getElementById("note").value;
      await api("/api/stream/start", {
        method: "POST",
        body: JSON.stringify({ record, note }),
      });
      resetChart();
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
  if (name === "setup") {
    loadConfig();
  } else {
    // The chart was hidden while on the setup tab; re-measure it now that it's
    // visible so the canvas fills the (possibly changed) width.
    requestAnimationFrame(() => {
      if (chart) chart.resize();
    });
  }
}

async function loadConfig() {
  try {
    currentConfig = await api("/api/config");
    renderProfileSelect();
    renderChannelTable();
    renderComputedTable();
    renderSettings();
    applyGraphSettings();
    updateChart();
    await updateMaxSps();
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
  CHANNEL_KEYS.forEach((key, i) => {
    const ch = profile.channels[key];
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${key}</td>` +
      `<td><input type="text" data-key="${key}" data-field="name" value="${ch.name}"></td>` +
      `<td><input type="text" data-key="${key}" data-field="unit" value="${ch.unit}"></td>` +
      `<td><input type="number" step="any" data-key="${key}" data-field="gain" value="${ch.gain}"></td>` +
      `<td><input type="number" step="any" data-key="${key}" data-field="offset" value="${ch.offset}"></td>` +
      `<td><input type="color" data-key="${key}" data-field="color" value="${
        ch.color || COLORS[i % COLORS.length]
      }"></td>` +
      `<td><input type="checkbox" data-key="${key}" data-field="show"${
        ch.show === false ? "" : " checked"
      }></td>`;
    tbody.appendChild(tr);
  });
}

function onProfileChange() {
  currentConfig.active_profile = document.getElementById("profile-select").value;
  renderChannelTable();
  renderComputedTable();
  updateChart();
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
    ch.show = document.querySelector(`input[data-key="${key}"][data-field="show"]`).checked;
    ch.color = document.querySelector(`input[data-key="${key}"][data-field="color"]`).value;
  });
  profile.computed = (profile.computed || []).map((_, i) => ({
    name: document.querySelector(`#computed-table input[data-index="${i}"][data-field="name"]`).value,
    unit: document.querySelector(`#computed-table input[data-index="${i}"][data-field="unit"]`).value,
    expr: document.querySelector(`#computed-table input[data-index="${i}"][data-field="expr"]`).value,
    show: document.querySelector(`#computed-table input[data-index="${i}"][data-field="show"]`).checked,
    color: document.querySelector(`#computed-table input[data-index="${i}"][data-field="color"]`).value,
  }));
  const srInput = document.getElementById("sample-rate");
  const gwInput = document.getElementById("graph-width");
  currentConfig.settings = currentConfig.settings || {};
  currentConfig.settings.sample_rate_hz = srInput.value === "" ? null : parseFloat(srInput.value);
  currentConfig.settings.graph_width =
    gwInput.value === "" ? null : parseInt(gwInput.value, 10);
  try {
    currentConfig = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify(currentConfig),
    });
    document.getElementById("config-status").textContent = "saved";
    renderProfileSelect();
    renderChannelTable();
    renderComputedTable();
    renderSettings();
    updateChart();
    applyGraphSettings();
  } catch (e) {
    document.getElementById("config-status").textContent = "save error: " + e.message;
  }
}

function renderComputedTable() {
  const tbody = document.querySelector("#computed-table tbody");
  tbody.innerHTML = "";
  const profile = currentConfig.profiles[currentConfig.active_profile];
  (profile.computed || []).forEach((comp, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input type="text" data-index="${i}" data-field="name" value="${comp.name || ""}"></td>` +
      `<td><input type="text" data-index="${i}" data-field="unit" value="${comp.unit || ""}"></td>` +
      `<td><input type="text" data-index="${i}" data-field="expr" value="${comp.expr || ""}"></td>` +
      `<td><input type="color" data-index="${i}" data-field="color" value="${
        comp.color || COLORS[(CHANNEL_KEYS.length + i) % COLORS.length]
      }"></td>` +
      `<td><input type="checkbox" data-index="${i}" data-field="show"${
        comp.show === false ? "" : " checked"
      }></td>` +
      `<td><button data-delete="${i}">✕</button></td>`;
    tr.querySelector("button").addEventListener("click", () => onDeleteComputed(i));
    tbody.appendChild(tr);
  });
}

function renderSettings() {
  const s = currentConfig.settings || {};
  const el = document.getElementById("sample-rate");
  el.value = s.sample_rate_hz == null ? "" : String(s.sample_rate_hz);
  const gw = document.getElementById("graph-width");
  gw.value = s.graph_width == null ? "" : String(s.graph_width);
}

function applyGraphSettings() {
  const s = (currentConfig && currentConfig.settings) || {};
  const width = Math.min(100, Math.max(1, Number(s.graph_width) || 100));
  WINDOW_POINTS = Math.max(10, Math.round(width * POINTS_PER_PERCENT));
  document.getElementById("graph").style.width = width + "%";
  document.getElementById("window-count").textContent = WINDOW_POINTS;
  // Resize after the browser reflows the new width; a synchronous resize here
  // reads a stale container size (zero while the graph tab is hidden).
  requestAnimationFrame(() => {
    if (chart) chart.resize();
  });
}

async function updateMaxSps() {
  try {
    const s = await api("/api/status");
    const el = document.getElementById("max-sps");
    const input = document.getElementById("sample-rate");
    if (s.max_sps) {
      el.textContent = `max ~${Math.round(s.max_sps)} Hz`;
      input.max = String(Math.ceil(s.max_sps));
    } else {
      el.textContent = "";
      input.removeAttribute("max");
    }
  } catch (e) {
    // status unavailable; leave the max hint empty
  }
}

function onNewProfile() {
  const name = document.getElementById("new-profile-name").value.trim();
  if (!name || currentConfig.profiles[name]) return;
  currentConfig.profiles[name] = JSON.parse(
    JSON.stringify(currentConfig.profiles[currentConfig.active_profile])
  );
  currentConfig.active_profile = name;
  document.getElementById("new-profile-name").value = "";
  renderProfileSelect();
  renderChannelTable();
  renderComputedTable();
  updateChart();
}

function onAddComputed() {
  const profile = currentConfig.profiles[currentConfig.active_profile];
  profile.computed = profile.computed || [];
  const i = profile.computed.length;
  profile.computed.push({
    name: "",
    unit: "A",
    expr: "",
    color: COLORS[(CHANNEL_KEYS.length + i) % COLORS.length],
  });
  renderComputedTable();
}

function onDeleteComputed(index) {
  const profile = currentConfig.profiles[currentConfig.active_profile];
  profile.computed = profile.computed || [];
  profile.computed.splice(index, 1);
  renderComputedTable();
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
  document.querySelectorAll('input[name="graph-mode"]').forEach((r) =>
    r.addEventListener("change", syncGraphMode)
  );
  document.getElementById("save-png").addEventListener("click", onSavePng);
  document.getElementById("reveal-png").addEventListener("click", onRevealPng);
  document.getElementById("reset-peak").addEventListener("click", resetPeakHold);
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab))
  );
  document.getElementById("profile-select").addEventListener("change", onProfileChange);
  document.getElementById("save-config").addEventListener("click", onSaveConfig);
  document.getElementById("new-profile").addEventListener("click", onNewProfile);
  document.getElementById("add-computed").addEventListener("click", onAddComputed);
  discover();
});
