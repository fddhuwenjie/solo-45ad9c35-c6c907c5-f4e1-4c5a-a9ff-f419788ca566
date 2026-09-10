#!/usr/bin/env python3
"""生成合成间歇式呼吸室数据：多个"冲洗-测量"周期，含零点漂移、
开盖扰动、气泡、传感器断点、搅拌未稳、时间倒序等典型问题。

结构：每周期 600s = 冲洗段（0–60s，氧浓度回到饱和附近）
                + 测量段（60–600s，线性耗氧下降）。
探头漂移在整个运行中累积，冲洗时刻读数 = 饱和值 + 漂移量，
因此可用各周期起点作校准点做分段漂移修正。

输出到本目录：
  fish_A_run1.csv   样本A第1次：线性漂移 + 开盖 + 气泡 + 传感器断点
  fish_A_run2.csv   样本A第2次：两段速率非线性漂移 + 搅拌未稳
  fish_B_run1.csv   样本B：温和漂移 + 时间倒序坏点
  blank_chamber.csv 空白室：仅微生物耗氧 + 漂移
"""

import csv
import math
import os
import random

random.seed(42)
HERE = os.path.dirname(os.path.abspath(__file__))

CYCLE = 600          # 周期长度 s
FLUSH = 60           # 冲洗段长度 s
DT = 5               # 采样间隔 s
N_CYCLES = 3


def o2_sat(temp, press=101.3):
    return (14.652 - 0.41022 * temp + 0.007991 * temp ** 2
            - 0.000077774 * temp ** 3) * (press / 101.325)


def write_csv(name, rows):
    path = os.path.join(HERE, name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "o2", "temp", "pressure", "volume", "event"])
        w.writerows(rows)
    print(f"已生成 {path}（{len(rows)} 行）")


def true_o2(t, temp, press, vol, mo2_mg_h):
    """真实氧浓度：每个周期冲洗回饱和，测量段线性下降。"""
    sat = o2_sat(temp, press) * 0.985
    in_cycle = t % CYCLE
    if in_cycle < FLUSH:                       # 冲洗段：向饱和指数回升
        return sat
    slope = -mo2_mg_h / 3600.0 / vol           # mg/L/s
    return sat + slope * (in_cycle - FLUSH)


def drift_linear(t):
    return 0.00010 * t


def drift_two_rate(t):
    return 0.00022 * t if t < 900 else 0.00022 * 900 + 0.00005 * (t - 900)


def drift_mild(t):
    return 0.00006 * t


def make_rows(mo2_mg_h, vol, temp, press, drift_fn, disturbances=None):
    """disturbances: 函数 (t, value, in_cycle) -> (value, event)"""
    rows = []
    duration = N_CYCLES * CYCLE
    for t in range(0, duration + 1, DT):
        in_cycle = t % CYCLE
        tt = temp + 0.1 * math.sin(t / 700)
        pp = press + 0.15 * math.sin(t / 900)
        v = true_o2(t, tt, pp, vol, mo2_mg_h) + drift_fn(t)
        v += random.gauss(0, 0.004)
        event = "flush" if in_cycle == 0 and t > 0 else ""
        if disturbances:
            v, event = disturbances(t, v, in_cycle, tt, pp) or (v, event)
        rows.append([t, round(v, 4), round(tt, 2), round(pp, 2), vol, event])
    return rows


def fish_a_run1():
    """第2周期开盖复氧(900–960s)、气泡尖峰(750s)、
    第3周期传感器断点(1300–1360s 读数卡死)。"""
    def disturb(t, v, ic, temp, press):
        if 900 <= t <= 960:                    # 开盖换样：向饱和回弹
            return v + (o2_sat(temp, press) - v) * 0.75, \
                ("lid_open" if t == 920 else "")
        if t == 750:                           # 气泡附着尖峰
            return v + 0.35, "bubble"
        if 1300 <= t <= 1360:                  # 传感器断点：读数卡死
            return 7.321, ("sensor_break" if t == 1300 else "")
        return v, ""
    rows = make_rows(0.42, 0.95, 22.0, 101.3, drift_linear, disturb)
    write_csv("fish_A_run1.csv", rows)


def fish_a_run2():
    """两段速率漂移 + 首周期前 240s 搅拌未稳振荡。"""
    def disturb(t, v, ic, temp, press):
        if t < 240 and ic >= FLUSH:
            return v + 0.06 * math.sin(t / 18), \
                ("stirring" if t == FLUSH else "")
        return v, ""
    rows = make_rows(0.40, 0.95, 22.5, 101.0, drift_two_rate, disturb)
    write_csv("fish_A_run2.csv", rows)


def fish_b_run1():
    """温和漂移 + t=900 处时间倒序坏点。"""
    rows = make_rows(0.28, 0.80, 23.0, 100.8, drift_mild)
    for r in rows:
        if r[0] == 900:
            r[0] = 860                         # 时间写反
    write_csv("fish_B_run1.csv", rows)


def blank_chamber():
    """空白室：仅微生物耗氧，速率低，带漂移。"""
    rows = make_rows(0.03, 0.95, 22.2, 101.2, drift_linear)
    write_csv("blank_chamber.csv", rows)


if __name__ == "__main__":
    fish_a_run1()
    fish_a_run2()
    fish_b_run1()
    blank_chamber()
