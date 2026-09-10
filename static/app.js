/* 间歇式呼吸室分析工作台 —— 原生 JS 前端 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const SVGNS = "http://www.w3.org/2000/svg";

const state = {
  datasets: [],
  currentId: null,
  points: [],          // 原始点（分析后替换为修正曲线 corrected）
  corrected: null,     // 最近一次分析返回的修正曲线
  window: null,        // [t0, t1]
  calibPoints: [],     // [[t, expected], ...]
  calibMode: false,
  lastResult: null,
  seriesList: [],      // 背景序列（时变空白）
  blankRates: {},      // 空白数据集 id -> {rate, volume, version_id}
};

// ------------------------------------------------------------ 通用

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "请求失败");
  return data;
}

function fmt(x, nd = 5) {
  return typeof x === "number" && isFinite(x) ? x.toFixed(nd) : "—";
}

// ------------------------------------------------------------ 视图切换

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
    $("#view-" + btn.dataset.view).classList.remove("hidden");
    if (btn.dataset.view === "groups") loadGroups();
    if (btn.dataset.view === "batch") initBatchView();
    if (btn.dataset.view === "series") initSeriesView();
  });
});

// ------------------------------------------------------------ 上传

$("#btn-upload").addEventListener("click", async () => {
  const file = $("#file-input").files[0];
  const msg = $("#upload-msg");
  if (!file) { msg.textContent = "请先选择 CSV 文件"; msg.className = "msg err"; return; }
  const text = await file.text();
  const params = new URLSearchParams({
    name: $("#up-name").value || file.name.replace(/\.csv$/i, ""),
    sample_id: $("#up-sample").value,
    is_blank: $("#up-blank").checked ? "1" : "0",
    filename: file.name,
  });
  try {
    const data = await api("/api/upload?" + params, { method: "POST", body: text });
    let s = `已上传，${data.n_points} 个数据点`;
    if (data.parse_errors && data.parse_errors.length)
      s += `\n跳过 ${data.parse_errors.length} 行: ` + data.parse_errors.slice(0, 3).join("；");
    msg.textContent = s; msg.className = "msg";
    await loadDatasets();
    selectDataset(data.id);
  } catch (e) { msg.textContent = e.message; msg.className = "msg err"; }
});

// ------------------------------------------------------------ 数据集列表

async function loadDatasets() {
  const data = await api("/api/datasets");
  state.datasets = data.datasets;
  try {
    const sdata = await api("/api/series");
    state.seriesList = sdata.series || [];
    state.blankRates = sdata.blank_rates || {};
  } catch (e) {
    state.seriesList = [];
    state.blankRates = {};
  }
  const ul = $("#dataset-list");
  ul.innerHTML = "";
  for (const d of data.datasets) {
    const li = document.createElement("li");
    li.dataset.id = d.id;
    li.innerHTML = `<span>${d.name}${d.sample_id ? " · " + d.sample_id : ""}</span>` +
      (d.is_blank ? '<span class="tag">空白</span>' : `<span>${d.n_points}点</span>`);
    if (d.id === state.currentId) li.classList.add("active");
    li.addEventListener("click", () => selectDataset(d.id));
    ul.appendChild(li);
  }
  // 空白室下拉
  const sel = $("#sel-blank");
  const prev = sel.value;
  sel.innerHTML = '<option value="">（不使用）</option>';
  for (const d of data.datasets.filter((x) => x.is_blank)) {
    const op = document.createElement("option");
    op.value = d.id; op.textContent = d.name;
    sel.appendChild(op);
  }
  sel.value = prev;
  // 批量分析视图：数据集与空白室下拉
  const bds = $("#batch-dataset");
  const prevB = bds.value;
  bds.innerHTML = "";
  for (const d of data.datasets) {
    const op = document.createElement("option");
    op.value = d.id;
    op.textContent = d.name + (d.is_blank ? "（空白）" : "");
    bds.appendChild(op);
  }
  bds.value = prevB;
  const bb = $("#batch-blank");
  const prevBB = bb.value;
  bb.innerHTML = '<option value="">（不使用）</option>';
  for (const d of data.datasets.filter((x) => x.is_blank)) {
    const op = document.createElement("option");
    op.value = d.id; op.textContent = d.name;
    bb.appendChild(op);
  }
  bb.value = prevBB;
  // 批量分析视图：背景序列下拉
  const bs = $("#batch-series");
  const prevS = bs.value;
  bs.innerHTML = '<option value="">（不使用）</option>';
  for (const s of state.seriesList) {
    const op = document.createElement("option");
    op.value = s.id;
    op.textContent = `${s.name}（${s.method === "linear" ? "线性插值" : "最近锚点"} rev${s.rev}）`;
    bs.appendChild(op);
  }
  bs.value = prevS;
  toggleBlankSelects();
}

async function selectDataset(id) {
  state.currentId = id;
  state.window = null;
  state.calibPoints = [];
  state.lastResult = null;
  state.corrected = null;
  document.querySelectorAll("#dataset-list li").forEach((li) =>
    li.classList.toggle("active", +li.dataset.id === id));
  const data = await api(`/api/dataset/${id}`);
  state.points = data.points;
  renderVersions(data.versions);
  // 若有历史版本，恢复最近一次参数
  if (data.versions.length) {
    const last = data.versions[data.versions.length - 1];
    state.window = last.params.window;
    state.calibPoints = last.params.calib_points || [];
    state.lastResult = last.result;
    if (last.params.blank_id) $("#sel-blank").value = last.params.blank_id;
    // 本地重算修正曲线，使拟合线叠加在修正后的曲线上
    state.corrected = driftCorrect(state.points, state.calibPoints);
  }
  updateExportLinks();
  drawPlot();
  drawResiduals(null);
  renderStats();
  renderWarnings(state.lastResult ? state.lastResult.warnings : []);
}

function updateExportLinks() {
  const id = state.currentId;
  $("#exp-csv").href = `/api/export/csv/${id}`;
  $("#exp-json").href = `/api/export/json/${id}`;
  $("#exp-report").href = `/api/report/${id}`;
}

// ------------------------------------------------------------ 绘图

const PLOT = { w: 900, h: 380, ml: 60, mr: 15, mt: 15, mb: 38 };

function scales(points, cfg) {
  const ts = points.map((p) => p.t);
  const ys = points.map((p) => (p.o2_corr !== undefined ? p.o2_corr : p.o2));
  let t0 = Math.min(...ts), t1 = Math.max(...ts);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.08 || 0.1;
  y0 -= pad; y1 += pad;
  const pw = cfg.w - cfg.ml - cfg.mr, ph = cfg.h - cfg.mt - cfg.mb;
  return {
    t0, t1, y0, y1,
    X: (t) => cfg.ml + ((t - t0) / (t1 - t0 || 1)) * pw,
    Y: (y) => cfg.mt + ph - ((y - y0) / (y1 - y0 || 1)) * ph,
    invX: (x) => t0 + ((x - cfg.ml) / pw) * (t1 - t0),
  };
}

function el(name, attrs, parent) {
  const e = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function drawPlot() {
  const svg = $("#plot");
  svg.innerHTML = "";
  const pts = state.corrected || state.points;
  if (!pts.length) return;
  const sc = scales(pts, PLOT);
  state._sc = sc;
  const ph = PLOT.h - PLOT.mt - PLOT.mb;

  // 窗口高亮
  if (state.window) {
    const [w0, w1] = state.window;
    el("rect", { x: sc.X(Math.min(w0, w1)), y: PLOT.mt,
                 width: Math.abs(sc.X(w1) - sc.X(w0)), height: ph,
                 fill: "#cfe8cf", opacity: 0.55 }, svg);
  }
  // 事件竖线
  for (const p of pts) {
    if (!p.event) continue;
    const x = sc.X(p.t);
    el("line", { x1: x, y1: PLOT.mt, x2: x, y2: PLOT.mt + ph,
                 stroke: "#d9534f", "stroke-dasharray": "4 3" }, svg);
    const t = el("text", { x: x + 3, y: PLOT.mt + 12, "font-size": 10,
                           fill: "#d9534f" }, svg);
    t.textContent = p.event;
  }
  // 原始曲线（有修正曲线时淡显）
  if (state.corrected) {
    el("polyline", {
      points: state.points.map((p) => `${sc.X(p.t)},${sc.Y(p.o2)}`).join(" "),
      fill: "none", stroke: "#bbb", "stroke-width": 1,
      "stroke-dasharray": "3 2" }, svg);
  }
  // 主曲线
  el("polyline", {
    points: pts.map((p) =>
      `${sc.X(p.t)},${sc.Y(p.o2_corr !== undefined ? p.o2_corr : p.o2)}`).join(" "),
    fill: "none", stroke: "#2266aa", "stroke-width": 1.6 }, svg);
  // 校准点
  state.calibPoints.forEach(([ct, cv], i) => {
    const c = el("circle", { cx: sc.X(ct), cy: sc.Y(cv), r: 5,
                             fill: "#e8a13a", stroke: "#8a5a00",
                             "stroke-width": 1.5, cursor: "pointer" }, svg);
    const title = el("title", {}, c);
    title.textContent = `校准点 ${i + 1}: t=${ct.toFixed(0)}s 期望=${cv.toFixed(3)}（点击删除）`;
    c.addEventListener("click", (ev) => {
      ev.stopPropagation();
      state.calibPoints.splice(i, 1);
      drawPlot();
    });
  });
  // 拟合线
  const r = state.lastResult;
  if (r && r.slope != null && state.window) {
    const [w0, w1] = state.window;
    el("line", { x1: sc.X(w0), y1: sc.Y(r.slope * w0 + r.intercept),
                 x2: sc.X(w1), y2: sc.Y(r.slope * w1 + r.intercept),
                 stroke: "#cc3300", "stroke-width": 2.2 }, svg);
  }
  drawAxes(svg, sc, PLOT);
}

function drawAxes(svg, sc, cfg) {
  const pw = cfg.w - cfg.ml - cfg.mr, ph = cfg.h - cfg.mt - cfg.mb;
  el("line", { x1: cfg.ml, y1: cfg.mt + ph, x2: cfg.ml + pw, y2: cfg.mt + ph, stroke: "#333" }, svg);
  el("line", { x1: cfg.ml, y1: cfg.mt, x2: cfg.ml, y2: cfg.mt + ph, stroke: "#333" }, svg);
  for (let i = 0; i <= 5; i++) {
    const tv = sc.t0 + (sc.t1 - sc.t0) * i / 5;
    const tx = el("text", { x: sc.X(tv), y: cfg.mt + ph + 18, "font-size": 10,
                            "text-anchor": "middle" }, svg);
    tx.textContent = tv.toFixed(0);
    const yv = sc.y0 + (sc.y1 - sc.y0) * i / 5;
    const ty = el("text", { x: cfg.ml - 6, y: sc.Y(yv) + 3, "font-size": 10,
                            "text-anchor": "end" }, svg);
    ty.textContent = yv.toFixed(2);
  }
  const xl = el("text", { x: cfg.ml + pw / 2, y: cfg.h - 4, "font-size": 11,
                          "text-anchor": "middle" }, svg);
  xl.textContent = "时间 (s)";
}

// 残差图
function drawResiduals(result) {
  const svg = $("#resid-plot");
  svg.innerHTML = "";
  if (!result || !result.residuals || !result.residuals.length) return;
  const cfg = { w: 900, h: 130, ml: 60, mr: 15, mt: 10, mb: 25 };
  const wp = result.window_points;
  const ts = wp.map((p) => p.t), rs = result.residuals;
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const rmax = Math.max(...rs.map(Math.abs), 1e-9);
  const pw = cfg.w - cfg.ml - cfg.mr, ph = cfg.h - cfg.mt - cfg.mb;
  const X = (t) => cfg.ml + ((t - t0) / (t1 - t0 || 1)) * pw;
  const Y = (r) => cfg.mt + ph / 2 - (r / rmax) * (ph / 2 - 4);
  el("line", { x1: cfg.ml, y1: Y(0), x2: cfg.ml + pw, y2: Y(0),
               stroke: "#999", "stroke-dasharray": "3 2" }, svg);
  ts.forEach((t, i) => {
    el("line", { x1: X(t), y1: Y(0), x2: X(t), y2: Y(rs[i]),
                 stroke: "#cc3300", "stroke-width": 1 }, svg);
    el("circle", { cx: X(t), cy: Y(rs[i]), r: 2.5, fill: "#cc3300" }, svg);
  });
  const lbl = el("text", { x: cfg.ml, y: cfg.h - 5, "font-size": 10, fill: "#666" }, svg);
  lbl.textContent = `窗口内残差 (mg/L)，最大 |r| = ${rmax.toFixed(5)}`;
}

// ------------------------------------------------------------ 交互：拖窗口 / 校准点

function svgX(evt) {
  const rect = $("#plot").getBoundingClientRect();
  return evt.clientX - rect.left;
}

let dragStart = null;
const plot = $("#plot");

plot.addEventListener("mousedown", (evt) => {
  if (!state.points.length || state.calibMode) return;
  dragStart = svgX(evt);
});
plot.addEventListener("mousemove", (evt) => {
  if (dragStart === null || !state._sc) return;
  const t0 = state._sc.invX(dragStart), t1 = state._sc.invX(svgX(evt));
  state.window = [Math.max(t0, state._sc.t0), Math.min(t1, state._sc.t1)];
  drawPlot();
  // 拖动中画临时框
  const sc = state._sc, svg = $("#plot");
  const ph = PLOT.h - PLOT.mt - PLOT.mb;
  el("rect", { x: Math.min(dragStart, svgX(evt)), y: PLOT.mt,
               width: Math.abs(svgX(evt) - dragStart), height: ph,
               fill: "none", stroke: "#2a7a2a", "stroke-dasharray": "5 3" }, svg);
});
window.addEventListener("mouseup", () => { dragStart = null; });

plot.addEventListener("click", (evt) => {
  if (!state.calibMode || !state._sc) return;
  const t = state._sc.invX(svgX(evt));
  // 找最近实测点，用其温压估算饱和浓度作为期望值
  const pts = state.points;
  const nearest = pts.reduce((a, b) => Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a);
  const temp = nearest.temp != null ? nearest.temp : 25;
  const press = nearest.press != null ? nearest.press : 101.325;
  const cs = (14.652 - 0.41022 * temp + 0.007991 * temp ** 2 - 0.000077774 * temp ** 3)
             * (press / 101.325);
  const input = prompt(`校准点 t=${t.toFixed(0)}s 的期望氧浓度 (mg/L)`,
                       cs.toFixed(3));
  if (input === null) return;
  const expected = parseFloat(input);
  if (!isFinite(expected)) { alert("请输入数值"); return; }
  state.calibPoints.push([nearest.t, expected]);
  state.calibPoints.sort((a, b) => a[0] - b[0]);
  drawPlot();
});

$("#btn-calib-mode").addEventListener("click", () => {
  state.calibMode = !state.calibMode;
  $("#btn-calib-mode").classList.toggle("armed", state.calibMode);
  $("#btn-calib-mode").textContent = state.calibMode ? "点击曲线放置校准点…" : "添加校准点";
});
$("#btn-clear-calib").addEventListener("click", () => {
  state.calibPoints = []; drawPlot();
});
$("#btn-clear-window").addEventListener("click", () => {
  state.window = null; state.lastResult = null;
  drawPlot(); drawResiduals(null); renderStats(); renderWarnings([]);
});

// ------------------------------------------------------------ 分析 / 撤销

$("#btn-analyze").addEventListener("click", async () => {
  if (!state.currentId) return alert("请先选择数据集");
  if (!state.window) return alert("请先在图上拖动选择稳态窗");
  const payload = {
    dataset_id: state.currentId,
    window: state.window,
    calib_points: state.calibPoints,
    blank_id: $("#sel-blank").value || null,
    r2_threshold: parseFloat($("#in-r2").value) || 0.9,
    min_points: parseInt($("#in-minpts").value) || 5,
    note: $("#in-note").value,
  };
  try {
    const data = await api("/api/analyze", {
      method: "POST", body: JSON.stringify(payload) });
    state.lastResult = data.result;
    state.lastResult.window_points = data.result.window_points;
    state.corrected = data.corrected;
    drawPlot();
    drawResiduals(data.result);
    renderStats();
    renderWarnings(data.result.warnings);
    const v = await api(`/api/versions/${state.currentId}`);
    renderVersions(v.versions);
  } catch (e) { alert("分析失败: " + e.message); }
});

$("#btn-undo").addEventListener("click", async () => {
  if (!state.currentId) return;
  const data = await api("/api/undo", {
    method: "POST", body: JSON.stringify({ dataset_id: state.currentId }) });
  if (!data.ok) { alert(data.message); return; }
  const cur = data.current;
  state.window = cur.params.window;
  state.calibPoints = cur.params.calib_points || [];
  state.lastResult = cur.result;
  // 本地重算修正曲线用于显示（不产生新版本）
  state.corrected = driftCorrect(state.points, state.calibPoints);
  renderVersions(data.versions);
  drawPlot(); drawResiduals(state.lastResult); renderStats();
  renderWarnings(state.lastResult.warnings || []);
});

// 与服务端 analysis.drift_correct 等价的本地实现（仅用于视图刷新）
function driftCorrect(points, calib) {
  if (!calib || !calib.length)
    return points.map((p) => ({ ...p, o2_corr: p.o2, offset: 0 }));
  const cal = [...calib].sort((a, b) => a[0] - b[0]);
  const offsets = cal.map(([ct, exp]) => {
    const nearest = points.reduce((a, b) =>
      Math.abs(b.t - ct) < Math.abs(a.t - ct) ? b : a);
    return nearest.o2 - exp;
  });
  const offsetAt = (t) => {
    if (t <= cal[0][0]) return offsets[0];
    if (t >= cal[cal.length - 1][0]) return offsets[offsets.length - 1];
    for (let i = 0; i < cal.length - 1; i++) {
      const [t0] = cal[i], [t1] = cal[i + 1];
      if (t >= t0 && t <= t1) {
        const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
        return offsets[i] + f * (offsets[i + 1] - offsets[i]);
      }
    }
    return offsets[offsets.length - 1];
  };
  return points.map((p) => {
    const off = offsetAt(p.t);
    return { ...p, offset: off, o2_corr: p.o2 - off };
  });
}

function renderVersions(versions) {
  const ul = $("#version-list");
  ul.innerHTML = "";
  versions.forEach((v, i) => {
    const li = document.createElement("li");
    const time = new Date(v.created_at * 1000).toLocaleTimeString();
    li.innerHTML = `<b>v${i + 1}</b> ${time} ` +
      `<span class="vnote">${v.note || ""} R²=${fmt(v.result.r2, 4)} ` +
      `MO₂=${fmt(v.result.mo2_net, 4)}</span>`;
    ul.appendChild(li);
  });
}

function renderWarnings(warnings) {
  const box = $("#warnings");
  box.innerHTML = "";
  for (const w of warnings || []) {
    const div = document.createElement("div");
    div.className = "warn";
    div.textContent = `⚠ [${w.code}] ${w.msg}`;
    box.appendChild(div);
  }
}

function renderStats() {
  const r = state.lastResult, box = $("#stats");
  if (!r || r.slope == null) { box.className = "stats"; box.innerHTML = ""; return; }
  box.className = "stats show";
  box.innerHTML = `<table>
<tr><th>窗口 (s)</th><td>${state.window ? state.window.map((x) => x.toFixed(1)).join(" ~ ") : "—"}</td>
    <th>有效点数</th><td>${r.n}</td></tr>
<tr><th>斜率 (mg/L/s)</th><td>${fmt(r.slope, 8)}</td>
    <th>截距</th><td>${fmt(r.intercept, 4)}</td></tr>
<tr><th>R²</th><td>${fmt(r.r2, 5)}</td>
    <th>标准状况系数</th><td>${fmt(r.stp_factor, 5)}</td></tr>
<tr><th>平均温度 (°C)</th><td>${fmt(r.mean_temp, 2)}</td>
    <th>平均气压 (kPa)</th><td>${fmt(r.mean_press, 2)}</td></tr>
<tr><th>舱体体积 (L)</th><td>${fmt(r.volume, 3)}</td>
    <th>饱和氧 (mg/L)</th><td>${fmt(r.o2_sat, 3)}</td></tr>
<tr><th>MO₂ 修正前 (mg/h)</th><td>${fmt(r.mo2_raw, 4)}</td>
    <th>MO₂ 空白修正后 (mg/h)</th><td><b>${fmt(r.mo2_net, 4)}</b></td></tr>
${r.blank ? `<tr><th>空白室速率</th><td colspan="3">${fmt(r.blank.rate, 4)} mg/h
  （按体积比折算 ${fmt(r.blank.scaled, 4)}）</td></tr>` : ""}
</table>`;
}

// ------------------------------------------------------------ 分组比较

async function loadGroups() {
  const data = await api("/api/groups");
  const box = $("#groups-table");
  if (!data.groups.length) { box.innerHTML = "<p>暂无数据</p>"; return; }
  let html = `<table><tr><th>样本</th><th>测量</th><th>MO₂ (mg/h)</th>
<th>R²</th><th>版本数</th><th>操作</th></tr>`;
  for (const g of data.groups) {
    const rows = [];
    g.measurements.forEach((m, i) => {
      rows.push(`<tr>${i === 0 ? `<td rowspan="${g.measurements.length + g.excluded.length}">
        <b>${g.sample_id}</b><br>均值: ${fmt(g.mean_mo2, 4)}<br>有效: ${g.n_valid} 次</td>` : ""}
<td>${m.name}</td><td>${fmt(m.mo2_net, 4)}</td><td>${fmt(m.r2, 4)}</td><td>${m.n_versions}</td>
<td><button onclick="excludeDs(${m.dataset_id})">剔除…</button></td></tr>`);
    });
    for (const m of g.excluded) {
      rows.push(`<tr class="excluded"><td>${m.name}（已剔除）</td>
<td>${fmt(m.mo2_net, 4)}</td><td>${fmt(m.r2, 4)}</td><td>${m.n_versions}</td>
<td><span class="reason">${m.reasons.join("；")}</span>
<button onclick="restoreDs(${m.dataset_id})">恢复</button></td></tr>`);
    }
    html += rows.join("");
  }
  html += "</table>";
  box.innerHTML = html;
}

window.excludeDs = async (id) => {
  const reason = prompt("剔除理由（必填）：");
  if (!reason) return;
  await api("/api/exclude", { method: "POST",
    body: JSON.stringify({ dataset_id: id, reason }) });
  loadGroups();
};

window.restoreDs = async (id) => {
  await api("/api/exclude", { method: "POST",
    body: JSON.stringify({ undo_dataset: true, dataset_id: id }) });
  loadGroups();
};

$("#btn-refresh-groups").addEventListener("click", loadGroups);

// ------------------------------------------------------------ 批量分析

const BPLOT = { w: 1040, h: 360, ml: 60, mr: 15, mt: 15, mb: 38 };
const BRESID = { w: 1040, h: 130, ml: 60, mr: 15, mt: 10, mb: 25 };

const batch = {
  datasetId: null,
  points: [],
  corrected: null,   // 服务端返回的修正曲线
  calib: [],         // [[t, expected], ...]
  cycles: [],        // 服务端回显的周期（含结果/告警/裁决）
  summary: null,
  seriesInfo: null,  // 时变空白序列信息（锚点/方法/版本）
  selected: null,    // 选中周期 id（查看残差）
  calibMode: false,
  addMode: false,
  _sc: null,
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (ch) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

const WARN_NAMES = {
  CYCLE_OVERLAP: "周期重叠", CYCLE_TOO_SHORT: "测量段过短",
  EVENT_IN_WINDOW: "跨事件", LOW_R2: "R²未达标", RATE_OUTLIER: "速率离群",
  TOO_FEW_POINTS: "点数不足", TIME_REVERSED: "时间倒序", SIGN_FLIP: "符号反转",
  BLANK_NO_ANCHOR: "无可用锚点", BLANK_NO_BRACKET: "缺少前后锚点",
  BLANK_GAP_TOO_LARGE: "锚点间隔超限", BLANK_EXTRAPOLATE: "区间外推",
};

function readSegParams() {
  return {
    method: $("#seg-method").value,
    event_keyword: $("#seg-keyword").value.trim() || "flush",
    rise_threshold: parseFloat($("#seg-rise").value) || 0.01,
    rise_min_duration: parseFloat($("#seg-risedur").value) || 0,
    flush_offset: parseFloat($("#seg-offset").value) || 0,
    end_margin: parseFloat($("#seg-margin").value) || 0,
    min_duration: parseFloat($("#seg-mindur").value) || 0,
  };
}

function restoreBatchParams(p) {
  const seg = p.seg || {};
  $("#seg-method").value = seg.method || "event";
  $("#seg-keyword").value = seg.event_keyword ?? "flush";
  $("#seg-rise").value = seg.rise_threshold ?? 0.01;
  $("#seg-risedur").value = seg.rise_min_duration ?? 0;
  $("#seg-offset").value = seg.flush_offset ?? 60;
  $("#seg-margin").value = seg.end_margin ?? 5;
  $("#seg-mindur").value = p.min_duration ?? seg.min_duration ?? 60;
  batch.calib = p.calib_points || [];
  $("#batch-blank").value = p.blank_id || "";
  $("#batch-series").value = p.blank_series_id || "";
  $("#batch-r2").value = p.r2_threshold ?? 0.9;
  $("#batch-minpts").value = p.min_points ?? 5;
  toggleSegRows();
  toggleBlankSelects();
}

function toggleBlankSelects() {
  // 选择背景序列时静态空白室不生效（服务端以序列优先）
  $("#batch-blank").disabled = !!$("#batch-series").value;
}
$("#batch-series").addEventListener("change", toggleBlankSelects);

function toggleSegRows() {
  const m = $("#seg-method").value;
  $("#row-keyword").classList.toggle("seg-hidden", m !== "event");
  $("#row-rise").classList.toggle("seg-hidden", m !== "rise");
  $("#row-risedur").classList.toggle("seg-hidden", m !== "rise");
}
$("#seg-method").addEventListener("change", toggleSegRows);

async function initBatchView() {
  await loadDatasets();
  toggleSegRows();
  if (!batch.datasetId) {
    const first = state.currentId || (state.datasets[0] && state.datasets[0].id);
    if (first) {
      $("#batch-dataset").value = first;
      await batchSelect(first);
    }
  }
}

$("#batch-dataset").addEventListener("change", () => {
  if ($("#batch-dataset").value) batchSelect(+$("#batch-dataset").value);
});

async function batchSelect(id) {
  batch.datasetId = id;
  batch.cycles = []; batch.corrected = null; batch.calib = [];
  batch.summary = null; batch.selected = null; batch.seriesInfo = null;
  $("#seg-msg").textContent = ""; $("#batch-save-msg").textContent = "";
  const data = await api(`/api/dataset/${id}`);
  batch.points = data.points;
  renderBatchVersions(data.batch_versions || []);
  updateBatchExportLinks();
  if (data.batch_versions && data.batch_versions.length) {
    // 恢复最近批次版本并重算（曲线/残差由重算刷新）
    const full = await api(`/api/batch/version/${data.batch_versions[data.batch_versions.length - 1].id}`);
    restoreBatchParams(full.version.params);
    batch.cycles = full.version.cycles;
    await runBatch(false, false);
  } else {
    drawBatchPlot(); drawBatchResid(); drawBlankStrip();
    renderBatchTable(); renderBatchSummary();
  }
}

function updateBatchExportLinks() {
  const id = batch.datasetId;
  $("#batch-exp-csv").href = `/api/batch/export/csv/${id}`;
  $("#batch-exp-json").href = `/api/batch/export/json/${id}`;
  $("#batch-exp-report").href = `/api/batch/report/${id}`;
}

// ---------------- 与服务端交互 ----------------

async function runBatch(resegment, save) {
  if (!batch.datasetId) { alert("请先选择数据集"); return null; }
  const payload = {
    dataset_id: batch.datasetId,
    resegment: !!resegment,
    seg: readSegParams(),
    cycles: batch.cycles.map((c) => ({
      start: c.start, end: c.end, locked: !!c.locked,
      decision: c.decision || null, reason: c.reason || "",
    })),
    calib_points: batch.calib,
    blank_id: $("#batch-blank").value || null,
    blank_series_id: $("#batch-series").value || null,
    r2_threshold: parseFloat($("#batch-r2").value) || 0.9,
    min_points: parseInt($("#batch-minpts").value) || 5,
    min_duration: parseFloat($("#seg-mindur").value) || 60,
    save: !!save,
    note: $("#batch-note").value,
  };
  try {
    const data = await api("/api/batch/analyze", {
      method: "POST", body: JSON.stringify(payload) });
    batch.cycles = data.cycles;
    batch.corrected = data.corrected;
    batch.summary = data.summary;
    batch.seriesInfo = data.blank_series || null;
    renderBatchVersions(data.versions);
    drawBatchPlot(); drawBatchResid(); drawBlankStrip();
    renderBatchTable(); renderBatchSummary();
    if (resegment) {
      const msg = $("#seg-msg");
      msg.className = "msg";
      msg.textContent = data.n_candidates
        ? `分段完成：候选 ${data.n_candidates} 个，合并后共 ${data.cycles.length} 个周期（锁定周期未受影响）`
        : "按当前规则未找到候选周期，请调整分段方式或阈值";
    }
    if (save) {
      const msg = $("#batch-save-msg");
      msg.className = "msg";
      msg.textContent = `已保存批次版本 v${data.version_id}（可撤销）` +
        propagationText(data.blank_propagation);
      if ((data.blank_propagation || []).length) loadDatasets();
      $("#batch-note").value = "";
    }
    return data;
  } catch (e) {
    alert("批量分析失败: " + e.message);
    return null;
  }
}

// 空白批次更新传播到背景序列的提示
function propagationText(prop) {
  if (!prop || !prop.length) return "";
  const n = prop.reduce((s, p) => s + (p.affected ? p.affected.length : 0), 0);
  const revs = prop.map((p) => `序列#${p.series_id} rev${p.series_rev}`).join("、");
  return `；空白有效均值更新已传播（${revs}），${n} 个关联样本已自动重算`;
}

$("#btn-segment").addEventListener("click", () => runBatch(true, false));
$("#btn-batch-recalc").addEventListener("click", () => runBatch(false, false));
$("#btn-batch-save").addEventListener("click", () => runBatch(false, true));

$("#btn-batch-undo").addEventListener("click", async () => {
  if (!batch.datasetId) return;
  const data = await api("/api/batch/undo", {
    method: "POST", body: JSON.stringify({ dataset_id: batch.datasetId }) });
  if (!data.ok) { alert(data.message); return; }
  if (data.current) {
    restoreBatchParams(data.current.params);
    batch.cycles = data.current.cycles;
    await runBatch(false, false);
  } else {
    batch.cycles = []; batch.corrected = null; batch.summary = null;
    batch.selected = null; batch.seriesInfo = null;
    renderBatchVersions(data.versions);
    drawBatchPlot(); drawBatchResid(); drawBlankStrip();
    renderBatchTable(); renderBatchSummary();
  }
  const msg = $("#batch-save-msg");
  msg.className = "msg";
  msg.textContent = data.message + propagationText(data.blank_propagation);
  if ((data.blank_propagation || []).length) loadDatasets();
});

// ---------------- 总览图 ----------------

function bSvgX(evt) {
  const rect = $("#batch-plot").getBoundingClientRect();
  return evt.clientX - rect.left;
}

function drawBatchPlot() {
  const svg = $("#batch-plot");
  svg.innerHTML = "";
  const pts = batch.corrected || batch.points;
  if (!pts.length) return;
  const sc = scales(pts, BPLOT);
  batch._sc = sc;
  const ph = BPLOT.h - BPLOT.mt - BPLOT.mb;

  // 周期色带（绿=计入汇总，红=未计入）
  for (const c of batch.cycles) {
    const x0 = sc.X(c.start), x1 = sc.X(c.end);
    el("rect", { x: Math.min(x0, x1), y: BPLOT.mt,
                 width: Math.abs(x1 - x0), height: ph,
                 fill: c.included ? "#cfe8cf" : "#f2d7d7",
                 opacity: batch.selected === c.id ? 0.9 : 0.5,
                 stroke: c.locked ? "#1f3a5f" : "none",
                 "stroke-width": c.locked ? 1.5 : 0,
                 "data-band": c.id, cursor: "pointer" }, svg);
    const lbl = el("text", { x: (x0 + x1) / 2, y: BPLOT.mt + 13, "font-size": 11,
                             "text-anchor": "middle", fill: "#444",
                             "pointer-events": "none" }, svg);
    lbl.textContent = `#${c.id}${c.locked ? " 🔒" : ""}`;
  }
  // 事件竖线
  for (const p of pts) {
    if (!p.event) continue;
    const x = sc.X(p.t);
    el("line", { x1: x, y1: BPLOT.mt, x2: x, y2: BPLOT.mt + ph,
                 stroke: "#d9534f", "stroke-dasharray": "4 3" }, svg);
    const t = el("text", { x: x + 3, y: BPLOT.mt + 26, "font-size": 10,
                           fill: "#d9534f" }, svg);
    t.textContent = p.event;
  }
  // 原始曲线（有修正曲线时淡显）
  if (batch.corrected) {
    el("polyline", {
      points: batch.points.map((p) => `${sc.X(p.t)},${sc.Y(p.o2)}`).join(" "),
      fill: "none", stroke: "#bbb", "stroke-width": 1,
      "stroke-dasharray": "3 2" }, svg);
  }
  // 主曲线
  el("polyline", {
    points: pts.map((p) =>
      `${sc.X(p.t)},${sc.Y(p.o2_corr !== undefined ? p.o2_corr : p.o2)}`).join(" "),
    fill: "none", stroke: "#2266aa", "stroke-width": 1.6 }, svg);
  // 各周期拟合线
  for (const c of batch.cycles) {
    const r = c.result;
    if (!r || r.slope == null) continue;
    el("line", { x1: sc.X(c.start), y1: sc.Y(r.slope * c.start + r.intercept),
                 x2: sc.X(c.end), y2: sc.Y(r.slope * c.end + r.intercept),
                 stroke: "#cc3300", "stroke-width": 2,
                 opacity: c.included ? 1 : 0.35,
                 "pointer-events": "none" }, svg);
  }
  // 校准点
  batch.calib.forEach(([ct, cv], i) => {
    const c = el("circle", { cx: sc.X(ct), cy: sc.Y(cv), r: 5,
                             fill: "#e8a13a", stroke: "#8a5a00",
                             "stroke-width": 1.5, cursor: "pointer" }, svg);
    const title = el("title", {}, c);
    title.textContent = `校准点 ${i + 1}: t=${ct.toFixed(0)}s 期望=${cv.toFixed(3)}（点击删除）`;
    c.addEventListener("click", (ev) => {
      ev.stopPropagation();
      batch.calib.splice(i, 1);
      runBatch(false, false);
    });
  });
  // 周期边界手柄（锁定周期不可拖动）
  for (const c of batch.cycles) {
    for (const [side, t] of [["start", c.start], ["end", c.end]]) {
      const x = sc.X(t);
      el("line", { x1: x, y1: BPLOT.mt, x2: x, y2: BPLOT.mt + ph,
                   stroke: c.locked ? "#1f3a5f" : "#2a7a2a", "stroke-width": 1.5,
                   "stroke-dasharray": c.locked ? "3 3" : "none",
                   "pointer-events": "none" }, svg);
      const grip = el("rect", { x: x - 4, y: BPLOT.mt, width: 8, height: ph,
                                fill: "#000", opacity: 0,
                                cursor: c.locked ? "default" : "ew-resize",
                                "data-cid": c.id, "data-side": side }, svg);
      const tip = el("title", {}, grip);
      tip.textContent = c.locked ? "周期已锁定" : "拖动调整边界";
    }
  }
  drawAxes(svg, sc, BPLOT);
}

function drawBatchResid() {
  const svg = $("#batch-resid");
  svg.innerHTML = "";
  const c = batch.cycles.find((x) => x.id === batch.selected);
  if (!c || !c.result || !c.result.residuals || !c.result.residuals.length) {
    const t = el("text", { x: BRESID.ml, y: 32, "font-size": 11, fill: "#888" }, svg);
    t.textContent = "点击总览图中的周期色带，查看该周期残差。";
    return;
  }
  const ts = c.result.resid_t, rs = c.result.residuals;
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const rmax = Math.max(...rs.map(Math.abs), 1e-9);
  const pw = BRESID.w - BRESID.ml - BRESID.mr, ph = BRESID.h - BRESID.mt - BRESID.mb;
  const X = (t) => BRESID.ml + ((t - t0) / (t1 - t0 || 1)) * pw;
  const Y = (r) => BRESID.mt + ph / 2 - (r / rmax) * (ph / 2 - 4);
  el("line", { x1: BRESID.ml, y1: Y(0), x2: BRESID.ml + pw, y2: Y(0),
               stroke: "#999", "stroke-dasharray": "3 2" }, svg);
  ts.forEach((t, i) => {
    el("line", { x1: X(t), y1: Y(0), x2: X(t), y2: Y(rs[i]),
                 stroke: "#cc3300", "stroke-width": 1 }, svg);
    el("circle", { cx: X(t), cy: Y(rs[i]), r: 2.5, fill: "#cc3300" }, svg);
  });
  const lbl = el("text", { x: BRESID.ml, y: BRESID.h - 5, "font-size": 10,
                           fill: "#666" }, svg);
  lbl.textContent = `周期 #${c.id} 残差 (mg/L)，R²=${fmt(c.result.r2, 4)}，` +
    `最大 |r| = ${rmax.toFixed(5)}`;
}

// 时变空白：背景模型与样本测量窗在时间轴上对齐的预览条
function drawBlankStrip() {
  const svg = $("#blank-strip");
  svg.innerHTML = "";
  const info = batch.seriesInfo, sc = batch._sc;
  if (!info || !sc || !info.anchors) { svg.classList.add("hidden"); return; }
  svg.classList.remove("hidden");
  const cfg = { w: BPLOT.w, h: 90, ml: BPLOT.ml, mr: BPLOT.mr, mt: 16, mb: 20 };
  const pw = cfg.w - cfg.ml - cfg.mr, ph = cfg.h - cfg.mt - cfg.mb;
  const X = (t) => cfg.ml + ((t - sc.t0) / (sc.t1 - sc.t0 || 1)) * pw;
  const act = info.anchors.filter((a) => a.rate != null && !a.disabled)
    .sort((a, b) => a.collected_at - b.collected_at);
  const used = batch.cycles
    .map((c) => ({ mid: (c.start + c.end) / 2,
                   rate: c.result && c.result.blank && c.result.blank.rate,
                   inc: c.included }))
    .filter((u) => u.rate != null);
  const rates = [...act.map((a) => a.rate), ...used.map((u) => u.rate)];
  if (!rates.length) {
    const t = el("text", { x: cfg.ml, y: 30, "font-size": 11, fill: "#b3271e" }, svg);
    t.textContent = "背景序列无可用锚点（均停用或缺少已确认批量均值）。";
    return;
  }
  let y0 = Math.min(...rates), y1 = Math.max(...rates);
  const pad = (y1 - y0) * 0.2 || 0.01;
  y0 -= pad; y1 += pad;
  const Y = (r) => cfg.mt + ph - ((r - y0) / (y1 - y0)) * ph;
  // 模型曲线（线性插值 / 最近锚点阶梯）
  if (act.length >= 2) {
    let pts;
    if (info.method === "nearest") {
      pts = [[sc.t0, act[0].rate]];
      for (let i = 1; i < act.length; i++) {
        const mid = (act[i - 1].collected_at + act[i].collected_at) / 2;
        pts.push([mid, act[i - 1].rate], [mid, act[i].rate]);
      }
      pts.push([sc.t1, act[act.length - 1].rate]);
    } else {
      pts = act.map((a) => [a.collected_at, a.rate]);
    }
    el("polyline", { points: pts.map(([t, r]) => `${X(t)},${Y(r)}`).join(" "),
      fill: "none", stroke: "#8a4b08", "stroke-width": 1.5,
      "stroke-dasharray": "5 3" }, svg);
  }
  // 锚点（菱形；人工锁定深色；停用灰叉于基线）
  for (const a of info.anchors) {
    const x = X(a.collected_at);
    if (a.disabled || a.rate == null) {
      const y = cfg.mt + ph;
      el("line", { x1: x - 4, y1: y - 4, x2: x + 4, y2: y + 4, stroke: "#999" }, svg);
      el("line", { x1: x - 4, y1: y + 4, x2: x + 4, y2: y - 4, stroke: "#999" }, svg);
      const t = el("text", { x, y: y - 7, "font-size": 9, fill: "#999",
                             "text-anchor": "middle" }, svg);
      t.textContent = `A${a.id}${a.disabled ? "停用" : "无速率"}`;
    } else {
      const y = Y(a.rate);
      el("path", { d: `M ${x} ${y - 5} L ${x + 5} ${y} L ${x} ${y + 5} L ${x - 5} ${y} Z`,
        fill: a.locked_rate != null ? "#b3540e" : "#e8a13a",
        stroke: "#8a5a00", "stroke-width": 1 }, svg);
      const t = el("text", { x, y: y - 8, "font-size": 9, fill: "#8a4b08",
                             "text-anchor": "middle" }, svg);
      t.textContent = `A${a.id} ${a.rate.toFixed(4)}`;
    }
  }
  // 各周期实际采用速率（绿=计入汇总，红=未计入）
  for (const u of used) {
    el("circle", { cx: X(u.mid), cy: Y(u.rate), r: 3.2,
                   fill: u.inc ? "#2a7a2a" : "#b3271e" }, svg);
  }
  // 坐标轴
  el("line", { x1: cfg.ml, y1: cfg.mt + ph, x2: cfg.ml + pw, y2: cfg.mt + ph, stroke: "#333" }, svg);
  el("line", { x1: cfg.ml, y1: cfg.mt, x2: cfg.ml, y2: cfg.mt + ph, stroke: "#333" }, svg);
  for (let i = 0; i <= 2; i++) {
    const rv = y0 + (y1 - y0) * i / 2;
    const t = el("text", { x: cfg.ml - 5, y: Y(rv) + 3, "font-size": 9,
                           "text-anchor": "end", fill: "#666" }, svg);
    t.textContent = rv.toFixed(4);
  }
  const yl = el("text", { x: 12, y: cfg.mt + ph / 2, "font-size": 10, fill: "#666",
    "text-anchor": "middle",
    transform: `rotate(-90 12 ${cfg.mt + ph / 2})` }, svg);
  yl.textContent = "空白速率 (mg/h)";
}

// ---------------- 总览图交互：拖边界 / 添加周期 / 校准点 ----------------

let bDrag = null;   // {kind:"handle",cid,side} | {kind:"add",t0,t1}
const bplot = $("#batch-plot");

bplot.addEventListener("mousedown", (evt) => {
  if (!batch.points.length || batch.calibMode) return;
  const grip = evt.target.closest("[data-cid][data-side]");
  if (grip) {
    const c = batch.cycles.find((x) => x.id === +grip.dataset.cid);
    if (c && !c.locked) {
      bDrag = { kind: "handle", cid: c.id, side: grip.dataset.side };
      evt.preventDefault();
    }
    return;
  }
  if (batch.addMode && batch._sc) {
    bDrag = { kind: "add", t0: batch._sc.invX(bSvgX(evt)), t1: null };
    evt.preventDefault();
  }
});

bplot.addEventListener("mousemove", (evt) => {
  if (!bDrag || !batch._sc) return;
  const sc = batch._sc;
  const t = Math.max(sc.t0, Math.min(sc.t1, sc.invX(bSvgX(evt))));
  if (bDrag.kind === "handle") {
    const c = batch.cycles.find((x) => x.id === bDrag.cid);
    if (!c) return;
    if (bDrag.side === "start") c.start = Math.min(t, c.end - 1);
    else c.end = Math.max(t, c.start + 1);
    drawBatchPlot();
  } else {
    bDrag.t1 = t;
    drawBatchPlot();
    // 拖动中画临时框
    const x0 = sc.X(Math.min(bDrag.t0, t)), x1 = sc.X(Math.max(bDrag.t0, t));
    el("rect", { x: x0, y: BPLOT.mt, width: x1 - x0,
                 height: BPLOT.h - BPLOT.mt - BPLOT.mb,
                 fill: "none", stroke: "#2a7a2a", "stroke-dasharray": "5 3" },
       $("#batch-plot"));
  }
});

window.addEventListener("mouseup", () => {
  if (!bDrag) return;
  const d = bDrag; bDrag = null;
  if (d.kind === "handle") {
    runBatch(false, false);
  } else if (d.kind === "add") {
    if (d.t1 != null && Math.abs(d.t1 - d.t0) > 2) {
      batch.cycles.push({ start: Math.min(d.t0, d.t1), end: Math.max(d.t0, d.t1),
                          locked: false, decision: null, reason: "" });
      batch.addMode = false;
      updateAddBtn();
      runBatch(false, false);
    } else {
      drawBatchPlot();
    }
  }
});

bplot.addEventListener("click", (evt) => {
  if (batch.calibMode) { batchAddCalib(evt); return; }
  if (bDrag) return;
  const band = evt.target.closest("[data-band]");
  if (band) {
    batch.selected = +band.dataset.band;
    drawBatchPlot(); drawBatchResid(); renderBatchTable();
  }
});

function batchAddCalib(evt) {
  if (!batch._sc) return;
  const t = batch._sc.invX(bSvgX(evt));
  const pts = batch.points;
  const nearest = pts.reduce((a, b) => Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a);
  const temp = nearest.temp != null ? nearest.temp : 25;
  const press = nearest.press != null ? nearest.press : 101.325;
  const cs = (14.652 - 0.41022 * temp + 0.007991 * temp ** 2 - 0.000077774 * temp ** 3)
             * (press / 101.325);
  const input = prompt(`校准点 t=${t.toFixed(0)}s 的期望氧浓度 (mg/L)`, cs.toFixed(3));
  if (input === null) return;
  const expected = parseFloat(input);
  if (!isFinite(expected)) { alert("请输入数值"); return; }
  batch.calib.push([nearest.t, expected]);
  batch.calib.sort((a, b) => a[0] - b[0]);
  runBatch(false, false);
}

function updateAddBtn() {
  $("#btn-addcycle").classList.toggle("armed", batch.addMode);
  $("#btn-addcycle").textContent = batch.addMode ? "在图上拖动框选新周期…" : "添加周期";
}
$("#btn-addcycle").addEventListener("click", () => {
  batch.addMode = !batch.addMode;
  updateAddBtn();
});

$("#btn-batch-calib").addEventListener("click", () => {
  batch.calibMode = !batch.calibMode;
  $("#btn-batch-calib").classList.toggle("armed", batch.calibMode);
  $("#btn-batch-calib").textContent =
    batch.calibMode ? "点击曲线放置校准点…" : "添加校准点";
});
$("#btn-batch-clear-calib").addEventListener("click", () => {
  batch.calib = [];
  runBatch(false, false);
});

// ---------------- 周期表格 / 汇总 / 版本 ----------------

function cycleById(cid) {
  return batch.cycles.find((c) => c.id === cid);
}

function blankSrcText(b) {
  if (!b) return "—";
  const an = b.anchors || [];
  if (b.mode === "linear" && an.length === 2)
    return `A${an[0].id}↔A${an[1].id} w=${an[1].weight.toFixed(2)}`;
  if (b.mode === "nearest" && an.length) return `最近A${an[0].id}`;
  if (b.mode === "single" && an.length) return `唯一A${an[0].id}`;
  if (b.mode === "none") return "无锚点";
  if (b.rate != null) return "静态空白";
  return "—";
}

function blankTitle(b) {
  if (!b) return "";
  const an = (b.anchors || []).map((a) =>
    `A${a.id}(r${a.rev ?? "?"})@${(+a.t).toFixed(0)}s=${a.rate.toFixed(4)}` +
    `${a.locked ? " 🔒人工" : ""} w=${a.weight.toFixed(2)}`).join(" ↔ ");
  const gap = b.gap_s != null ? `；间隔/距离 ${b.gap_s.toFixed(0)}s` : "";
  if (an) return an + gap;
  if (b.mode === "none") return "无可用锚点，未做空白修正";
  if (b.rate != null) return `静态空白室速率 ${b.rate.toFixed(4)} mg/h`;
  return "";
}

function renderBatchTable() {
  const box = $("#batch-table");
  if (!batch.cycles.length) {
    box.innerHTML = '<p class="hint">尚未分段：设置分段规则后点“自动分段”，' +
      "或用“添加周期”在图上手动框选。</p>";
    return;
  }
  const r2th = parseFloat($("#batch-r2").value) || 0.9;
  let html = `<table><thead><tr>
<th>计入</th><th>#</th><th>起点 (s)</th><th>终点 (s)</th><th>时长 (s)</th>
<th>点数</th><th>斜率 (mg/L/s)</th><th>R²</th><th>MO₂前 (mg/h)</th>
<th>MO₂净 (mg/h)</th><th>空白速率</th><th>来源区间</th><th>离群 z</th>
<th>告警</th><th>锁定</th><th>裁决</th><th>理由</th><th></th></tr></thead><tbody>`;
  for (const c of batch.cycles) {
    const r = c.result || {};
    const blank = r.blank || null;
    const warns = (c.warnings || []).map((w) =>
      `<span class="wtag" title="${esc(w.msg)}">${esc(WARN_NAMES[w.code] || w.code)}</span>`
    ).join(" ");
    const outlier = (c.warnings || []).some((w) => w.code === "RATE_OUTLIER");
    html += `<tr data-cid="${c.id}" class="${c.included ? "" : "excluded"}${batch.selected === c.id ? " selected" : ""}">
<td>${c.included ? "✔" : "—"}</td>
<td>${c.id}${c.locked ? " 🔒" : ""}</td>
<td>${c.start.toFixed(0)}</td><td>${c.end.toFixed(0)}</td>
<td>${(c.end - c.start).toFixed(0)}</td>
<td>${r.n ?? "—"}</td>
<td>${fmt(r.slope, 8)}</td>
<td class="${r.r2 != null && r.r2 < r2th ? "bad" : ""}">${fmt(r.r2, 4)}</td>
<td>${fmt(r.mo2_raw, 4)}</td>
<td><b>${fmt(r.mo2_net, 4)}</b></td>
<td>${blank && blank.scaled != null ? fmt(blank.scaled, 4) : "—"}</td>
<td class="src" title="${esc(blankTitle(blank))}">${esc(blankSrcText(blank))}</td>
<td class="${outlier ? "outlier" : ""}">${c.outlier_z != null ? c.outlier_z.toFixed(2) : "—"}</td>
<td class="warns">${warns || "—"}</td>
<td><button class="btn-lock" data-cid="${c.id}" title="${c.locked ? "解锁" : "锁定（重新分段时保留边界）"}">${c.locked ? "🔓" : "🔒"}</button></td>
<td><select class="sel-decision" data-cid="${c.id}">
  <option value=""${!c.decision ? " selected" : ""}>自动</option>
  <option value="keep"${c.decision === "keep" ? " selected" : ""}>保留</option>
  <option value="exclude"${c.decision === "exclude" ? " selected" : ""}>剔除</option>
</select></td>
<td><span class="reason" data-cid="${c.id}" title="点击编辑理由">${esc(c.reason) || "…"}</span></td>
<td><button class="btn-del" data-cid="${c.id}" title="删除该周期">✕</button></td>
</tr>`;
  }
  html += "</tbody></table>";
  box.innerHTML = html;

  box.querySelectorAll(".btn-lock").forEach((b) => {
    b.addEventListener("click", () => {
      const c = cycleById(+b.dataset.cid);
      if (c) { c.locked = !c.locked; drawBatchPlot(); renderBatchTable(); }
    });
  });
  box.querySelectorAll(".sel-decision").forEach((s) => {
    s.addEventListener("change", () => {
      const c = cycleById(+s.dataset.cid);
      if (!c) return;
      if (s.value === "exclude") {
        const reason = prompt("剔除理由（必填）：", c.reason || "");
        if (!reason) { s.value = c.decision || ""; return; }
        c.reason = reason;
      } else if (s.value === "keep") {
        const reason = prompt("保留理由（可选，离群周期建议填写）：", c.reason || "");
        if (reason !== null && reason) c.reason = reason;
      }
      c.decision = s.value || null;
      runBatch(false, false);
    });
  });
  box.querySelectorAll(".reason").forEach((sp) => {
    sp.addEventListener("click", () => {
      const c = cycleById(+sp.dataset.cid);
      if (!c) return;
      const reason = prompt("保留/剔除理由：", c.reason || "");
      if (reason === null) return;
      c.reason = reason;
      renderBatchTable();
    });
  });
  box.querySelectorAll(".btn-del").forEach((b) => {
    b.addEventListener("click", () => {
      const c = cycleById(+b.dataset.cid);
      if (!c || !confirm(`删除周期 #${c.id}（${c.start.toFixed(0)}–${c.end.toFixed(0)}s）？`)) return;
      batch.cycles = batch.cycles.filter((x) => x.id !== c.id);
      if (batch.selected === c.id) batch.selected = null;
      runBatch(false, false);
    });
  });
  box.querySelectorAll("tr[data-cid]").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("button,select,.reason")) return;
      batch.selected = +tr.dataset.cid;
      drawBatchPlot(); drawBatchResid(); renderBatchTable();
    });
  });
}

function renderBatchSummary() {
  const box = $("#batch-summary");
  const s = batch.summary;
  if (!s || !batch.cycles.length) { box.innerHTML = ""; }
  else {
    const si = batch.seriesInfo;
    const seriesCard = si && si.kind === "series"
      ? `<div class="sum-card"><div class="sum-v">rev${si.series_rev}</div>` +
        `<div class="sum-k">背景序列·${si.method === "linear" ? "线性插值" : "最近锚点"}</div></div>`
      : "";
    box.innerHTML = seriesCard + `
<div class="sum-card"><div class="sum-v">${s.n_included} / ${s.n_total}</div><div class="sum-k">有效 / 总周期</div></div>
<div class="sum-card"><div class="sum-v">${fmt(s.mean, 4)}</div><div class="sum-k">均值 MO₂ (mg/h)</div></div>
<div class="sum-card"><div class="sum-v">${fmt(s.std, 4)}</div><div class="sum-k">标准差</div></div>
<div class="sum-card"><div class="sum-v">${s.cv == null ? "—" : s.cv.toFixed(1) + "%"}</div><div class="sum-k">变异系数</div></div>
<div class="sum-card"><div class="sum-v">${fmt(s.median, 4)}</div><div class="sum-k">中位数</div></div>`;
  }
  // 告警计数
  const chips = $("#batch-warnings");
  chips.innerHTML = "";
  const counts = {};
  for (const c of batch.cycles)
    for (const w of c.warnings || [])
      counts[w.code] = (counts[w.code] || 0) + 1;
  for (const [code, n] of Object.entries(counts)) {
    const d = document.createElement("span");
    d.className = "chip";
    d.textContent = `${WARN_NAMES[code] || code} × ${n}`;
    chips.appendChild(d);
  }
}

function renderBatchVersions(versions) {
  const ul = $("#batch-version-list");
  ul.innerHTML = "";
  (versions || []).forEach((v, i) => {
    const li = document.createElement("li");
    const time = new Date(v.created_at * 1000).toLocaleTimeString();
    const s = v.summary || {};
    li.innerHTML = `<b>v${i + 1}</b> ${time} ` +
      `<span class="vnote">${esc(v.note || "")} 有效${s.n_included ?? "—"}/${s.n_total ?? "—"}周期 ` +
      `均值=${fmt(s.mean, 4)}</span>`;
    ul.appendChild(li);
  });
}

// ------------------------------------------------------------ 空白序列（时变空白校正）

const sv = {
  id: null,          // 当前编辑的序列 id（null=新建草稿）
  anchors: [],       // 锚点草稿行
};

const SPLOT = { w: 1040, h: 240, ml: 60, mr: 20, mt: 24, mb: 34 };

async function initSeriesView() {
  await loadDatasets();           // 刷新空白室列表与序列列表
  const sel = $("#series-select");
  sel.innerHTML = "";
  for (const s of state.seriesList) {
    const op = document.createElement("option");
    op.value = s.id;
    op.textContent = `${s.name}（rev${s.rev}）`;
    sel.appendChild(op);
  }
  if (sv.id && state.seriesList.some((s) => s.id === sv.id)) {
    sel.value = sv.id;
    loadSeriesIntoEditor(sv.id);
  } else if (state.seriesList.length) {
    sel.value = state.seriesList[0].id;
    loadSeriesIntoEditor(state.seriesList[0].id);
  } else {
    newSeriesDraft();
  }
}

function newSeriesDraft() {
  sv.id = null;
  sv.anchors = [];
  $("#series-select").value = "";
  $("#series-name").value = "背景序列";
  $("#series-method").value = "linear";
  $("#series-maxgap").value = 7200;
  $("#series-msg").textContent = "";
  renderAnchorTable();
  drawSeriesPlot();
}

function loadSeriesIntoEditor(id) {
  const s = state.seriesList.find((x) => x.id === +id);
  if (!s) { newSeriesDraft(); return; }
  sv.id = s.id;
  $("#series-name").value = s.name;
  $("#series-method").value = s.method;
  $("#series-maxgap").value = s.max_gap_s;
  sv.anchors = (s.anchors || []).map((a) => ({ ...a }));
  $("#series-msg").textContent = "";
  renderAnchorTable();
  drawSeriesPlot();
}

$("#series-select").addEventListener("change", () => {
  if ($("#series-select").value) loadSeriesIntoEditor(+$("#series-select").value);
});
$("#btn-series-new").addEventListener("click", newSeriesDraft);
$("#series-method").addEventListener("change", drawSeriesPlot);

// 锚点当前有效速率：锁定值优先，否则取空白室已确认批量均值
function anchorRateOf(a) {
  if (a.locked_rate !== null && a.locked_rate !== "" && a.locked_rate !== undefined)
    return { rate: +a.locked_rate, src: "人工锁定" };
  const br = state.blankRates[a.blank_dataset_id];
  if (br && br.rate != null)
    return { rate: br.rate, src: `批次v${br.version_id} 均值` };
  return { rate: null, src: "缺少已确认批量均值" };
}

function renderAnchorTable() {
  const box = $("#anchor-table");
  const blanks = state.datasets.filter((d) => d.is_blank);
  if (!blanks.length) {
    box.innerHTML = '<p class="hint">尚无空白室数据集：请先在“曲线分析”页上传并勾选“空白室”。</p>';
    return;
  }
  if (!sv.anchors.length) {
    box.innerHTML = '<p class="hint">尚无锚点：点“添加锚点”，为每份空白数据填写采集时刻。</p>';
    return;
  }
  let html = `<table><thead><tr>
<th>锚点</th><th>空白数据集</th><th>采集时刻 (s)</th><th>当前速率 (mg/h)</th>
<th>锁定值</th><th>停用</th><th>备注</th><th>版本</th><th></th></tr></thead><tbody>`;
  sv.anchors.forEach((a, i) => {
    const { rate, src } = anchorRateOf(a);
    const opts = blanks.map((d) =>
      `<option value="${d.id}"${+a.blank_dataset_id === d.id ? " selected" : ""}>${esc(d.name)}</option>`
    ).join("");
    html += `<tr data-idx="${i}" class="${a.disabled ? "anchor-off" : ""}">
<td>${a.id ? "A" + a.id : "新" + (i + 1)}</td>
<td><select class="a-blank">${opts}</select></td>
<td><input type="number" class="a-time" value="${+a.collected_at || 0}" step="60"></td>
<td class="a-rate${rate == null ? " missing" : ""}" title="${esc(src)}">${rate != null ? rate.toFixed(4) : src}</td>
<td><input type="number" class="a-lock" value="${a.locked_rate ?? ""}" placeholder="自动" step="0.001" title="填写后锁定为该人工校正值，留空则自动取批量均值"></td>
<td><input type="checkbox" class="a-off"${a.disabled ? " checked" : ""} title="停用后该锚点不参与校正"></td>
<td><input type="text" class="a-note" value="${esc(a.note || "")}"></td>
<td>${a.rev ? "r" + a.rev : "—"}</td>
<td><button class="a-del" title="删除该锚点">✕</button></td>
</tr>`;
  });
  html += "</tbody></table>";
  box.innerHTML = html;

  box.querySelectorAll("tr[data-idx]").forEach((tr) => {
    const i = +tr.dataset.idx;
    const a = sv.anchors[i];
    tr.querySelector(".a-blank").addEventListener("change", (ev) => {
      a.blank_dataset_id = +ev.target.value;
      renderAnchorTable(); drawSeriesPlot();
    });
    tr.querySelector(".a-time").addEventListener("change", (ev) => {
      a.collected_at = parseFloat(ev.target.value) || 0;
      drawSeriesPlot();
    });
    tr.querySelector(".a-lock").addEventListener("change", (ev) => {
      const v = ev.target.value.trim();
      a.locked_rate = v === "" ? null : parseFloat(v);
      renderAnchorTable(); drawSeriesPlot();
    });
    tr.querySelector(".a-off").addEventListener("change", (ev) => {
      a.disabled = ev.target.checked;
      renderAnchorTable(); drawSeriesPlot();
    });
    tr.querySelector(".a-note").addEventListener("change", (ev) => {
      a.note = ev.target.value;
    });
    tr.querySelector(".a-del").addEventListener("click", () => {
      sv.anchors.splice(i, 1);
      renderAnchorTable(); drawSeriesPlot();
    });
  });
}

$("#btn-anchor-add").addEventListener("click", () => {
  const blanks = state.datasets.filter((d) => d.is_blank);
  if (!blanks.length) { alert("请先在“曲线分析”页上传空白室数据"); return; }
  // 默认时刻：接在现有锚点之后 1 小时
  const last = sv.anchors.length
    ? Math.max(...sv.anchors.map((a) => +a.collected_at || 0)) : 0;
  sv.anchors.push({
    blank_dataset_id: blanks[0].id,
    collected_at: sv.anchors.length ? last + 3600 : 0,
    disabled: false, locked_rate: null, note: "",
  });
  renderAnchorTable(); drawSeriesPlot();
});

// 背景模型预览：锚点速率随采集时刻的变化曲线
function drawSeriesPlot() {
  const svg = $("#series-plot");
  svg.innerHTML = "";
  const rows = sv.anchors.map((a, i) => ({ ...a, _i: i, ...anchorRateOf(a) }));
  if (!rows.length) {
    const t = el("text", { x: 60, y: 40, "font-size": 12, fill: "#888" }, svg);
    t.textContent = "尚无锚点：点击“添加锚点”，为每份空白数据填写采集时刻。";
    return;
  }
  const cfg = SPLOT;
  const pw = cfg.w - cfg.ml - cfg.mr, ph = cfg.h - cfg.mt - cfg.mb;
  const ts = rows.map((a) => +a.collected_at || 0);
  let t0 = Math.min(...ts), t1 = Math.max(...ts);
  const padT = (t1 - t0) * 0.1 || 600;
  t0 -= padT; t1 += padT;
  const act = rows.filter((a) => !a.disabled && a.rate != null)
    .sort((x, y) => x.collected_at - y.collected_at);
  const rates = act.map((a) => a.rate);
  let y0 = rates.length ? Math.min(...rates) : 0;
  let y1 = rates.length ? Math.max(...rates) : 0.1;
  const padY = (y1 - y0) * 0.2 || 0.01;
  y0 -= padY; y1 += padY;
  const X = (t) => cfg.ml + ((t - t0) / (t1 - t0 || 1)) * pw;
  const Y = (r) => cfg.mt + ph - ((r - y0) / (y1 - y0 || 1)) * ph;
  // 模型曲线（线性插值 / 最近锚点阶梯）
  const method = $("#series-method").value;
  if (act.length >= 2) {
    let pts;
    if (method === "nearest") {
      pts = [[t0, act[0].rate]];
      for (let i = 1; i < act.length; i++) {
        const mid = (act[i - 1].collected_at + act[i].collected_at) / 2;
        pts.push([mid, act[i - 1].rate], [mid, act[i].rate]);
      }
      pts.push([t1, act[act.length - 1].rate]);
    } else {
      pts = act.map((a) => [a.collected_at, a.rate]);
    }
    el("polyline", { points: pts.map(([t, r]) => `${X(t).toFixed(1)},${Y(r).toFixed(1)}`).join(" "),
      fill: "none", stroke: "#8a4b08", "stroke-width": 1.6,
      "stroke-dasharray": "6 3" }, svg);
  }
  // 锚点标记
  for (const a of rows) {
    const x = X(+a.collected_at || 0);
    const label = a.id ? `A${a.id}` : `新${a._i + 1}`;
    if (a.disabled || a.rate == null) {
      const y = cfg.mt + ph;
      el("line", { x1: x - 4, y1: y - 4, x2: x + 4, y2: y + 4, stroke: "#999", "stroke-width": 1.5 }, svg);
      el("line", { x1: x - 4, y1: y + 4, x2: x + 4, y2: y - 4, stroke: "#999", "stroke-width": 1.5 }, svg);
      const t = el("text", { x, y: y - 8, "font-size": 10, fill: "#999", "text-anchor": "middle" }, svg);
      t.textContent = label + (a.disabled ? "（停用）" : "（无速率）");
    } else {
      const y = Y(a.rate);
      el("path", { d: `M ${x} ${y - 6} L ${x + 6} ${y} L ${x} ${y + 6} L ${x - 6} ${y} Z`,
        fill: (a.locked_rate !== null && a.locked_rate !== "" && a.locked_rate !== undefined)
          ? "#b3540e" : "#e8a13a",
        stroke: "#8a5a00", "stroke-width": 1 }, svg);
      const t = el("text", { x, y: y - 10, "font-size": 10, fill: "#8a4b08", "text-anchor": "middle" }, svg);
      t.textContent = `${label} ${a.rate.toFixed(4)}`;
    }
  }
  // 坐标轴
  el("line", { x1: cfg.ml, y1: cfg.mt + ph, x2: cfg.ml + pw, y2: cfg.mt + ph, stroke: "#333" }, svg);
  el("line", { x1: cfg.ml, y1: cfg.mt, x2: cfg.ml, y2: cfg.mt + ph, stroke: "#333" }, svg);
  for (let i = 0; i <= 5; i++) {
    const tv = t0 + (t1 - t0) * i / 5;
    const tx = el("text", { x: X(tv), y: cfg.mt + ph + 16, "font-size": 10,
                            "text-anchor": "middle", fill: "#666" }, svg);
    tx.textContent = tv.toFixed(0);
    const rv = y0 + (y1 - y0) * i / 5;
    const ty = el("text", { x: cfg.ml - 6, y: Y(rv) + 3, "font-size": 10,
                            "text-anchor": "end", fill: "#666" }, svg);
    ty.textContent = rv.toFixed(4);
  }
  const xl = el("text", { x: cfg.ml + pw / 2, y: cfg.h - 4, "font-size": 11,
                          "text-anchor": "middle", fill: "#666" }, svg);
  xl.textContent = "采集时刻 (s)";
  const yl = el("text", { x: 14, y: cfg.mt + ph / 2, "font-size": 11, fill: "#666",
    "text-anchor": "middle", transform: `rotate(-90 14 ${cfg.mt + ph / 2})` }, svg);
  yl.textContent = "空白速率 (mg/h)";
}

$("#btn-series-save").addEventListener("click", async () => {
  const payload = {
    id: sv.id,
    name: $("#series-name").value,
    method: $("#series-method").value,
    max_gap_s: parseFloat($("#series-maxgap").value) || 7200,
    anchors: sv.anchors.map((a) => ({
      id: a.id || undefined,
      blank_dataset_id: +a.blank_dataset_id,
      collected_at: parseFloat(a.collected_at) || 0,
      disabled: !!a.disabled,
      locked_rate: (a.locked_rate === "" || a.locked_rate == null) ? null : +a.locked_rate,
      note: a.note || "",
    })),
  };
  const msg = $("#series-msg");
  try {
    const data = await api("/api/series/save", {
      method: "POST", body: JSON.stringify(payload) });
    sv.id = data.series.id;
    msg.className = "msg";
    msg.textContent = data.changed
      ? `已保存，序列 rev${data.rev}` : "内容无变化";
    renderAffected(data.affected, data.changed);
    await initSeriesView();           // 刷新锚点 rev 与速率显示
  } catch (e) {
    msg.textContent = "保存失败: " + e.message;
    msg.className = "msg err";
  }
});

function renderAffected(affected, changed) {
  const box = $("#series-affected");
  if (!changed) {
    box.innerHTML = '<span class="hint">序列无变化，未触发重算。</span>';
    return;
  }
  if (!affected || !affected.length) {
    box.innerHTML = '<span class="hint">无受影响样本（没有样本引用本序列，或空白速率未变）。</span>';
    return;
  }
  box.innerHTML = "<ul>" + affected.map((a) =>
    `<li><b>${esc(a.name)}</b>：v${a.from_version} → v${a.new_version}（自动重算，旧版本仍可查阅）</li>`
  ).join("") + "</ul>";
}

// ------------------------------------------------------------ 启动

loadDatasets();
