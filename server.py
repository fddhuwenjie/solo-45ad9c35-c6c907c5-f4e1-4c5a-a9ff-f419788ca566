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
    conn.close()
    return {"ok": True, "dataset": meta, "points": points,
            "versions": versions}, None


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
        # 空白室速率：取其最新一次分析版本
        versions = db.list_versions(conn, blank_id)
        if not versions:
            conn.close()
            return None, {"ok": False,
                          "error": "空白室尚未分析，请先在空白室数据上运行一次分析"}, \
                "400 Bad Request"
        blank_rate = versions[-1]["result"].get("mo2_net")
        blank_volume = versions[-1]["result"].get("volume")

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
