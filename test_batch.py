"""多周期批量分析核心逻辑测试。

运行: python3 test_batch.py
"""

import os
import tempfile
import unittest

import analysis
import db


def make_run(rates, cycle=600, flush=60, dt=10, sat=8.6, vol=1.0,
             temp=22.0, press=101.3):
    """合成间歇式记录：每周期 flush 秒冲洗（饱和）+ 线性耗氧测量段。"""
    pts = []
    n = len(rates)
    for t in range(0, n * cycle + 1, dt):
        ic = t % cycle
        cyc = min(t // cycle, n - 1)
        if ic < flush:
            o2 = sat
        else:
            o2 = sat - rates[cyc] / 3600.0 / vol * (ic - flush)
        pts.append({"t": float(t), "o2": o2, "temp": temp, "press": press,
                    "vol": vol,
                    "event": "flush" if ic == 0 and t > 0 else ""})
    return pts


def make_rise_run():
    """用户报告场景：0–260s 测量下降，260–300s 冲洗回升，300s 后再下降。"""
    pts = []
    for t in range(0, 601, 10):
        if t < 260:
            o2 = 8.6 - 0.0002 * t            # 测量段 1：缓慢下降
        elif t < 300:
            o2 = 8.548 + 0.02 * (t - 260)    # 冲洗回升段（快速上升）
        else:
            o2 = 9.348 - 0.0002 * (t - 300)  # 测量段 2
        pts.append({"t": float(t), "o2": round(o2, 4), "temp": 22.0,
                    "press": 101.3, "vol": 1.0, "event": ""})
    return pts


class SegmentRiseTest(unittest.TestCase):
    def test_rise_region_separated_from_measurement(self):
        """260–300s 回升段不得并入前一测量窗（原 bug：10–300s 窗 LOW_R2）。"""
        pts = make_rise_run()
        segs = analysis.segment(pts, {"method": "rise",
                                      "rise_threshold": 0.001,
                                      "rise_min_duration": 0,
                                      "flush_offset": 10,
                                      "end_margin": 5,
                                      "min_duration": 30})
        self.assertEqual(len(segs), 2)
        # 第一段在回升开始前结束，第二段在回升结束后开始
        self.assertEqual(segs[0], {"start": 10.0, "end": 255.0})
        self.assertEqual(segs[1], {"start": 310.0, "end": 600.0})
        # 回升段 260–300 内的点不属于任何周期
        for s in segs:
            self.assertFalse(s["start"] < 300 and s["end"] > 260)

    def test_rise_windows_pass_r2(self):
        """修正后两段测量窗各自拟合良好，不再误触发 LOW_R2。"""
        pts = make_rise_run()
        segs = analysis.segment(pts, {"method": "rise",
                                      "rise_threshold": 0.001,
                                      "flush_offset": 10,
                                      "end_margin": 5,
                                      "min_duration": 30})
        corrected = analysis.drift_correct([dict(p) for p in pts], [])
        batch = analysis.analyze_cycles(corrected, segs, r2_threshold=0.9,
                                        min_points=5, min_duration=30)
        self.assertEqual(batch["summary"]["n_included"], 2)
        for c in batch["cycles"]:
            self.assertGreater(c["result"]["r2"], 0.99)
            self.assertFalse(any(w["code"] == "LOW_R2"
                                 for w in c["warnings"]))

    def test_rise_min_duration_filters_spikes(self):
        """短于下限的瞬时尖峰不算冲洗区段。"""
        pts = []
        for t in range(0, 301, 10):
            if t <= 100:
                o2 = 8.6 - 0.002 * t            # 下降
            elif t == 110:
                o2 = 8.7                        # 单区间尖峰（10s）
            elif t <= 200:
                o2 = 8.7 - 0.002 * (t - 110)    # 下降
            elif t <= 240:
                o2 = 8.52 + 0.0125 * (t - 200)  # 冲洗回升（40s）
            else:
                o2 = 9.02 - 0.002 * (t - 240)   # 下降
            pts.append({"t": float(t), "o2": round(o2, 4), "temp": 22.0,
                        "press": 101.3, "vol": 1.0, "event": ""})
        regions = analysis._rise_regions(pts, 0.01, 15)
        self.assertEqual(regions, [(200.0, 240.0)])   # 尖峰被滤除


class SegmentEventTest(unittest.TestCase):
    def test_event_segment(self):
        pts = make_run([0.4, 0.4, 0.4])
        segs = analysis.segment(pts, {"method": "event",
                                      "event_keyword": "flush",
                                      "flush_offset": 60,
                                      "end_margin": 5,
                                      "min_duration": 60})
        # 数据末点 t=1800 本身是 flush 事件：末段在其前 end_margin 收尾
        self.assertEqual(segs, [{"start": 60.0, "end": 595.0},
                                {"start": 660.0, "end": 1195.0},
                                {"start": 1260.0, "end": 1795.0}])

    def test_no_events_returns_empty(self):
        pts = make_run([0.4])
        for p in pts:
            p["event"] = ""
        self.assertEqual(analysis.segment(pts, {"method": "event"}), [])


class MergeLockedTest(unittest.TestCase):
    def test_locked_cycles_survive_resegment(self):
        existing = [{"start": 70.0, "end": 590.0, "locked": True,
                     "decision": "keep", "reason": "已人工确认"}]
        candidates = [{"start": 60.0, "end": 595.0},
                      {"start": 660.0, "end": 1195.0}]
        merged = analysis.merge_locked_cycles(existing, candidates)
        self.assertEqual(len(merged), 2)
        locked = [c for c in merged if c["locked"]]
        self.assertEqual(len(locked), 1)
        # 锁定边界不被规则结果覆盖
        self.assertEqual((locked[0]["start"], locked[0]["end"]), (70.0, 590.0))
        self.assertEqual(locked[0]["decision"], "keep")
        # 与锁定周期重叠的候选被丢弃，未重叠的补入
        self.assertIn({"start": 660.0, "end": 1195.0, "locked": False,
                       "decision": None, "reason": ""}, merged)


class AnalyzeCyclesTest(unittest.TestCase):
    def test_summary_stats(self):
        pts = make_run([0.3, 0.3, 0.3])
        corrected = analysis.drift_correct([dict(p) for p in pts], [])
        segs = analysis.segment(corrected, {"method": "event",
                                            "flush_offset": 60,
                                            "end_margin": 5,
                                            "min_duration": 60})
        batch = analysis.analyze_cycles(corrected, segs, min_duration=60)
        s = batch["summary"]
        self.assertEqual(s["n_included"], 3)
        expect = 0.3 * analysis.stp_factor(22.0, 101.3)
        self.assertAlmostEqual(s["mean"], expect, places=4)
        self.assertAlmostEqual(s["std"], 0.0, places=4)
        self.assertAlmostEqual(s["cv"], 0.0, places=2)

    def test_overlap_and_short_warnings(self):
        pts = make_run([0.3])
        corrected = analysis.drift_correct([dict(p) for p in pts], [])
        cycles = [{"start": 60, "end": 300}, {"start": 250, "end": 300}]
        batch = analysis.analyze_cycles(corrected, cycles, min_duration=60)
        w0 = [w["code"] for w in batch["cycles"][0]["warnings"]]
        w1 = [w["code"] for w in batch["cycles"][1]["warnings"]]
        self.assertIn("CYCLE_OVERLAP", w0)
        self.assertIn("CYCLE_OVERLAP", w1)
        self.assertIn("CYCLE_TOO_SHORT", w1)   # 第二周期仅 50s < 60s 下限
        self.assertEqual(batch["summary"]["n_included"], 0)

    def test_event_in_window_blocks(self):
        pts = make_run([0.3, 0.3])
        for p in pts:
            if p["t"] == 700:
                p["event"] = "lid_open"
        corrected = analysis.drift_correct([dict(p) for p in pts], [])
        segs = analysis.segment(corrected, {"method": "event",
                                            "flush_offset": 60,
                                            "end_margin": 5,
                                            "min_duration": 60})
        batch = analysis.analyze_cycles(corrected, segs, min_duration=60)
        c2 = batch["cycles"][1]
        self.assertIn("EVENT_IN_WINDOW", [w["code"] for w in c2["warnings"]])
        self.assertFalse(c2["included"])
        self.assertEqual(batch["summary"]["n_included"], 1)

    def test_mad_outlier_and_decisions(self):
        """5 周期中 1 个高速率周期被 MAD 判离群；裁决可覆盖自动结论。"""
        pts = make_run([0.30, 0.30, 0.30, 0.90, 0.30])
        corrected = analysis.drift_correct([dict(p) for p in pts], [])
        segs = analysis.segment(corrected, {"method": "event",
                                            "flush_offset": 60,
                                            "end_margin": 5,
                                            "min_duration": 60})
        batch = analysis.analyze_cycles(corrected, segs, min_duration=60)
        cycles = batch["cycles"]
        flagged = [c for c in cycles if any(w["code"] == "RATE_OUTLIER"
                                            for w in c["warnings"])]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["id"], 4)
        self.assertFalse(flagged[0]["included"])
        self.assertEqual(batch["summary"]["n_included"], 4)
        # 裁决保留 → 计入；裁决剔除 → 不计入
        segs[3]["decision"] = "keep"
        segs[3]["reason"] = "检查为鱼活动所致，数据有效"
        segs[0]["decision"] = "exclude"
        segs[0]["reason"] = "探头气泡"
        batch2 = analysis.analyze_cycles(corrected, segs, min_duration=60)
        inc = [c["id"] for c in batch2["cycles"] if c["included"]]
        self.assertEqual(inc, [2, 3, 4, 5])
        self.assertEqual(batch2["cycles"][3]["reason"], "检查为鱼活动所致，数据有效")


class DbBatchTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["RESPIRO_DB"] = self.path
        db.DB_PATH = self.path

    def tearDown(self):
        os.unlink(self.path)

    def test_version_roundtrip_and_undo(self):
        conn = db.connect()
        ds = db.create_dataset(conn, "t", "s", False, "f.csv",
                               [{"t": 0, "o2": 8.5}, {"t": 10, "o2": 8.4}])
        params = {"seg": {"method": "event"}, "min_duration": 60}
        cycles = [{"id": 1, "start": 0, "end": 10, "included": True,
                   "result": {"mo2_net": 0.3}, "warnings": []}]
        summary = {"n_included": 1, "mean": 0.3}
        db.save_batch_version(conn, ds, params, cycles, summary, note="v1")
        db.save_batch_version(conn, ds, params, cycles, summary, note="v2")
        self.assertEqual(len(db.list_batch_versions(conn, ds)), 2)
        ok, msg, cur = db.undo_batch_version(conn, ds)
        self.assertTrue(ok)
        self.assertIsNotNone(cur)
        self.assertEqual(len(db.list_batch_versions(conn, ds)), 1)
        ok, msg, cur = db.undo_batch_version(conn, ds)
        self.assertTrue(ok)
        self.assertIsNone(cur)          # 可撤销到空
        ok, msg, cur = db.undo_batch_version(conn, ds)
        self.assertFalse(ok)
        conn.close()


if __name__ == "__main__":
    unittest.main()
