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

// -- Analyse tab state ------------------------------------------------------

let analyseChart = null;
let analyseData = null; // { series:[{name,unit,points:[[t,v],...]}], meta, ... }
let analyseRecords = []; // scan() output, for join with projects/stems
let analyseProjects = []; // [{name, retention_days}]
let analyseZoom = { min: null, max: null }; // current x-axis zoom window (elapsed s)
let brushStart = null;
let brushEnd = null;
let brushDragging = false;

const brushPlugin = {
  id: "brush",
  afterDraw(chart, _args) {
    if (brushStart === null || brushEnd === null) return;
    const x = chart.scales.x;
    const x1 = x.getPixelForValue(Math.min(brushStart, brushEnd));
    const x2 = x.getPixelForValue(Math.max(brushStart, brushEnd));
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = "rgba(67, 99, 216, 0.14)";
    ctx.fillRect(x1, chart.chartArea.top, x2 - x1, chart.chartArea.bottom - chart.chartArea.top);
    ctx.strokeStyle = "rgba(67, 99, 216, 0.85)";
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(x1, chart.chartArea.top, x2 - x1, chart.chartArea.bottom - chart.chartArea.top);
    ctx.restore();
  },
};

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

function initAnalyseChart() {
  const el = document.getElementById("analyse-chart");
  analyseChart = new Chart(el, {
    type: "line",
    data: { datasets: [] },
    plugins: [brushPlugin],
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      parsing: false,
      scales: {
        x: {
          type: "linear",
          title: { display: true, text: "elapsed (s)" },
        },
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
  attachAnalyseChartEvents(el);
}

function attachAnalyseChartEvents(el) {
  // Wheel → zoom the x-axis around the cursor.
  el.addEventListener(
    "wheel",
    (ev) => {
      if (!analyseChart || !analyseData) return;
      ev.preventDefault();
      const x = analyseChart.scales.x;
      const lo = x.min;
      const hi = x.max;
      const cursor = x.getValueForPixel(ev.offsetX);
      const factor = ev.deltaY < 0 ? 0.7 : 1.4;
      let nlo = cursor - (cursor - lo) * factor;
      let nhi = cursor + (hi - cursor) * factor;
      if (nhi - nlo < 1e-6) return;
      analyseZoom = { min: nlo, max: nhi };
      x.min = nlo;
      x.max = nhi;
      analyseChart.update("none");
    },
    { passive: false }
  );

  // Drag → brush a region and compute its integral.
  el.addEventListener("mousedown", (ev) => {
    if (!analyseChart || !analyseData) return;
    brushDragging = true;
    brushStart = analyseChart.scales.x.getValueForPixel(ev.offsetX);
    brushEnd = brushStart;
    analyseChart.update("none");
  });
  window.addEventListener("mousemove", (ev) => {
    if (!brushDragging || !analyseChart) return;
    const rect = analyseChart.canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    if (px < analyseChart.chartArea.left || px > analyseChart.chartArea.right) return;
    brushEnd = analyseChart.scales.x.getValueForPixel(px);
    updateIntegralReadout();
    analyseChart.update("none");
  });
  window.addEventListener("mouseup", () => {
    if (!brushDragging) return;
    brushDragging = false;
    updateIntegralReadout();
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
  const pause = document.getElementById("pause-toggle");
  pause.hidden = !s.streaming;
  pause.textContent = s.paused ? "Resume" : "Pause";
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

async function onPauseToggle() {
  try {
    const s = await refreshStatus();
    if (s.paused) {
      await api("/api/stream/resume", { method: "POST" });
    } else {
      await api("/api/stream/pause", { method: "POST" });
    }
    await refreshStatus();
  } catch (e) {
    setStatus("pause error: " + e.message);
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
  document.getElementById("tab-analyse").hidden = name !== "analyse";
  if (name === "setup") {
    loadConfig();
  } else if (name === "analyse") {
    refreshAnalyse();
    requestAnimationFrame(() => {
      if (analyseChart) analyseChart.resize();
    });
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

// -- Analyse tab ------------------------------------------------------------

function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
  return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

function formatSigned(v) {
  const a = Math.abs(v);
  const s = a >= 100 ? a.toFixed(1) : a >= 1 ? a.toFixed(3) : a.toPrecision(3);
  return (v < 0 ? "-" : "+") + s;
}

function formatNumber(v) {
  const a = Math.abs(v);
  const s = a >= 100 ? a.toFixed(1) : a >= 1 ? a.toFixed(3) : a.toPrecision(3);
  return (v < 0 ? "-" : "") + s;
}

function formatTime(t) {
  if (t == null || !Number.isFinite(t)) return "—";
  if (t < 0.001) return (t * 1e6).toFixed(0) + " µs";
  if (t < 1) return (t * 1e3).toFixed(2) + " ms";
  return t.toFixed(3) + " s";
}

async function refreshAnalyse() {
  try {
    await loadAnalyseProjects();
    await loadAnalyseCaptures();
    await renderStorage();
    renderRetention();
    document.getElementById("analyse-status").textContent = "";
  } catch (e) {
    document.getElementById("analyse-status").textContent = "load error: " + e.message;
  }
}

async function loadAnalyseProjects() {
  analyseProjects = await api("/api/projects");
  renderProjectSelect();
}

function renderProjectSelect() {
  const sel = document.getElementById("analyse-project-select");
  const keep = sel.value;
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "(no project)";
  sel.appendChild(none);
  analyseProjects.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = p.retention_days == null ? `${p.name} (inherit)` : `${p.name} (${p.retention_days}d)`;
    sel.appendChild(o);
  });
  if (keep) sel.value = keep;
}

async function loadAnalyseCaptures() {
  analyseRecords = await api("/api/captures");
  renderCsvList();
  renderPngList();
}

function renderCsvList() {
  const ul = document.getElementById("analyse-csv-list");
  ul.innerHTML = "";
  const csvs = analyseRecords.filter((r) => r.kind === "csv");
  if (csvs.length === 0) {
    const li = document.createElement("li");
    li.textContent = "(no CSV captures)";
    ul.appendChild(li);
    return;
  }
  csvs.forEach((r) => {
    const li = document.createElement("li");
    if (r.expired) li.classList.add("expired");

    const name = document.createElement("span");
    name.className = "fname";
    name.textContent = r.name;
    name.title = "Click to plot all channels";
    name.addEventListener("click", () => loadCapture(r.stem));
    li.appendChild(name);

    const meta = document.createElement("span");
    meta.className = "fmeta";
    meta.textContent = `${(r.captured_at || "").replace("T", " ").slice(5, 16)} · ${fmtBytes(r.size_bytes)}`;
    li.appendChild(meta);

    const sel = document.createElement("select");
    sel.className = "proj";
    sel.title = "Assign this capture to a project";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "(none)";
    sel.appendChild(none);
    analyseProjects.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.name;
      sel.appendChild(o);
    });
    sel.value = r.project || "";
    sel.addEventListener("change", () => assignProject(r.stem, sel.value || null));
    li.appendChild(sel);

    ul.appendChild(li);
  });
}

function renderPngList() {
  const ul = document.getElementById("analyse-png-list");
  ul.innerHTML = "";
  const pngs = analyseRecords.filter((r) => r.kind === "png");
  if (pngs.length === 0) {
    const li = document.createElement("li");
    li.textContent = "(no PNG charts)";
    ul.appendChild(li);
    return;
  }
  pngs.forEach((r) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.className = "fname";
    a.textContent = r.name;
    a.href = `/api/captures/${r.stem}/file?ext=png`;
    a.target = "_blank";
    a.title = "Open the chart image in a new tab";
    li.appendChild(a);
    const meta = document.createElement("span");
    meta.className = "fmeta";
    meta.textContent = `${(r.captured_at || "").replace("T", " ").slice(5, 16)} · ${fmtBytes(r.size_bytes)}`;
    li.appendChild(meta);
    ul.appendChild(li);
  });
}

async function renderStorage() {
  try {
    const stats = await api("/api/captures/storage");
    document.getElementById("analyse-storage-total").textContent =
      `Total: ${fmtBytes(stats.total_bytes)} across ${stats.file_count} file(s)`;
    const tbody = document.querySelector("#analyse-storage-table tbody");
    tbody.innerHTML = "";
    stats.per_project.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${p.project}</td><td>${p.count}</td><td>${fmtBytes(p.bytes)}</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) {
    document.getElementById("analyse-status").textContent = "storage error: " + e.message;
  }
}

function renderRetention() {
  const expired = analyseRecords.filter((r) => r.expired);
  const section = document.getElementById("analyse-retention");
  const ul = document.getElementById("analyse-expired-list");
  ul.innerHTML = "";
  if (expired.length === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  [...new Set(expired.map((r) => r.stem))].forEach((stem) => {
    const rec = analyseRecords.find((r) => r.stem === stem);
    const li = document.createElement("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.dataset.stem = stem;
    cb.title = "Include this capture when moving to trash";
    li.appendChild(cb);
    const label = document.createElement("span");
    label.className = "fname";
    label.textContent = rec ? rec.name.replace(/\.[^.]+$/, "") : stem;
    label.title = stem;
    li.appendChild(label);
    const meta = document.createElement("span");
    meta.className = "fmeta";
    meta.textContent = rec && rec.project ? rec.project : "";
    li.appendChild(meta);
    ul.appendChild(li);
  });
}

let analyseCurrentStem = null;

async function loadCapture(stem) {
  try {
    const data = await api(`/api/captures/${stem}/data`);
    analyseCurrentStem = stem;
    analyseData = data;
    analyseZoom = { min: null, max: null };
    brushStart = brushEnd = null;
    document.getElementById("analyse-viewer").hidden = false;
    document.getElementById("analyse-file-name").textContent = data.name;
    const metaBits = [];
    if (data.meta) {
      if (data.meta.note) metaBits.push(data.meta.note);
      if (data.meta.sample_rate_hz) metaBits.push(`${data.meta.sample_rate_hz} Hz`);
    }
    metaBits.push(`${data.sample_count.toLocaleString()} samples`);
    metaBits.push(`${data.duration_s.toFixed(1)} s`);
    document.getElementById("analyse-file-meta").textContent = " — " + metaBits.join(" · ");
    document.getElementById("analyse-integral").textContent = "";
    clearRegionStats();
    clearEdgeAnalysis();
    renderVoltageSelect();
    renderEdgeSelect();
    renderAnalyseChart();
  } catch (e) {
    document.getElementById("analyse-status").textContent = "plot error: " + e.message;
  }
}

function renderVoltageSelect() {
  const sel = document.getElementById("analyse-v-select");
  sel.innerHTML = "";
  const series = analyseData.series;
  let defIdx = series.findIndex((s) => (s.unit || "").toUpperCase() === "V");
  if (defIdx < 0) defIdx = 0;
  series.forEach((s, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = `${s.name} (${s.unit || "?"})`;
    sel.appendChild(o);
  });
  sel.value = String(defIdx);
}

function renderAnalyseChart() {
  const series = analyseData.series;
  analyseChart.data.datasets = series.map((s, i) => {
    const color = COLORS[i % COLORS.length];
    const isA = (s.unit || "").toUpperCase() === "A";
    return {
      label: `${s.name} (${s.unit || ""})`,
      data: s.points.map(([t, v]) => ({ x: t, y: v })),
      borderColor: color,
      backgroundColor: color,
      borderWidth: 1,
      pointRadius: 0,
      yAxisID: isA ? "y2" : "y",
    };
  });
  const x = analyseChart.options.scales.x;
  if (analyseZoom.min == null) delete x.min;
  else x.min = analyseZoom.min;
  if (analyseZoom.max == null) delete x.max;
  else x.max = analyseZoom.max;
  analyseChart.update();
  analyseChart.resize();
}

function currentSeriesIndex() {
  return analyseData.series.findIndex((s) => (s.unit || "").toUpperCase() === "A");
}

function powerPoints(V, I) {
  const n = Math.min(V.length, I.length);
  const pts = [];
  for (let k = 0; k < n; k++) {
    const t = V[k][0];
    const v = V[k][1] == null ? 0 : V[k][1];
    const i = I[k][1] == null ? 0 : I[k][1];
    pts.push([t, v * i]);
  }
  return pts;
}

function trapezoid(points, lo, hi) {
  let total = 0;
  for (let k = 0; k < points.length - 1; k++) {
    const t0 = points[k][0];
    const t1 = points[k + 1][0];
    const v0 = points[k][1];
    const v1 = points[k + 1][1];
    const a = Math.max(t0, lo);
    const b = Math.min(t1, hi);
    if (b <= a) continue;
    const span = t1 - t0 || 1;
    const va = v0 + (v1 - v0) * ((a - t0) / span);
    const vb = v0 + (v1 - v0) * ((b - t0) / span);
    total += ((va + vb) / 2) * (b - a);
  }
  return total;
}

function regionStats(points, lo, hi) {
  let count = 0;
  let min = Infinity;
  let max = -Infinity;
  let sum = 0;
  let sumSq = 0;
  for (const [t, v] of points) {
    if (v == null || t < lo || t > hi) continue;
    count += 1;
    if (v < min) min = v;
    if (v > max) max = v;
    sum += v;
    sumSq += v * v;
  }
  if (count === 0) return null;
  const mean = sum / count;
  return { count, min, max, mean, rms: Math.sqrt(sumSq / count), pp: max - min };
}

function powerRegionStats(V, I, lo, hi) {
  let count = 0;
  let sum = 0;
  let peak = -Infinity;
  const n = Math.min(V.length, I.length);
  for (let k = 0; k < n; k++) {
    const t = V[k][0];
    if (t < lo || t > hi) continue;
    const v = V[k][1] == null ? 0 : V[k][1];
    const i = I[k][1] == null ? 0 : I[k][1];
    const p = v * i;
    count += 1;
    sum += p;
    if (p > peak) peak = p;
  }
  if (count === 0) return null;
  return { mean: sum / count, peak };
}

function integrateRegion(x1, x2) {
  if (!analyseData) return null;
  const lo = Math.min(x1, x2);
  const hi = Math.max(x1, x2);
  const iIdx = currentSeriesIndex();
  const vIdx = Number(document.getElementById("analyse-v-select").value) || 0;

  const result = { wh: null, ah: null };
  if (iIdx >= 0) {
    result.ah = trapezoid(analyseData.series[iIdx].points, lo, hi) / 3600;
  }
  if (iIdx >= 0 && vIdx >= 0 && vIdx < analyseData.series.length) {
    const power = powerPoints(analyseData.series[vIdx].points, analyseData.series[iIdx].points);
    result.wh = trapezoid(power, lo, hi) / 3600;
  }
  return result;
}

function updateIntegralReadout() {
  if (!analyseData || brushStart === null || brushEnd === null) return;
  const el = document.getElementById("analyse-integral");
  const lo = Math.min(brushStart, brushEnd);
  const hi = Math.max(brushStart, brushEnd);
  if (Math.abs(brushEnd - brushStart) < 1e-9) {
    el.textContent = "";
    clearRegionStats();
    clearEdgeAnalysis();
    return;
  }
  const r = integrateRegion(brushStart, brushEnd);
  if (!r) {
    el.textContent = "";
    clearRegionStats();
    clearEdgeAnalysis();
    return;
  }
  const bits = [];
  if (r.ah != null) bits.push(`${formatSigned(r.ah)} Ah`);
  if (r.wh != null) bits.push(`${formatSigned(r.wh)} Wh`);
  const iIdx = currentSeriesIndex();
  const vIdx = Number(document.getElementById("analyse-v-select").value) || 0;
  if (iIdx >= 0 && vIdx >= 0 && vIdx < analyseData.series.length) {
    const p = powerRegionStats(
      analyseData.series[vIdx].points,
      analyseData.series[iIdx].points,
      lo,
      hi
    );
    if (p) bits.push(`${formatNumber(p.mean)} W avg`, `${formatNumber(p.peak)} W pk`);
  }
  el.textContent = "∫ " + bits.join("  ·  ");
  renderRegionStats(lo, hi);
  renderEdgeAnalysis(lo, hi);
}

function clearRegionStats() {
  document.getElementById("analyse-region-stats").hidden = true;
  document.getElementById("analyse-region-stats-body").innerHTML = "";
  document.getElementById("analyse-region-summary").textContent = "";
}

function renderRegionStats(lo, hi) {
  const section = document.getElementById("analyse-region-stats");
  const tbody = document.getElementById("analyse-region-stats-body");
  tbody.innerHTML = "";
  let any = false;
  for (const s of analyseData.series) {
    const st = regionStats(s.points, lo, hi);
    if (!st) continue;
    any = true;
    const tr = document.createElement("tr");
    const cells = [
      s.name,
      s.unit || "",
      formatNumber(st.min),
      formatNumber(st.mean),
      formatNumber(st.max),
      formatNumber(st.rms),
      formatNumber(st.pp),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  if (!any) {
    section.hidden = true;
    document.getElementById("analyse-region-summary").textContent = "";
    return;
  }
  document.getElementById("analyse-region-summary").textContent =
    `Region ${formatNumber(lo)} s → ${formatNumber(hi)} s (Δ ${formatNumber(hi - lo)} s)`;
  section.hidden = false;
}

function regionPoints(points, lo, hi) {
  const out = [];
  for (const [t, v] of points) {
    if (v != null && t >= lo && t <= hi) out.push([t, v]);
  }
  return out;
}

function meanLevel(pts) {
  if (pts.length === 0) return null;
  let s = 0;
  for (const [, v] of pts) s += v;
  return s / pts.length;
}

// First time the series crosses `level`, travelling up (dir=+1) or down (dir=-1).
// Interpolates between samples; null if it never crosses.
function crossTime(pts, level, dir) {
  for (let k = 0; k < pts.length - 1; k++) {
    const [t0, v0] = pts[k];
    const [t1, v1] = pts[k + 1];
    const below0 = dir > 0 ? v0 < level : v0 > level;
    const below1 = dir > 0 ? v1 < level : v1 > level;
    if (below0 === below1) continue;
    const frac = (level - v0) / (v1 - v0);
    return t0 + frac * (t1 - t0);
  }
  return null;
}

function edgeAnalysis(points, lo, hi, settlePct) {
  const pts = regionPoints(points, lo, hi);
  if (pts.length < 3) return null;
  const n = pts.length;
  const headN = Math.max(1, Math.floor(n * 0.15));
  const tailN = Math.max(1, Math.floor(n * 0.15));
  const baseline = meanLevel(pts.slice(0, headN));
  const final = meanLevel(pts.slice(n - tailN));
  if (baseline == null || final == null) return null;
  const step = final - baseline;
  const scale = Math.max(Math.abs(baseline), Math.abs(final), 1e-12);
  if (Math.abs(step) < 0.005 * scale) return null; // flat region: no transition

  const rising = step > 0;
  const dir = rising ? 1 : -1;
  const t10 = crossTime(pts, baseline + 0.10 * step, dir);
  const t90 = crossTime(pts, baseline + 0.90 * step, dir);
  const riseTime = t10 != null && t90 != null ? t90 - t10 : null;

  const tol = (settlePct / 100) * Math.abs(step);
  const bandLo = final - tol;
  const bandHi = final + tol;
  let lastOutside = -1;
  for (let k = 0; k < n; k++) {
    if (t10 != null && pts[k][0] < t10) continue;
    const v = pts[k][1];
    if (v < bandLo || v > bandHi) lastOutside = k;
  }
  const t0 = t10 != null ? t10 : pts[0][0];
  let settleTime = null;
  if (lastOutside < 0) settleTime = 0;
  else if (lastOutside + 1 < n) settleTime = Math.max(0, pts[lastOutside + 1][0] - t0);

  return { rising, baseline, final, step, riseTime, settleTime };
}

function clearEdgeAnalysis() {
  document.getElementById("analyse-edge").hidden = true;
  document.getElementById("analyse-edge-readout").textContent = "";
}

function renderEdgeSelect() {
  const sel = document.getElementById("analyse-edge-select");
  sel.innerHTML = "";
  const series = analyseData.series;
  let defIdx = series.findIndex((s) => (s.unit || "").toUpperCase() === "V");
  if (defIdx < 0) defIdx = 0;
  series.forEach((s, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = `${s.name} (${s.unit || "?"})`;
    sel.appendChild(o);
  });
  sel.value = String(defIdx);
}

function renderEdgeAnalysis(lo, hi) {
  const panel = document.getElementById("analyse-edge");
  const readout = document.getElementById("analyse-edge-readout");
  const sel = document.getElementById("analyse-edge-select");
  if (!analyseData || brushStart === null || brushEnd === null) {
    panel.hidden = true;
    return;
  }
  const idx = Number(sel.value) || 0;
  const series = analyseData.series[idx];
  if (!series) {
    panel.hidden = true;
    return;
  }
  const rawPct = Number(document.getElementById("analyse-settle-pct").value);
  const settlePct = Number.isFinite(rawPct) && rawPct > 0 ? rawPct : 2;
  const e = edgeAnalysis(series.points, lo, hi, settlePct);
  if (!e) {
    readout.textContent = "no clear transition in the region";
    panel.hidden = false;
    return;
  }
  const bits = [];
  bits.push(e.rising ? "rising" : "falling");
  bits.push(`${formatNumber(e.baseline)} → ${formatNumber(e.final)} ${series.unit || ""}`);
  bits.push(`t10–90 ${e.riseTime != null ? formatTime(e.riseTime) : "—"}`);
  bits.push(`settle ±${settlePct}% ${e.settleTime != null ? formatTime(e.settleTime) : "—"}`);
  readout.textContent = bits.join("  ·  ");
  panel.hidden = false;
}

function onResetZoom() {
  analyseZoom = { min: null, max: null };
  brushStart = brushEnd = null;
  document.getElementById("analyse-integral").textContent = "";
  clearRegionStats();
  clearEdgeAnalysis();
  if (analyseChart) {
    delete analyseChart.options.scales.x.min;
    delete analyseChart.options.scales.x.max;
    analyseChart.update();
  }
}

function onVoltageChange() {
  updateIntegralReadout();
}

async function assignProject(stem, project) {
  try {
    await api(`/api/captures/${stem}/project`, {
      method: "POST",
      body: JSON.stringify({ project }),
    });
    await loadAnalyseCaptures();
    renderStorage();
    renderRetention();
  } catch (e) {
    document.getElementById("analyse-status").textContent = "assign error: " + e.message;
  }
}

async function onNewProject() {
  const name = document.getElementById("analyse-new-project").value.trim();
  const rdInput = document.getElementById("analyse-project-retention");
  const retention = rdInput.value === "" ? null : parseInt(rdInput.value, 10);
  if (!name) return;
  try {
    await api("/api/projects", { method: "POST", body: JSON.stringify({ name, retention_days: retention }) });
    document.getElementById("analyse-new-project").value = "";
    await loadAnalyseProjects();
    await loadAnalyseCaptures();
    renderStorage();
    renderRetention();
  } catch (e) {
    document.getElementById("analyse-status").textContent = "project error: " + e.message;
  }
}

async function onSetRetention() {
  const name = document.getElementById("analyse-project-select").value;
  const rdInput = document.getElementById("analyse-project-retention");
  const retention = rdInput.value === "" ? null : parseInt(rdInput.value, 10);
  if (!name) return;
  try {
    await api(`/api/projects/${encodeURIComponent(name)}`, {
      method: "PATCH",
      body: JSON.stringify({ retention_days: retention }),
    });
    await loadAnalyseProjects();
    await loadAnalyseCaptures();
    renderStorage();
    renderRetention();
  } catch (e) {
    document.getElementById("analyse-status").textContent = "retention error: " + e.message;
  }
}

async function onTrashSelected() {
  const boxes = document.querySelectorAll("#analyse-expired-list input[type=checkbox]:checked");
  const stems = [...boxes].map((cb) => cb.dataset.stem);
  if (stems.length === 0) return;
  try {
    const res = await api("/api/captures/trash", { method: "POST", body: JSON.stringify({ stems }) });
    document.getElementById("analyse-status").textContent =
      `trashed ${res.trashed.length} file(s)` + (res.errors.length ? `, ${res.errors.length} failed` : "");
    await loadAnalyseCaptures();
    renderStorage();
    renderRetention();
  } catch (e) {
    document.getElementById("analyse-status").textContent = "trash error: " + e.message;
  }
}

async function onApplyConfig() {
  if (!analyseCurrentStem) return;
  try {
    await api(`/api/captures/${analyseCurrentStem}/apply-config`, { method: "POST" });
    document.getElementById("analyse-status").textContent = "config applied to board";
    await loadConfig();
  } catch (e) {
    document.getElementById("analyse-status").textContent = "apply error: " + e.message;
  }
}

// -- Wire up ----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initChart();
  initAnalyseChart();
  loadConfig();
  buildChannelCheckboxes();
  document.getElementById("discover-btn").addEventListener("click", discover);
  document.getElementById("connect-btn").addEventListener("click", connect);
  document.getElementById("averaging").addEventListener("change", onAveragingChange);
  document.getElementById("stream-toggle").addEventListener("click", onStreamToggle);
  document.getElementById("pause-toggle").addEventListener("click", onPauseToggle);
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

  document.getElementById("analyse-refresh").addEventListener("click", refreshAnalyse);
  document.getElementById("analyse-new-project-btn").addEventListener("click", onNewProject);
  document.getElementById("analyse-set-retention").addEventListener("click", onSetRetention);
  document.getElementById("analyse-trash-selected").addEventListener("click", onTrashSelected);
  document.getElementById("analyse-apply-config").addEventListener("click", onApplyConfig);
  document.getElementById("analyse-reset-zoom").addEventListener("click", onResetZoom);
  document.getElementById("analyse-v-select").addEventListener("change", onVoltageChange);
  document.getElementById("analyse-edge-select").addEventListener("change", updateIntegralReadout);
  document.getElementById("analyse-settle-pct").addEventListener("input", updateIntegralReadout);

  discover();
});
