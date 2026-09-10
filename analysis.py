"""间歇式呼吸室核心分析：漂移修正、线性回归、温压体积换算、告警。

纯函数实现，不依赖第三方库，便于测试与复用。
"""

from __future__ import annotations

import math
from datetime import datetime


# ---------------------------------------------------------------- CSV 解析

def parse_time(value):
    """接受 Unix 秒、HH:MM:SS 或 ISO 时间戳，统一返回秒（float）。"""
    value = value.strip()
    if not value:
        raise ValueError("空时间字段")
    try:
        return float(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if fmt == "%H:%M:%S":
                return dt.hour * 3600 + dt.minute * 60 + dt.second
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {value!r}")


def parse_csv(text):
    """解析 CSV 文本，返回 (points, errors)。

    必需列: time, o2；可选列: temp, pressure, volume, event。
    points 元素: {t, o2, temp, press, vol, event}
    """
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        raise ValueError("CSV 为空")
    header = [h.strip().lower() for h in lines[0].split(",")]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_t = col("time", "t", "时间")
    i_o2 = col("o2", "oxygen", "do", "氧浓度")
    if i_t is None or i_o2 is None:
        raise ValueError("CSV 必须包含 time 与 o2 列")
    i_temp = col("temp", "temperature", "温度")
    i_press = col("pressure", "press", "气压")
    i_vol = col("volume", "vol", "体积")
    i_evt = col("event", "mark", "事件")

    points, errors = [], []
    for ln_no, ln in enumerate(lines[1:], start=2):
        cells = ln.split(",")
        try:
            t = parse_time(cells[i_t])
            o2 = float(cells[i_o2])
        except (ValueError, IndexError) as exc:
            errors.append(f"第{ln_no}行: {exc}")
            continue

        def opt(idx, default=None):
            if idx is None or idx >= len(cells) or cells[idx].strip() == "":
                return default
            try:
                return float(cells[idx])
            except ValueError:
                return default

        points.append({
            "t": t,
            "o2": o2,
            "temp": opt(i_temp),
            "press": opt(i_press),
            "vol": opt(i_vol),
            "event": (cells[i_evt].strip() if i_evt is not None
                      and i_evt < len(cells) else ""),
        })
    return points, errors


# ---------------------------------------------------------------- 物性换算

def o2_saturation(temp_c, press_kpa):
    """淡水氧溶解度 (mg/L)，Benson & Krause 经验式近似 + 气压修正。"""
    t = temp_c
    cs = 14.652 - 0.41022 * t + 0.007991 * t ** 2 - 0.000077774 * t ** 3
    return cs * (press_kpa / 101.325)


def stp_factor(temp_c, press_kpa):
    """浓度换算到标准状况 (0°C, 101.325 kPa) 的倍率。"""
    return (273.15 / (273.15 + temp_c)) * (press_kpa / 101.325)


# ---------------------------------------------------------------- 漂移修正

def drift_correct(points, calib_points):
    """分段线性零点漂移修正。

    calib_points: [(t, expected_o2), ...] 校准时刻与期望浓度。
    每个校准点的偏移 = 实测 - 期望；相邻校准点间线性插值，
    端点之外取最近端点偏移。返回新点列（附加 o2_corr 与 offset）。
    """
    if not calib_points:
        for p in points:
            p["o2_corr"] = p["o2"]
            p["offset"] = 0.0
        return points

    calib = sorted(calib_points, key=lambda c: c[0])
    raw_at = []
    for ct, _ in calib:
        # 取时间上最近的实测点作为校准点实测值
        nearest = min(points, key=lambda p: abs(p["t"] - ct))
        raw_at.append(nearest["o2"])
    offsets = [raw - exp for (ct, exp), raw in zip(calib, raw_at)]

    def offset_at(t):
        if t <= calib[0][0]:
            return offsets[0]
        if t >= calib[-1][0]:
            return offsets[-1]
        for i in range(len(calib) - 1):
            t0, t1 = calib[i][0], calib[i + 1][0]
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return offsets[i] + frac * (offsets[i + 1] - offsets[i])
        return offsets[-1]

    for p in points:
        off = offset_at(p["t"])
        p["offset"] = off
        p["o2_corr"] = p["o2"] - off
    return points


# ---------------------------------------------------------------- 回归与速率

def linregress(xs, ys):
    """最小二乘线性回归，返回 slope/intercept/r2/residuals。"""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        raise ValueError("窗口内时间无变化，无法回归")
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    mean_y = sy / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    return slope, intercept, r2, residuals


def analyze_window(corrected, window, blank_rate=None, blank_volume=None,
                   r2_threshold=0.9, min_points=5):
    """在已修正曲线上分析一个测量窗口，返回结果字典（含 warnings 列表）。

    window: (t0, t1)；blank_rate: 空白室耗氧率 mg/h（已换算）；
    blank_volume: 空白室体积 L。
    """
    warnings = []

    t0, t1 = window
    if t1 < t0:
        t0, t1 = t1, t0
        warnings.append({"code": "WINDOW_REVERSED",
                         "msg": "窗口起止时间倒序，已自动交换"})

    in_win = [p for p in corrected if t0 <= p["t"] <= t1]

    # 时间倒序检查（窗内）
    ts = [p["t"] for p in in_win]
    if any(b < a for a, b in zip(ts, ts[1:])):
        warnings.append({"code": "TIME_REVERSED",
                         "msg": "窗口内存在时间倒序数据点，请检查原始记录"})

    # 事件检查
    evts = [(p["t"], p["event"]) for p in in_win if p.get("event")]
    if evts:
        desc = "、".join(f"{e}@{t:.0f}s" for t, e in evts)
        warnings.append({"code": "EVENT_IN_WINDOW",
                         "msg": f"窗口跨越事件: {desc}，建议避开"})

    n = len(in_win)
    if n < min_points:
        warnings.append({"code": "TOO_FEW_POINTS",
                         "msg": f"窗口内有效点 {n} 个，少于下限 {min_points} 个"})

    result = {
        "window": [t0, t1],
        "n": n,
        "warnings": warnings,
    }
    if n < 2:
        result["error"] = "窗口内点数不足，无法回归"
        return result

    xs = [p["t"] for p in in_win]
    ys = [p["o2_corr"] for p in in_win]
    slope, intercept, r2, residuals = linregress(xs, ys)

    if r2 < r2_threshold:
        warnings.append({"code": "LOW_R2",
                         "msg": f"R²={r2:.4f} 低于阈值 {r2_threshold}"})

    # 温压体积换算
    temps = [p["temp"] for p in in_win if p["temp"] is not None]
    press = [p["press"] for p in in_win if p["press"] is not None]
    vols = [p["vol"] for p in in_win if p["vol"] is not None]
    mean_temp = sum(temps) / len(temps) if temps else 25.0
    mean_press = sum(press) / len(press) if press else 101.325
    volume = vols[0] if vols else 1.0

    factor = stp_factor(mean_temp, mean_press)
    slope_std = slope * factor                      # mg/L/s → 标准状况
    mo2_raw = -slope_std * volume * 3600.0          # mg/h，耗氧为正

    mo2_net = mo2_raw
    blank_used = None
    if blank_rate is not None:
        bv = blank_volume or volume
        scaled = blank_rate * (volume / bv) if bv else blank_rate
        blank_used = {"rate": blank_rate, "volume": bv, "scaled": scaled}
        mo2_net = mo2_raw - scaled
        if mo2_raw != 0 and (mo2_net < 0) != (mo2_raw < 0):
            warnings.append({
                "code": "SIGN_FLIP",
                "msg": (f"空白修正后符号反转（修正前 {mo2_raw:.4f} → "
                        f"修正后 {mo2_net:.4f} mg/h），"
                        "空白速率可能过大或信号过弱"),
            })

    result.update({
        "slope": slope, "intercept": intercept, "r2": r2,
        "residuals": residuals,
        "window_points": in_win,
        "mean_temp": mean_temp, "mean_press": mean_press, "volume": volume,
        "stp_factor": factor,
        "slope_std": slope_std,
        "mo2_raw": mo2_raw, "mo2_net": mo2_net,
        "blank": blank_used,
        "o2_sat": o2_saturation(mean_temp, mean_press),
    })
    return result


def analyze(points, window, calib_points=None, blank_rate=None,
            blank_volume=None, r2_threshold=0.9, min_points=5):
    """完整单窗分析流程：漂移修正 + 窗口分析。返回结果字典（含 warnings）。"""
    calib_points = calib_points or []
    corrected = drift_correct([dict(p) for p in points], calib_points)
    result = analyze_window(corrected, window, blank_rate=blank_rate,
                            blank_volume=blank_volume,
                            r2_threshold=r2_threshold, min_points=min_points)
    result["corrected"] = corrected
    result["calib_points"] = calib_points
    return result


# ---------------------------------------------------------------- 自动分段

def _o2_of(p):
    return p.get("o2_corr", p["o2"])


def _rise_bounds(points, threshold, min_duration):
    """氧浓度回升（冲洗）区段的末端时刻列表。

    相邻点升高速率超过 threshold (mg/L/s) 视为上升；连续上升区段
    持续时间短于 min_duration (s) 的视为噪声忽略。
    """
    pts = sorted(points, key=lambda p: p["t"])
    n = len(pts)
    rising = []
    for i in range(n - 1):
        dt = pts[i + 1]["t"] - pts[i]["t"]
        rising.append(dt > 0 and
                      (_o2_of(pts[i + 1]) - _o2_of(pts[i])) / dt > threshold)
    bounds = []
    i = 0
    while i < n - 1:
        if not rising[i]:
            i += 1
            continue
        j = i
        while j + 1 < n - 1 and rising[j + 1]:
            j += 1
        if pts[j + 1]["t"] - pts[i]["t"] >= min_duration:
            bounds.append(pts[j + 1]["t"])
        i = j + 1
    return bounds


def segment(points, seg):
    """按分段规则生成候选测量周期 [{start, end}, ...]。

    seg 字段：
      method            "event"（flush 事件）或 "rise"（氧浓度回升阈值）
      event_keyword     事件关键字（默认 flush，不区分大小写）
      rise_threshold    回升速率阈值 mg/L/s（method=rise）
      rise_min_duration 回升区段最短持续 s（method=rise）
      flush_offset      边界后跳过 s（冲洗/换水平稳时间）
      end_margin        下一边界前预留 s
      min_duration      首末段短于该值则舍弃（记录可能未覆盖完整周期）
    """
    if not points:
        return []
    offset = float(seg.get("flush_offset", 60.0))
    margin = float(seg.get("end_margin", 5.0))
    min_dur = float(seg.get("min_duration", 60.0))
    ts = [p["t"] for p in points]
    t0, t1 = min(ts), max(ts)
    if seg.get("method") == "rise":
        bounds = _rise_bounds(points,
                              float(seg.get("rise_threshold", 0.01)),
                              float(seg.get("rise_min_duration", 0.0)))
    else:
        kw = (seg.get("event_keyword") or "flush").strip().lower()
        bounds = sorted(p["t"] for p in points
                        if p.get("event") and kw in p["event"].lower())
    bounds = [b for b in bounds if t0 < b < t1]
    if not bounds:
        return []
    starts = [t0] + bounds
    out = []
    for i, s in enumerate(starts):
        a = s + offset
        e = (bounds[i] - margin) if i < len(bounds) else t1
        if e <= a:
            continue
        if i in (0, len(starts) - 1) and (e - a) < min_dur:
            continue
        out.append({"start": a, "end": e})
    return out


def merge_locked_cycles(existing, candidates):
    """锁定周期原样保留；候选周期只补入未与锁定周期重叠的区域。"""
    locked = [c for c in existing if c.get("locked")]

    def blocked(c):
        return any(float(c["start"]) < float(L["end"]) and
                   float(L["start"]) < float(c["end"]) for L in locked)

    merged = [dict(L) for L in locked]
    for c in candidates:
        if not blocked(c):
            merged.append({"start": float(c["start"]), "end": float(c["end"]),
                           "locked": False, "decision": None, "reason": ""})
    merged.sort(key=lambda c: (float(c["start"]), float(c["end"])))
    return merged


# ---------------------------------------------------------------- 批量分析

# 出现任一即判定周期“自动无效”的告警（用户可裁决保留）
BLOCKING_CODES = ("CYCLE_OVERLAP", "CYCLE_TOO_SHORT", "TOO_FEW_POINTS",
                  "LOW_R2", "EVENT_IN_WINDOW", "TIME_REVERSED", "SIGN_FLIP",
                  "RATE_OUTLIER")


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def analyze_cycles(corrected, cycles, blank_rate=None, blank_volume=None,
                   r2_threshold=0.9, min_points=5, min_duration=60.0,
                   mad_threshold=3.5):
    """批量分析一组周期边界。

    cycles: [{start, end, locked, decision, reason}, ...]
      decision: None=自动 / "keep"=保留 / "exclude"=剔除（reason 为理由）。
    返回 {"cycles": [...含结果与告警...], "summary": {...}}。
    """
    cycles = sorted(cycles,
                    key=lambda c: (float(c["start"]), float(c["end"])))
    # 重叠检测（相邻周期）
    overlap = [False] * len(cycles)
    for i in range(len(cycles) - 1):
        if float(cycles[i]["end"]) > float(cycles[i + 1]["start"]) + 1e-9:
            overlap[i] = overlap[i + 1] = True

    out = []
    for i, c in enumerate(cycles):
        start, end = float(c["start"]), float(c["end"])
        res = analyze_window(corrected, (start, end), blank_rate=blank_rate,
                             blank_volume=blank_volume,
                             r2_threshold=r2_threshold, min_points=min_points)
        warnings = list(res["warnings"])
        if overlap[i]:
            warnings.append({"code": "CYCLE_OVERLAP",
                             "msg": "与相邻周期时间重叠，请拖动边界消除"})
        dur = abs(end - start)
        if dur < min_duration:
            warnings.append({"code": "CYCLE_TOO_SHORT",
                             "msg": f"测量段 {dur:.0f}s 短于下限 {min_duration:.0f}s"})
        decision = c.get("decision")
        out.append({
            "id": i + 1,
            "start": res["window"][0], "end": res["window"][1],
            "locked": bool(c.get("locked")),
            "decision": decision if decision in ("keep", "exclude") else None,
            "reason": c.get("reason", "") or "",
            "result": res,
            "warnings": warnings,
            "outlier_z": None,
        })

    # MAD 离群识别：在其余质量合格的周期中检验速率
    pool = [c for c in out if c["result"].get("mo2_net") is not None
            and not any(w["code"] in BLOCKING_CODES for w in c["warnings"])]
    if len(pool) >= 4:
        rates = [c["result"]["mo2_net"] for c in pool]
        med = _median(rates)
        mad = _median([abs(r - med) for r in rates])
        if mad and mad > 0:
            for c in pool:
                z = 0.6745 * (c["result"]["mo2_net"] - med) / mad
                c["outlier_z"] = z
                if abs(z) > mad_threshold:
                    c["warnings"].append({
                        "code": "RATE_OUTLIER",
                        "msg": (f"速率离群：修正 z={z:.2f}，|z|>{mad_threshold}"
                                "（基于中位数绝对偏差），请裁决保留或剔除")})

    # 计入汇总标记
    for c in out:
        blocked = any(w["code"] in BLOCKING_CODES for w in c["warnings"])
        c["auto_valid"] = c["result"].get("mo2_net") is not None and not blocked
        if c["decision"] == "keep":
            c["included"] = c["result"].get("mo2_net") is not None
        elif c["decision"] == "exclude":
            c["included"] = False
        else:
            c["included"] = c["auto_valid"]

    return {"cycles": out, "summary": summarize_cycles(out)}


def summarize_cycles(cycles):
    """有效（计入）周期 MO₂ 的均值、标准差、变异系数等。"""
    inc = [c for c in cycles if c.get("included")
           and c["result"].get("mo2_net") is not None]
    rates = [c["result"]["mo2_net"] for c in inc]
    vols = [c["result"]["volume"] for c in inc
            if c["result"].get("volume") is not None]
    n = len(rates)
    summary = {"n_total": len(cycles), "n_included": n,
               "n_excluded": len(cycles) - n,
               "mean": None, "std": None, "cv": None, "median": None,
               "volume": sum(vols) / len(vols) if vols else None}
    if n:
        mean = sum(rates) / n
        summary["mean"] = mean
        summary["median"] = _median(rates)
        if n > 1:
            var = sum((r - mean) ** 2 for r in rates) / (n - 1)
            summary["std"] = math.sqrt(var)
            if mean:
                summary["cv"] = summary["std"] / mean * 100.0
    return summary
