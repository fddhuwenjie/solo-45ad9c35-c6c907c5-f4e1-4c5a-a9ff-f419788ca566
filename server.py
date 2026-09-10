#!/usr/bin/env python3
"""间歇式呼吸室分析工作台 —— 基于 Python 内置 WSGI 的服务端。

运行: python3 server.py [--port 8000]
仅使用标准库 (wsgiref + sqlite3)。
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

import analysis
import db

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ------------------------------------------------------------------ 工具

def json_response(start_response, obj, status="200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def text_response(start_response, text, mime="text/plain; charset=utf-8",
                  filename=None, status="200 OK"):
    body = text.encode("utf-8")
    headers = [("Content-Type", mime), ("Content-Length", str(len(body)))]
    if filename:
        headers.append(("Content-Disposition",
                        f'attachment; filename="{filename}"'))
    start_response(status, headers)
    return [body]


def read_body(environ):
    length = int(environ.get("CONTENT_LENGTH") or 0)
    return environ["wsgi.input"].read(length) if length else b""


def get_conn():
    return db.connect()


def _blank_rate_volume(conn, blank_id):
    """空白室速率与体积：优先其最近批次版本的有效周期均值，
    否则取最近单窗分析版本。返回 (rate, volume) 或 (None, None)。"""
    bvs = db.list_batch_versions(conn, blank_id)
    if bvs:
        s = bvs[-1]["summary"]
        if s.get("mean") is not None:
            return s["mean"], s.get("volume")
    vs = db.list_versions(conn, blank_id)
    if vs:
        r = vs[-1]["result"]
        if r.get("mo2_net") is not None:
            return r["mo2_net"], r.get("volume")
    return None, None


# ------------------------------------------------------------------ API

def api_upload(environ, qs):
    """POST /api/upload?name=&sample_id=&is_blank=  body=CSV 文本"""
    conn = get_conn()
    text = read_body(environ).decode("utf-8-sig")
    try:
        points, errors = analysis.parse_csv(text)
    except ValueError as exc:
        return None, {"ok": False, "error": str(exc)}, "400 Bad Request"
    if not points:
        return None, {"ok": False, "error": "没有有效数据行",
                      "errors": errors}, "400 Bad Request"
    name = qs.get("name", ["未命名"])[0]
    sample_id = qs.get("sample_id", [""])[0]
    is_blank = qs.get("is_blank", ["0"])[0] in ("1", "true", "是")
    filename = qs.get("filename", [""])[0]
    ds_id = db.create_dataset(conn, name, sample_id, is_blank, filename, points)
    conn.close()
    return None, {"ok": True, "id": ds_id, "n_points": len(points),
                  "parse_errors": errors}, "200 OK"


def api_datasets(_qs):
    conn = get_conn()
    out = db.list_datasets(conn)
    conn.close()
    return {"ok": True, "datasets": out}


def api_dataset(ds_id):
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    if not meta:
        conn.close()
        return None, {"ok": False, "error": "数据集不存在"}, "404 Not Found"
    points = db.get_points(conn, ds_id)
    versions = db.list_versions(conn, ds_id)
    batch_versions = _slim_batch_versions(db.list_batch_versions(conn, ds_id))
    conn.close()
    return {"ok": True, "dataset": meta, "points": points,
            "versions": versions, "batch_versions": batch_versions}, None


def api_analyze(environ):
    """POST /api/analyze  body=JSON 参数；保存为新版本。"""
    payload = json.loads(read_body(environ).decode("utf-8"))
    ds_id = int(payload["dataset_id"])
    conn = get_conn()
    points = db.get_points(conn, ds_id)
    if not points:
        conn.close()
        return None, {"ok": False, "error": "数据集不存在"}, "404 Not Found"

    blank_rate = blank_volume = None
    blank_id = payload.get("blank_id")
    if blank_id:
        blank_id = int(blank_id)
        # 空白室速率：优先批次版本均值，否则取最新单窗分析版本
        blank_rate, blank_volume = _blank_rate_volume(conn, blank_id)
        if blank_rate is None:
            conn.close()
            return None, {"ok": False,
                          "error": "空白室尚未分析，请先在空白室数据上运行一次分析"}, \
                "400 Bad Request"

    params = {
        "dataset_id": ds_id,
        "window": payload["window"],
        "calib_points": payload.get("calib_points", []),
        "blank_id": blank_id,
        "r2_threshold": float(payload.get("r2_threshold", 0.9)),
        "min_points": int(payload.get("min_points", 5)),
    }
    result = analysis.analyze(
        points, tuple(params["window"]),
        calib_points=[tuple(c) for c in params["calib_points"]],
        blank_rate=blank_rate, blank_volume=blank_volume,
        r2_threshold=params["r2_threshold"],
        min_points=params["min_points"])
    db.save_version(conn, ds_id, params, result,
                    note=payload.get("note", ""))
    conn.close()
    # 响应保留窗内点与残差供前端绘图；corrected 单独返回
    slim = {k: v for k, v in result.items() if k != "corrected"}
    return {"ok": True, "params": params, "result": slim,
            "corrected": result["corrected"]}, None


def api_undo(environ):
    payload = json.loads(read_body(environ).decode("utf-8"))
    ds_id = int(payload["dataset_id"])
    conn = get_conn()
    ok, msg, current = db.undo_version(conn, ds_id)
    versions = db.list_versions(conn, ds_id)
    conn.close()
    return {"ok": ok, "message": msg, "current": current,
            "versions": versions}


def api_versions(ds_id):
    conn = get_conn()
    out = db.list_versions(conn, ds_id)
    conn.close()
    return {"ok": True, "versions": out}


def api_exclude(environ):
    payload = json.loads(read_body(environ).decode("utf-8"))
    conn = get_conn()
    if payload.get("undo"):
        db.remove_exclusion(conn, int(payload["exclusion_id"]))
    elif payload.get("undo_dataset"):
        # 撤销指定数据集最近一条剔除记录
        row = conn.execute(
            "SELECT id FROM exclusions WHERE dataset_id=? ORDER BY id DESC"
            " LIMIT 1", (int(payload["dataset_id"]),)).fetchone()
        if row:
            db.remove_exclusion(conn, row["id"])
    else:
        reason = payload.get("reason", "").strip()
        if not reason:
            conn.close()
            return None, {"ok": False, "error": "剔除理由不能为空"}, \
                "400 Bad Request"
        db.add_exclusion(conn, int(payload["dataset_id"]), reason)
    conn.close()
    return {"ok": True}


def api_groups(_qs):
    """按 sample_id 归组比较，附剔除记录。"""
    conn = get_conn()
    datasets = db.list_datasets(conn)
    exclusions = db.list_exclusions(conn)
    excluded_ids = {e["dataset_id"] for e in exclusions}
    groups = {}
    for d in datasets:
        if d["is_blank"]:
            continue
        sid = d["sample_id"] or "(未分组)"
        g = groups.setdefault(sid, {"sample_id": sid, "measurements": [],
                                    "excluded": []})
        versions = db.list_versions(conn, d["id"])
        latest = versions[-1]["result"] if versions else None
        entry = {"dataset_id": d["id"], "name": d["name"],
                 "n_versions": len(versions),
                 "mo2_net": latest.get("mo2_net") if latest else None,
                 "r2": latest.get("r2") if latest else None}
        if d["id"] in excluded_ids:
            reasons = [e["reason"] for e in exclusions
                       if e["dataset_id"] == d["id"]]
            g["excluded"].append({**entry, "reasons": reasons})
        else:
            g["measurements"].append(entry)
    for g in groups.values():
        rates = [m["mo2_net"] for m in g["measurements"]
                 if m["mo2_net"] is not None]
        g["mean_mo2"] = sum(rates) / len(rates) if rates else None
        g["n_valid"] = len(rates)
    conn.close()
    return {"ok": True, "groups": sorted(groups.values(),
                                         key=lambda g: g["sample_id"])}


def api_export_csv(ds_id):
    """修正曲线 CSV：time, o2_raw, drift_offset, o2_corr, event。"""
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    points = db.get_points(conn, ds_id)
    versions = db.list_versions(conn, ds_id)
    conn.close()
    if not meta:
        return None, {"ok": False, "error": "数据集不存在"}, "404 Not Found"
    calib = []
    if versions:
        calib = [tuple(c) for c in versions[-1]["params"].get(
            "calib_points", [])]
    corrected = analysis.drift_correct([dict(p) for p in points], calib)
    lines = ["time,o2_raw,drift_offset,o2_corrected,event"]
    for p in corrected:
        lines.append(",".join([
            f"{p['t']:.3f}", f"{p['o2']:.5f}", f"{p['offset']:.5f}",
            f"{p['o2_corr']:.5f}", p.get("event") or ""]))
    name = re.sub(r"[^\w\-]+", "_", meta["name"])
    return ("csv", "\n".join(lines), f"{name}_corrected.csv"), None


def api_export_json(ds_id):
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    versions = db.list_versions(conn, ds_id)
    conn.close()
    if not meta or not versions:
        return None, {"ok": False, "error": "数据集或版本不存在"}, \
            "404 Not Found"
    v = versions[-1]
    payload = {"dataset": meta, "version_id": v["id"],
               "created_at": v["created_at"], "params": v["params"],
               "result": v["result"]}
    name = re.sub(r"[^\w\-]+", "_", meta["name"])
    return ("json", json.dumps(payload, ensure_ascii=False, indent=2),
            f"{name}_window_params.json"), None


# ------------------------------------------------------------------ 批量分析

def _slim_batch_versions(versions):
    """列表场景只保留摘要，周期明细按需单独取。"""
    return [{"id": v["id"], "created_at": v["created_at"], "note": v["note"],
             "summary": v["summary"]} for v in versions]


def _cycle_store(c):
    """入库/导出用：去掉窗内点列与残差（可由参数重算）。"""
    r = {k: v for k, v in c["result"].items()
         if k not in ("window_points", "residuals")}
    return {**c, "result": r}


def _cycle_wire(c):
    """响应用：窗内点列压缩为时间轴，保留残差供前端绘图。"""
    r = {k: v for k, v in c["result"].items() if k != "window_points"}
    r["resid_t"] = [p["t"] for p in c["result"].get("window_points", [])]
    return {**c, "result": r}


def api_batch_analyze(environ):
    """POST /api/batch/analyze —— 多周期批量分析。

    body: {dataset_id, resegment, seg, cycles, calib_points, blank_id,
           r2_threshold, min_points, min_duration, save, note}
    resegment=true 时按 seg 规则重新分段，锁定周期保留。
    """
    payload = json.loads(read_body(environ).decode("utf-8"))
    ds_id = int(payload["dataset_id"])
    conn = get_conn()
    points = db.get_points(conn, ds_id)
    if not points:
        conn.close()
        return None, {"ok": False, "error": "数据集不存在"}, "404 Not Found"

    calib = [tuple(c) for c in payload.get("calib_points", [])]
    blank_rate = blank_volume = None
    blank_id = payload.get("blank_id")
    if blank_id:
        blank_id = int(blank_id)
        blank_rate, blank_volume = _blank_rate_volume(conn, blank_id)
        if blank_rate is None:
            conn.close()
            return None, {"ok": False,
                          "error": "空白室尚未分析，请先对空白室运行单窗或批量分析"}, \
                "400 Bad Request"

    corrected = analysis.drift_correct([dict(p) for p in points], calib)
    seg = payload.get("seg") or {}
    min_duration = float(payload.get("min_duration")
                         or seg.get("min_duration") or 60.0)
    seg.setdefault("min_duration", min_duration)

    n_candidates = None
    if payload.get("resegment"):
        candidates = analysis.segment(corrected, seg)
        cycles = analysis.merge_locked_cycles(payload.get("cycles") or [],
                                              candidates)
        n_candidates = len(candidates)
    else:
        cycles = payload.get("cycles") or []

    r2_threshold = float(payload.get("r2_threshold", 0.9))
    min_points = int(payload.get("min_points", 5))
    batch = analysis.analyze_cycles(
        corrected, cycles, blank_rate=blank_rate, blank_volume=blank_volume,
        r2_threshold=r2_threshold, min_points=min_points,
        min_duration=min_duration)

    if blank_rate is not None:
        batch["summary"]["blank"] = {"dataset_id": blank_id,
                                     "rate": blank_rate,
                                     "volume": blank_volume}

    params = {"dataset_id": ds_id, "seg": seg,
              "calib_points": payload.get("calib_points", []),
              "blank_id": blank_id, "r2_threshold": r2_threshold,
              "min_points": min_points, "min_duration": min_duration}
    version_id = None
    if payload.get("save"):
        version_id = db.save_batch_version(
            conn, ds_id, params,
            [_cycle_store(c) for c in batch["cycles"]], batch["summary"],
            note=payload.get("note", ""))
    versions = _slim_batch_versions(db.list_batch_versions(conn, ds_id))
    conn.close()
    return {"ok": True, "params": params,
            "cycles": [_cycle_wire(c) for c in batch["cycles"]],
            "summary": batch["summary"], "corrected": corrected,
            "n_candidates": n_candidates, "version_id": version_id,
            "versions": versions}, None


def api_batch_undo(environ):
    payload = json.loads(read_body(environ).decode("utf-8"))
    ds_id = int(payload["dataset_id"])
    conn = get_conn()
    ok, msg, current = db.undo_batch_version(conn, ds_id)
    versions = _slim_batch_versions(db.list_batch_versions(conn, ds_id))
    conn.close()
    return {"ok": ok, "message": msg, "current": current,
            "versions": versions}


def api_batch_versions(ds_id):
    conn = get_conn()
    out = _slim_batch_versions(db.list_batch_versions(conn, ds_id))
    conn.close()
    return {"ok": True, "versions": out}


def api_batch_version(vid):
    conn = get_conn()
    v = db.get_batch_version(conn, vid)
    conn.close()
    if not v:
        return None, {"ok": False, "error": "批次版本不存在"}, "404 Not Found"
    return {"ok": True, "version": v}, None


def _latest_batch(conn, ds_id):
    versions = db.list_batch_versions(conn, ds_id)
    return versions[-1] if versions else None


def _csv_cell(s):
    s = str(s)
    if any(ch in s for ch in ",\"\n"):
        return '"' + s.replace('"', '""') + '"'
    return s


def api_batch_export_csv(ds_id):
    """逐周期结果 CSV：每行一个周期的全部指标与裁决信息。"""
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    v = _latest_batch(conn, ds_id)
    conn.close()
    if not meta or not v:
        return None, {"ok": False, "error": "数据集或批次版本不存在"}, \
            "404 Not Found"
    lines = ["cycle,start_s,end_s,duration_s,n_points,slope,intercept,r2,"
             "mean_temp_c,mean_press_kpa,volume_l,stp_factor,mo2_raw_mg_h,"
             "mo2_net_mg_h,included,decision,reason,warnings"]
    for c in v["cycles"]:
        r = c["result"]

        def f(x, nd=6):
            return f"{x:.{nd}f}" if isinstance(x, (int, float)) else ""

        warns = "|".join(w["code"] for w in c.get("warnings", []))
        lines.append(",".join([
            str(c["id"]), f(c["start"], 1), f(c["end"], 1),
            f(c["end"] - c["start"], 1), str(r.get("n", "")),
            f(r.get("slope"), 9), f(r.get("intercept"), 6), f(r.get("r2"), 5),
            f(r.get("mean_temp"), 2), f(r.get("mean_press"), 2),
            f(r.get("volume"), 3), f(r.get("stp_factor"), 5),
            f(r.get("mo2_raw"), 5), f(r.get("mo2_net"), 5),
            "1" if c.get("included") else "0",
            c.get("decision") or "", _csv_cell(c.get("reason", "")),
            _csv_cell(warns)]))
    name = re.sub(r"[^\w\-]+", "_", meta["name"])
    return ("csv", "\n".join(lines), f"{name}_cycles.csv"), None


def api_batch_export_json(ds_id):
    """分段参数 + 逐周期结果 + 汇总 JSON。"""
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    v = _latest_batch(conn, ds_id)
    conn.close()
    if not meta or not v:
        return None, {"ok": False, "error": "数据集或批次版本不存在"}, \
            "404 Not Found"
    payload = {"dataset": meta, "version_id": v["id"],
               "created_at": v["created_at"], "note": v["note"],
               "params": v["params"], "cycles": v["cycles"],
               "summary": v["summary"]}
    name = re.sub(r"[^\w\-]+", "_", meta["name"])
    return ("json", json.dumps(payload, ensure_ascii=False, indent=2),
            f"{name}_batch_params.json"), None


# ------------------------------------------------------------------ 报告

def _svg_chart(corrected, window, result, width=760, height=320):
    """服务端生成内联 SVG 曲线图。"""
    if not corrected:
        return ""
    ts = [p["t"] for p in corrected]
    ys = [p["o2_corr"] for p in corrected]
    t0, t1 = min(ts), max(ts)
    y0, y1 = min(ys), max(ys)
    pad_y = (y1 - y0) * 0.08 or 0.1
    y0, y1 = y0 - pad_y, y1 + pad_y
    ml, mr, mt, mb = 55, 15, 15, 35
    pw, ph = width - ml - mr, height - mt - mb

    def X(t):
        return ml + (t - t0) / (t1 - t0 or 1) * pw

    def Y(y):
        return mt + ph - (y - y0) / (y1 - y0 or 1) * ph

    pts = " ".join(f"{X(p['t']):.1f},{Y(p['o2_corr']):.1f}" for p in corrected)
    parts = [f'<svg width="{width}" height="{height}" '
             f'xmlns="http://www.w3.org/2000/svg" '
             f'style="background:#fff;border:1px solid #ccc">']
    # 窗口高亮
    if window:
        x0, x1 = X(window[0]), X(window[1])
        parts.append(f'<rect x="{x0:.1f}" y="{mt}" width="{x1-x0:.1f}" '
                     f'height="{ph}" fill="#cfe8cf" opacity="0.5"/>')
    # 事件线
    for p in corrected:
        if p.get("event"):
            parts.append(
                f'<line x1="{X(p["t"]):.1f}" y1="{mt}" x2="{X(p["t"]):.1f}" '
                f'y2="{mt+ph}" stroke="#d9534f" stroke-dasharray="4 3"/>')
            parts.append(
                f'<text x="{X(p["t"])+3:.1f}" y="{mt+12}" font-size="10" '
                f'fill="#d9534f">{p["event"]}</text>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#2266aa" '
                 f'stroke-width="1.5"/>')
    # 拟合线
    if result and result.get("slope") is not None and window:
        s, b = result["slope"], result["intercept"]
        parts.append(
            f'<line x1="{X(window[0]):.1f}" y1="{Y(s*window[0]+b):.1f}" '
            f'x2="{X(window[1]):.1f}" y2="{Y(s*window[1]+b):.1f}" '
            f'stroke="#cc3300" stroke-width="2"/>')
    # 坐标轴
    parts.append(f'<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" '
                 f'stroke="#333"/>')
    parts.append(f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" '
                 f'stroke="#333"/>')
    for i in range(6):
        tv = t0 + (t1 - t0) * i / 5
        parts.append(f'<text x="{X(tv):.1f}" y="{mt+ph+18}" font-size="10" '
                     f'text-anchor="middle">{tv:.0f}</text>')
        yv = y0 + (y1 - y0) * i / 5
        parts.append(f'<text x="{ml-6}" y="{Y(yv)+3:.1f}" font-size="10" '
                     f'text-anchor="end">{yv:.2f}</text>')
    parts.append(f'<text x="{ml+pw/2:.0f}" y="{height-4}" font-size="11" '
                 f'text-anchor="middle">时间 (s)</text>')
    parts.append(f'<text x="12" y="{mt+ph/2:.0f}" font-size="11" '
                 f'transform="rotate(-90 12 {mt+ph/2:.0f})" '
                 f'text-anchor="middle">O₂ (mg/L)</text>')
    parts.append("</svg>")
    return "".join(parts)


def api_report(ds_id):
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    points = db.get_points(conn, ds_id)
    versions = db.list_versions(conn, ds_id)
    conn.close()
    if not meta or not versions:
        return None, {"ok": False, "error": "数据集或版本不存在"}, \
            "404 Not Found"
    v = versions[-1]
    params, result = v["params"], v["result"]
    corrected = analysis.drift_correct(
        [dict(p) for p in points],
        [tuple(c) for c in params.get("calib_points", [])])
    svg = _svg_chart(corrected, params.get("window"), result)

    warn_html = "".join(
        f'<li class="warn">[{w["code"]}] {w["msg"]}</li>'
        for w in result.get("warnings", [])) or "<li>无</li>"

    def fmt(x, nd=5):
        return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "—"

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>实验报告 - {meta['name']}</title><style>
body{{font-family:system-ui,sans-serif;max-width:820px;margin:2em auto;
padding:0 1em;color:#222}}
table{{border-collapse:collapse;margin:1em 0}}
td,th{{border:1px solid #bbb;padding:4px 12px;font-size:14px}}
.warn{{color:#b3540e}} h1{{font-size:22px}} h2{{font-size:17px}}
</style></head><body>
<h1>间歇式呼吸室实验报告</h1>
<p>数据集：<b>{meta['name']}</b>　样本编号：{meta['sample_id'] or '—'}
　版本：v{v['id']}　分析时间：{__import__('datetime').datetime
.fromtimestamp(v['created_at']).strftime('%Y-%m-%d %H:%M:%S')}</p>
<h2>修正曲线与拟合</h2>{svg}
<h2>窗口与拟合参数</h2>
<table>
<tr><th>窗口 (s)</th><td>{params['window'][0]:.1f} ~ {params['window'][1]:.1f}</td></tr>
<tr><th>有效点数</th><td>{result.get('n')}</td></tr>
<tr><th>斜率 (mg/L/s)</th><td>{fmt(result.get('slope'), 8)}</td></tr>
<tr><th>R²</th><td>{fmt(result.get('r2'))}</td></tr>
<tr><th>平均温度 (°C)</th><td>{fmt(result.get('mean_temp'), 2)}</td></tr>
<tr><th>平均气压 (kPa)</th><td>{fmt(result.get('mean_press'), 2)}</td></tr>
<tr><th>舱体体积 (L)</th><td>{fmt(result.get('volume'), 3)}</td></tr>
<tr><th>标准状况换算系数</th><td>{fmt(result.get('stp_factor'))}</td></tr>
<tr><th>耗氧率 MO₂ (mg/h，空白修正前)</th><td>{fmt(result.get('mo2_raw'))}</td></tr>
<tr><th>耗氧率 MO₂ (mg/h，空白修正后)</th><td><b>{fmt(result.get('mo2_net'))}</b></td></tr>
</table>
<h2>校准点（漂移修正）</h2>
<p>{'；'.join(f't={c[0]:.0f}s → {c[1]:.3f} mg/L'
             for c in params.get('calib_points', [])) or '未设置'}</p>
<h2>告警</h2><ul>{warn_html}</ul>
</body></html>"""
    name = re.sub(r"[^\w\-]+", "_", meta["name"])
    return ("html", html, f"{name}_report.html"), None


# ------------------------------------------------------------------ 批次报告

def _svg_overview(corrected, cycles, width=900, height=330):
    """批次报告总览图：全程修正曲线 + 周期色带 + 各周期拟合线。"""
    if not corrected:
        return ""
    ts = [p["t"] for p in corrected]
    ys = [p["o2_corr"] for p in corrected]
    t0, t1 = min(ts), max(ts)
    y0, y1 = min(ys), max(ys)
    pad_y = (y1 - y0) * 0.08 or 0.1
    y0, y1 = y0 - pad_y, y1 + pad_y
    ml, mr, mt, mb = 55, 15, 15, 35
    pw, ph = width - ml - mr, height - mt - mb

    def X(t):
        return ml + (t - t0) / (t1 - t0 or 1) * pw

    def Y(y):
        return mt + ph - (y - y0) / (y1 - y0 or 1) * ph

    parts = [f'<svg width="{width}" height="{height}" '
             f'xmlns="http://www.w3.org/2000/svg" '
             f'style="background:#fff;border:1px solid #ccc">']
    # 周期色带（绿=计入汇总，红=未计入）
    for c in cycles:
        x0, x1 = X(c["start"]), X(c["end"])
        fill = "#cfe8cf" if c.get("included") else "#f0d5d5"
        parts.append(f'<rect x="{x0:.1f}" y="{mt}" width="{x1-x0:.1f}" '
                     f'height="{ph}" fill="{fill}" opacity="0.55"/>')
        parts.append(f'<text x="{(x0+x1)/2:.1f}" y="{mt+12}" font-size="10" '
                     f'text-anchor="middle" fill="#555">#{c["id"]}</text>')
    # 事件线
    for p in corrected:
        if p.get("event"):
            parts.append(
                f'<line x1="{X(p["t"]):.1f}" y1="{mt}" x2="{X(p["t"]):.1f}" '
                f'y2="{mt+ph}" stroke="#d9534f" stroke-dasharray="4 3"/>')
            parts.append(
                f'<text x="{X(p["t"])+3:.1f}" y="{mt+24}" font-size="10" '
                f'fill="#d9534f">{p["event"]}</text>')
    # 修正曲线
    pts = " ".join(f"{X(p['t']):.1f},{Y(p['o2_corr']):.1f}" for p in corrected)
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#2266aa" '
                 f'stroke-width="1.5"/>')
    # 各周期拟合线
    for c in cycles:
        r = c.get("result") or {}
        if r.get("slope") is not None:
            s, b = r["slope"], r["intercept"]
            op = "1" if c.get("included") else "0.4"
            parts.append(
                f'<line x1="{X(c["start"]):.1f}" y1="{Y(s*c["start"]+b):.1f}" '
                f'x2="{X(c["end"]):.1f}" y2="{Y(s*c["end"]+b):.1f}" '
                f'stroke="#cc3300" stroke-width="2" opacity="{op}"/>')
    # 坐标轴
    parts.append(f'<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" '
                 f'stroke="#333"/>')
    parts.append(f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" '
                 f'stroke="#333"/>')
    for i in range(6):
        tv = t0 + (t1 - t0) * i / 5
        parts.append(f'<text x="{X(tv):.1f}" y="{mt+ph+18}" font-size="10" '
                     f'text-anchor="middle">{tv:.0f}</text>')
        yv = y0 + (y1 - y0) * i / 5
        parts.append(f'<text x="{ml-6}" y="{Y(yv)+3:.1f}" font-size="10" '
                     f'text-anchor="end">{yv:.2f}</text>')
    parts.append(f'<text x="{ml+pw/2:.0f}" y="{height-4}" font-size="11" '
                 f'text-anchor="middle">时间 (s)</text>')
    parts.append(f'<text x="12" y="{mt+ph/2:.0f}" font-size="11" '
                 f'transform="rotate(-90 12 {mt+ph/2:.0f})" '
                 f'text-anchor="middle">O₂ (mg/L)</text>')
    parts.append("</svg>")
    return "".join(parts)


def api_batch_report(ds_id):
    """批次报告 HTML：总览图 + 汇总统计 + 逐周期结果 + 分段参数。"""
    import html as _html
    conn = get_conn()
    meta = db.get_dataset(conn, ds_id)
    points = db.get_points(conn, ds_id)
    v = _latest_batch(conn, ds_id)
    conn.close()
    if not meta or not v:
        return None, {"ok": False, "error": "数据集或批次版本不存在"}, \
            "404 Not Found"
    esc = _html.escape
    params, cycles, summary = v["params"], v["cycles"], v["summary"]
    corrected = analysis.drift_correct(
        [dict(p) for p in points],
        [tuple(c) for c in params.get("calib_points", [])])
    svg = _svg_overview(corrected, cycles)

    def fmt(x, nd=5):
        return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "—"

    seg = params.get("seg") or {}
    if seg.get("method") == "rise":
        seg_desc = (f"按氧浓度回升阈值（{seg.get('rise_threshold')} mg/L/s，"
                    f"最短持续 {seg.get('rise_min_duration')} s）")
    else:
        seg_desc = f"按 flush 事件（关键字 “{esc(str(seg.get('event_keyword', 'flush')))}”）"

    blank = summary.get("blank")
    blank_desc = (f"空白室 #{blank['dataset_id']}，速率 "
                  f"{fmt(blank['rate'], 4)} mg/h（体积 "
                  f"{fmt(blank['volume'], 3)} L，按体积比折算扣除）") \
        if blank else "未使用"

    cyc_rows = []
    for c in cycles:
        r = c["result"]
        warns = "；".join(f'[{w["code"]}] {esc(w["msg"])}'
                          for w in c.get("warnings", [])) or "—"
        decision = {"keep": "保留", "exclude": "剔除"}.get(
            c.get("decision"), "自动")
        reason = f'：{esc(c["reason"])}' if c.get("reason") else ""
        z = f'{c["outlier_z"]:.2f}' if isinstance(
            c.get("outlier_z"), (int, float)) else "—"
        cyc_rows.append(
            f"<tr class='{'' if c.get('included') else 'ex'}'>"
            f"<td>{c['id']}{' 🔒' if c.get('locked') else ''}</td>"
            f"<td>{c['start']:.0f} ~ {c['end']:.0f}</td>"
            f"<td>{c['end']-c['start']:.0f}</td>"
            f"<td>{r.get('n', '—')}</td>"
            f"<td>{fmt(r.get('slope'), 8)}</td>"
            f"<td>{fmt(r.get('r2'), 4)}</td>"
            f"<td>{fmt(r.get('mo2_raw'), 4)}</td>"
            f"<td><b>{fmt(r.get('mo2_net'), 4)}</b></td>"
            f"<td>{z}</td>"
            f"<td>{'✔' if c.get('included') else '✘'}</td>"
            f"<td>{decision}{reason}</td>"
            f"<td class='warn'>{warns}</td></tr>")

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>批次分析报告 - {esc(meta['name'])}</title><style>
body{{font-family:system-ui,sans-serif;max-width:960px;margin:2em auto;
padding:0 1em;color:#222}}
table{{border-collapse:collapse;margin:1em 0}}
td,th{{border:1px solid #bbb;padding:4px 10px;font-size:13px}}
th{{background:#f0f2f5;text-align:left}}
.warn{{color:#b3540e;font-size:12px}} .ex td{{color:#999;background:#fafafa}}
h1{{font-size:22px}} h2{{font-size:17px}}
</style></head><body>
<h1>间歇式呼吸室批次分析报告</h1>
<p>数据集：<b>{esc(meta['name'])}</b>　样本编号：{esc(meta['sample_id']) or '—'}
　批次版本：v{v['id']}　分析时间：{__import__('datetime').datetime
.fromtimestamp(v['created_at']).strftime('%Y-%m-%d %H:%M:%S')}</p>
<h2>总览（绿=计入汇总，红=未计入）</h2>{svg}
<h2>有效周期汇总（MO₂，mg/h）</h2>
<table>
<tr><th>有效 / 总周期</th><td>{summary['n_included']} / {summary['n_total']}</td></tr>
<tr><th>均值</th><td><b>{fmt(summary.get('mean'), 4)}</b></td></tr>
<tr><th>标准差</th><td>{fmt(summary.get('std'), 4)}</td></tr>
<tr><th>变异系数</th><td>{fmt(summary.get('cv'), 2)}%</td></tr>
<tr><th>中位数</th><td>{fmt(summary.get('median'), 4)}</td></tr>
<tr><th>平均舱体体积 (L)</th><td>{fmt(summary.get('volume'), 3)}</td></tr>
</table>
<h2>逐周期结果</h2>
<table>
<tr><th>#</th><th>窗口 (s)</th><th>时长 (s)</th><th>点数</th>
<th>斜率 (mg/L/s)</th><th>R²</th><th>MO₂ 修正前</th><th>MO₂ 空白修正后</th>
<th>离群 z</th><th>计入</th><th>裁决</th><th>告警</th></tr>
{''.join(cyc_rows)}
</table>
<h2>分段与分析参数</h2>
<table>
<tr><th>分段方式</th><td>{seg_desc}</td></tr>
<tr><th>冲洗后跳过 / 周期末预留 / 最短测量段</th>
<td>{seg.get('flush_offset')} s / {seg.get('end_margin')} s /
{seg.get('min_duration')} s</td></tr>
<tr><th>R² 阈值 / 最少点数</th>
<td>{params.get('r2_threshold')} / {params.get('min_points')}</td></tr>
<tr><th>校准点（漂移修正）</th>
<td>{'；'.join(f't={c[0]:.0f}s → {c[1]:.3f} mg/L'
             for c in params.get('calib_points', [])) or '未设置'}</td></tr>
<tr><th>空白修正</th><td>{blank_desc}</td></tr>
</table>
</body></html>"""
    name = re.sub(r"[^\w\-]+", "_", meta["name"])
    return ("html", html, f"{name}_batch_report.html"), None


# ------------------------------------------------------------------ 路由

def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    qs = parse_qs(environ.get("QUERY_STRING", ""))

    try:
        if path == "/":
            return serve_static(start_response, "index.html")
        if path.startswith("/static/"):
            return serve_static(start_response, path[len("/static/"):])

        m = re.fullmatch(r"/api/dataset/(\d+)", path)
        if m and method == "GET":
            out, err = api_dataset(int(m.group(1)))
            return json_response(start_response, err or out,
                                 "404 Not Found" if err else "200 OK")
        m = re.fullmatch(r"/api/export/csv/(\d+)", path)
        if m and method == "GET":
            res, err = api_export_csv(int(m.group(1)))
            if err:
                return json_response(start_response, err, "404 Not Found")
            _, text, fname = res
            return text_response(start_response, text, MIME[".csv"], fname)
        m = re.fullmatch(r"/api/export/json/(\d+)", path)
        if m and method == "GET":
            res, err = api_export_json(int(m.group(1)))
            if err:
                return json_response(start_response, err, "404 Not Found")
            _, text, fname = res
            return text_response(start_response, text,
                                 MIME[".json"], fname)
        m = re.fullmatch(r"/api/report/(\d+)", path)
        if m and method == "GET":
            res, err = api_report(int(m.group(1)))
            if err:
                return json_response(start_response, err, "404 Not Found")
            _, text, fname = res
            return text_response(start_response, text,
                                 "text/html; charset=utf-8", fname)
        m = re.fullmatch(r"/api/batch/export/csv/(\d+)", path)
        if m and method == "GET":
            res, err = api_batch_export_csv(int(m.group(1)))
            if err:
                return json_response(start_response, err, "404 Not Found")
            _, text, fname = res
            return text_response(start_response, text, MIME[".csv"], fname)
        m = re.fullmatch(r"/api/batch/export/json/(\d+)", path)
        if m and method == "GET":
            res, err = api_batch_export_json(int(m.group(1)))
            if err:
                return json_response(start_response, err, "404 Not Found")
            _, text, fname = res
            return text_response(start_response, text,
                                 MIME[".json"], fname)
        m = re.fullmatch(r"/api/batch/report/(\d+)", path)
        if m and method == "GET":
            res, err = api_batch_report(int(m.group(1)))
            if err:
                return json_response(start_response, err, "404 Not Found")
            _, text, fname = res
            return text_response(start_response, text,
                                 "text/html; charset=utf-8", fname)
        m = re.fullmatch(r"/api/batch/versions/(\d+)", path)
        if m and method == "GET":
            return json_response(start_response,
                                 api_batch_versions(int(m.group(1))))
        m = re.fullmatch(r"/api/batch/version/(\d+)", path)
        if m and method == "GET":
            out, err = api_batch_version(int(m.group(1)))
            return json_response(start_response, err or out,
                                 "404 Not Found" if err else "200 OK")
        m = re.fullmatch(r"/api/versions/(\d+)", path)
        if m and method == "GET":
            return json_response(start_response, api_versions(int(m.group(1))))

        if path == "/api/upload" and method == "POST":
            _, out, status = api_upload(environ, qs)
            return json_response(start_response, out, status)
        if path == "/api/datasets" and method == "GET":
            return json_response(start_response, api_datasets(qs))
        if path == "/api/analyze" and method == "POST":
            out, err = api_analyze(environ)
            return json_response(start_response, err or out,
                                 "400 Bad Request" if err else "200 OK")
        if path == "/api/batch/analyze" and method == "POST":
            out, err = api_batch_analyze(environ)
            return json_response(start_response, err or out,
                                 "400 Bad Request" if err else "200 OK")
        if path == "/api/batch/undo" and method == "POST":
            return json_response(start_response, api_batch_undo(environ))
        if path == "/api/undo" and method == "POST":
            return json_response(start_response, api_undo(environ))
        if path == "/api/exclude" and method == "POST":
            out, err = api_exclude(environ), None
            if isinstance(out, tuple):
                _, out, status = out
                return json_response(start_response, out, status)
            return json_response(start_response, out)
        if path == "/api/groups" and method == "GET":
            return json_response(start_response, api_groups(qs))
        m = re.fullmatch(r"/api/dataset/(\d+)/delete", path)
        if m and method == "POST":
            conn = get_conn()
            db.delete_dataset(conn, int(m.group(1)))
            conn.close()
            return json_response(start_response, {"ok": True})

        return json_response(start_response, {"ok": False, "error": "未找到"},
                             "404 Not Found")
    except Exception as exc:  # noqa: BLE001 - 统一返回 JSON 错误
        return json_response(start_response,
                             {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             "500 Internal Server Error")


def serve_static(start_response, rel):
    rel = rel or "index.html"
    full = os.path.normpath(os.path.join(STATIC, rel))
    if not full.startswith(STATIC) or not os.path.isfile(full):
        return json_response(start_response, {"ok": False, "error": "未找到"},
                             "404 Not Found")
    ext = os.path.splitext(full)[1]
    with open(full, "rb") as fh:
        body = fh.read()
    start_response("200 OK", [("Content-Type", MIME.get(ext, "text/plain")),
                              ("Content-Length", str(len(body)))])
    return [body]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="间歇式呼吸室分析工作台")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"服务已启动: http://{args.host}:{args.port}")
    with make_server(args.host, args.port, application) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
