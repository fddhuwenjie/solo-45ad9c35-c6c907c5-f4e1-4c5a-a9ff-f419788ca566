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

function scales(points) {
  const ts = points.map((p) => p.t);
  const ys = points.map((p) => (p.o2_corr !== undefined ? p.o2_corr : p.o2));
  let t0 = Math.min(...ts), t1 = Math.max(...ts);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.08 || 0.1;
  y0 -= pad; y1 += pad;
  const pw = PLOT.w - PLOT.ml - PLOT.mr, ph = PLOT.h - PLOT.mt - PLOT.mb;
  return {
    t0, t1, y0, y1,
    X: (t) => PLOT.ml + ((t - t0) / (t1 - t0 || 1)) * pw,
    Y: (y) => PLOT.mt + ph - ((y - y0) / (y1 - y0 || 1)) * ph,
    invX: (x) => t0 + ((x - PLOT.ml) / pw) * (t1 - t0),
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
  const sc = scales(pts);
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

// ------------------------------------------------------------ 启动

loadDatasets();
