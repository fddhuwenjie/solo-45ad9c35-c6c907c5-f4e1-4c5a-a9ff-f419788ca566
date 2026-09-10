"""时变空白校正（背景序列）核心逻辑测试。

运行: python3 test_blank_series.py
"""

import io
import json
import os
import tempfile
import unittest

import analysis
import db
import server


def make_run(rates, cycle=600, flush=60, dt=10, sat=8.6, vol=1.0,
             temp=22.0, press=101.3, t0=0):
    """合成间歇式记录：每周期 flush 秒冲洗（饱和）+ 线性耗氧测量段。"""
    pts = []
    n = len(rates)
    for t in range(t0, t0 + n * cycle + 1, dt):
        ic = (t - t0) % cycle
        cyc = min((t - t0) // cycle, n - 1)
        if ic < flush:
            o2 = sat
        else:
            o2 = sat - rates[cyc] / 3600.0 / vol * (ic - flush)
        pts.append({"t": float(t), "o2": o2, "temp": temp, "press": press,
                    "vol": vol,
                    "event": "flush" if ic == 0 and t > t0 else ""})
    return pts


def seg_of(pts):
    corrected = analysis.drift_correct([dict(p) for p in pts], [])
    segs = analysis.segment(corrected, {"method": "event", "flush_offset": 60,
                                        "end_margin": 5, "min_duration": 60})
    return corrected, segs


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.anchors = [
            {"id": 1, "t": 1000.0, "rate": 0.030, "volume": 1.0, "rev": 1},
            {"id": 2, "t": 3000.0, "rate": 0.050, "volume": 1.0, "rev": 1},
        ]

    def test_linear_interp_weights(self):
        r = analysis.make_blank_resolver(self.anchors, "linear", 7200)
        rate, vol, src, warns = r(2000.0)
        self.assertAlmostEqual(rate, 0.040)
        self.assertAlmostEqual(vol, 1.0)
        self.assertEqual(src["mode"], "linear")
        self.assertEqual([a["id"] for a in src["anchors"]], [1, 2])
        self.assertEqual([a["weight"] for a in src["anchors"]], [0.5, 0.5])
        self.assertEqual(src["gap_s"], 2000.0)
        self.assertEqual(warns, [])

    def test_nearest(self):
        r = analysis.make_blank_resolver(self.anchors, "nearest", 7200)
        rate, _, src, _ = r(1600.0)
        self.assertAlmostEqual(rate, 0.030)
        self.assertEqual(src["mode"], "nearest")
        self.assertEqual(src["anchors"][0]["id"], 1)
        rate2, _, src2, _ = r(2400.0)
        self.assertAlmostEqual(rate2, 0.050)
        self.assertEqual(src2["anchors"][0]["id"], 2)

    def test_extrapolate_and_negative_clamp(self):
        r = analysis.make_blank_resolver(self.anchors, "linear", 7200)
        rate, _, _, warns = r(4000.0)
        self.assertAlmostEqual(rate, 0.060)          # 线性外推
        self.assertIn("BLANK_EXTRAPOLATE", [w["code"] for w in warns])
        # 下降趋势外推为负 → 截断为 0
        anchors = [{"id": 1, "t": 1000.0, "rate": 0.03, "volume": 1.0},
                   {"id": 2, "t": 3000.0, "rate": 0.0, "volume": 1.0}]
        r2 = analysis.make_blank_resolver(anchors, "linear", None)
        rate2, _, _, warns2 = r2(5000.0)
        self.assertEqual(rate2, 0.0)
        self.assertIn("BLANK_EXTRAPOLATE", [w["code"] for w in warns2])

    def test_gap_too_large(self):
        r = analysis.make_blank_resolver(self.anchors, "linear", 1500)
        _, _, _, warns = r(2000.0)
        self.assertIn("BLANK_GAP_TOO_LARGE", [w["code"] for w in warns])
        rn = analysis.make_blank_resolver(self.anchors, "nearest", 500)
        _, _, _, warns_n = rn(1600.0)
        self.assertIn("BLANK_GAP_TOO_LARGE", [w["code"] for w in warns_n])

    def test_single_anchor_and_no_anchor(self):
        r1 = analysis.make_blank_resolver([self.anchors[0]], "linear", None)
        rate, _, src, warns = r1(500.0)
        self.assertAlmostEqual(rate, 0.030)
        self.assertEqual(src["mode"], "single")
        self.assertIn("BLANK_NO_BRACKET", [w["code"] for w in warns])
        # 最近锚点方式下单锚点不告警
        r1n = analysis.make_blank_resolver([self.anchors[0]], "nearest", None)
        _, _, _, warns_n = r1n(500.0)
        self.assertEqual(warns_n, [])
        r0 = analysis.make_blank_resolver([], "linear", None)
        rate0, _, src0, warns0 = r0(500.0)
        self.assertIsNone(rate0)
        self.assertEqual(src0["mode"], "none")
        self.assertIn("BLANK_NO_ANCHOR", [w["code"] for w in warns0])

    def test_rateless_anchor_ignored(self):
        anchors = self.anchors + [{"id": 3, "t": 2000.0, "rate": None}]
        r = analysis.make_blank_resolver(anchors, "linear", None)
        rate, _, src, _ = r(2000.0)
        self.assertAlmostEqual(rate, 0.040)   # 仍按 A1~A2 插值
        self.assertEqual([a["id"] for a in src["anchors"]], [1, 2])


class SeriesCyclesTest(unittest.TestCase):
    def test_per_cycle_rates_aligned_to_windows(self):
        pts = make_run([0.4, 0.4, 0.4])
        corrected, segs = seg_of(pts)
        anchors = [{"id": 1, "t": 0.0, "rate": 0.10, "volume": 1.0},
                   {"id": 2, "t": 1800.0, "rate": 0.20, "volume": 1.0}]
        resolver = analysis.make_blank_resolver(anchors, "linear", None)
        batch = analysis.analyze_cycles(corrected, segs,
                                        blank_resolver=resolver,
                                        min_duration=60)
        rates = [c["result"]["blank"]["rate"] for c in batch["cycles"]]
        self.assertTrue(rates[0] < rates[1] < rates[2])   # 随时间轴增大
        for c in batch["cycles"]:
            r = c["result"]
            self.assertAlmostEqual(r["mo2_net"],
                                   r["mo2_raw"] - r["blank"]["scaled"],
                                   places=9)
            self.assertEqual(r["blank"]["mode"], "linear")
        self.assertEqual(batch["summary"]["n_included"], 3)

    def test_blank_warning_blocks_until_confirmed(self):
        pts = make_run([0.4, 0.4])
        corrected, segs = seg_of(pts)
        # 锚点间隔 100s 超过阈值 60s，且测量窗在区间之外（外推）
        anchors = [{"id": 1, "t": 0.0, "rate": 0.10, "volume": 1.0},
                   {"id": 2, "t": 100.0, "rate": 0.10, "volume": 1.0}]
        resolver = analysis.make_blank_resolver(anchors, "linear", 60)
        batch = analysis.analyze_cycles(corrected, segs,
                                        blank_resolver=resolver,
                                        min_duration=60)
        self.assertEqual(batch["summary"]["n_included"], 0)
        for c in batch["cycles"]:
            codes = [w["code"] for w in c["warnings"]]
            self.assertIn("BLANK_GAP_TOO_LARGE", codes)
            self.assertIn("BLANK_EXTRAPOLATE", codes)
        # 界面确认（裁决保留）后才纳入汇总
        for s in segs:
            s["decision"] = "keep"
            s["reason"] = "确认空白趋势可信"
        batch2 = analysis.analyze_cycles(corrected, segs,
                                         blank_resolver=resolver,
                                         min_duration=60)
        self.assertEqual(batch2["summary"]["n_included"], 2)

    def test_no_anchor_leaves_mo2_uncorrected_and_blocked(self):
        pts = make_run([0.4])
        corrected, segs = seg_of(pts)
        resolver = analysis.make_blank_resolver([], "linear", None)
        batch = analysis.analyze_cycles(corrected, segs,
                                        blank_resolver=resolver,
                                        min_duration=60)
        c = batch["cycles"][0]
        self.assertAlmostEqual(c["result"]["mo2_net"], c["result"]["mo2_raw"])
        self.assertIn("BLANK_NO_ANCHOR", [w["code"] for w in c["warnings"]])
        self.assertFalse(c["included"])


class DbSeriesTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["RESPIRO_DB"] = self.path
        db.DB_PATH = self.path

    def tearDown(self):
        os.unlink(self.path)

    def test_series_crud_and_rev(self):
        conn = db.connect()
        sid = db.create_series(conn, "序列A", "linear", 3600)
        a1 = db.add_anchor(conn, sid, 1, 1000.0)
        a2 = db.add_anchor(conn, sid, 1, 3000.0, locked_rate=0.05)
        s = db.get_series(conn, sid)
        self.assertEqual(s["rev"], 1)
        self.assertEqual(len(s["anchors"]), 2)
        self.assertEqual(s["anchors"][1]["locked_rate"], 0.05)
        db.update_anchor(conn, a1, 1, 1200.0, True, None, "调整时刻")
        s2 = db.get_series(conn, sid)
        self.assertEqual(s2["anchors"][0]["rev"], 2)
        self.assertEqual(s2["anchors"][0]["collected_at"], 1200.0)
        self.assertTrue(s2["anchors"][0]["disabled"])
        self.assertEqual(db.bump_series_rev(conn, sid), 2)
        db.delete_anchor(conn, a2)
        self.assertEqual(len(db.get_series(conn, sid)["anchors"]), 1)
        conn.close()


class SeriesAffectedTest(unittest.TestCase):
    """锚点变化 → 受影响样本自动生成关联新版本，旧版本仍可查阅。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["RESPIRO_DB"] = self.path
        db.DB_PATH = self.path

    def tearDown(self):
        os.unlink(self.path)

    @staticmethod
    def _post(fn, payload):
        body = json.dumps(payload).encode()
        env = {"wsgi.input": io.BytesIO(body),
               "CONTENT_LENGTH": str(len(body))}
        out, err = fn(env)
        assert err is None, err
        return out

    def test_anchor_change_spawns_linked_versions(self):
        conn = db.connect()
        blank_id = db.create_dataset(conn, "blank1", "", True, "b.csv",
                                     make_run([0.03]))
        db.save_batch_version(conn, blank_id, {}, [],
                              {"n_total": 1, "n_included": 1, "mean": 0.03,
                               "volume": 1.0})
        sample_pts = make_run([0.4, 0.4, 0.4])
        sample_id = db.create_dataset(conn, "fishX", "FX", False, "s.csv",
                                      sample_pts)
        other_id = db.create_dataset(conn, "fishY", "FY", False, "s2.csv",
                                     sample_pts)
        # 序列：A1=blank1@0（自动取批量均值 0.03），A2=blank1@1800 锁定 0.06
        out = self._post(server.api_series_save, {
            "name": "背景", "method": "linear", "max_gap_s": 7200,
            "anchors": [
                {"blank_dataset_id": blank_id, "collected_at": 0},
                {"blank_dataset_id": blank_id, "collected_at": 1800,
                 "locked_rate": 0.06},
            ]})
        sid = out["series"]["id"]
        a1, a2 = (a["id"] for a in out["series"]["anchors"])
        self.assertEqual(out["rev"], 1)
        self.assertAlmostEqual(out["series"]["anchors"][0]["rate"], 0.03)
        # 样本批量分析（使用序列）并保存版本
        _, segs = seg_of(sample_pts)
        out2 = self._post(server.api_batch_analyze, {
            "dataset_id": sample_id, "resegment": False, "cycles": segs,
            "blank_series_id": sid, "save": True, "min_duration": 60})
        v1 = out2["version_id"]
        rates1 = [c["result"]["blank"]["rate"] for c in out2["cycles"]]
        self.assertEqual(out2["summary"]["blank"]["series_rev"], 1)
        # 另一样本不使用序列 → 不应受影响
        self._post(server.api_batch_analyze, {
            "dataset_id": other_id, "resegment": False, "cycles": segs,
            "save": True, "min_duration": 60})
        # 调整 A2 采集时刻 1800 → 900：插值结果变化
        out3 = self._post(server.api_series_save, {
            "id": sid, "name": "背景", "method": "linear", "max_gap_s": 7200,
            "anchors": [
                {"id": a1, "blank_dataset_id": blank_id, "collected_at": 0},
                {"id": a2, "blank_dataset_id": blank_id, "collected_at": 900,
                 "locked_rate": 0.06},
            ]})
        self.assertTrue(out3["changed"])
        self.assertEqual(out3["rev"], 2)
        self.assertEqual(len(out3["affected"]), 1)
        aff = out3["affected"][0]
        self.assertEqual(aff["dataset_id"], sample_id)
        self.assertEqual(aff["from_version"], v1)
        # 新版本已生成且旧版本仍可查阅；未用序列的样本不受影响
        versions = db.list_batch_versions(conn, sample_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1]["params"]["auto_from_version"], v1)
        self.assertEqual(versions[-1]["params"]["series_rev"], 2)
        rates2 = [c["result"]["blank"]["rate"] for c in versions[-1]["cycles"]]
        self.assertNotEqual(rates1, rates2)
        self.assertEqual(len(db.list_batch_versions(conn, other_id)), 1)
        # 再次保存相同内容 → 无变化，不产生新版本
        out4 = self._post(server.api_series_save, {
            "id": sid, "name": "背景", "method": "linear", "max_gap_s": 7200,
            "anchors": [
                {"id": a1, "blank_dataset_id": blank_id, "collected_at": 0},
                {"id": a2, "blank_dataset_id": blank_id, "collected_at": 900,
                 "locked_rate": 0.06},
            ]})
        self.assertFalse(out4["changed"])
        self.assertEqual(out4["affected"], [])
        self.assertEqual(len(db.list_batch_versions(conn, sample_id)), 2)
        conn.close()

    def test_disable_anchor_triggers_recompute(self):
        conn = db.connect()
        blank_id = db.create_dataset(conn, "blank1", "", True, "b.csv",
                                     make_run([0.03]))
        db.save_batch_version(conn, blank_id, {}, [],
                              {"n_total": 1, "n_included": 1, "mean": 0.03,
                               "volume": 1.0})
        sample_pts = make_run([0.4, 0.4])
        sample_id = db.create_dataset(conn, "fishX", "FX", False, "s.csv",
                                      sample_pts)
        out = self._post(server.api_series_save, {
            "name": "背景", "method": "nearest", "max_gap_s": 0,
            "anchors": [
                {"blank_dataset_id": blank_id, "collected_at": 0},
                {"blank_dataset_id": blank_id, "collected_at": 1200,
                 "locked_rate": 0.09},
            ]})
        sid = out["series"]["id"]
        a2 = out["series"]["anchors"][1]["id"]
        _, segs = seg_of(sample_pts)
        self._post(server.api_batch_analyze, {
            "dataset_id": sample_id, "resegment": False, "cycles": segs,
            "blank_series_id": sid, "save": True, "min_duration": 60})
        # 停用 A2 → 仅剩 A1，所有周期改用 A1
        out2 = self._post(server.api_series_save, {
            "id": sid, "name": "背景", "method": "nearest", "max_gap_s": 0,
            "anchors": [
                {"id": out["series"]["anchors"][0]["id"],
                 "blank_dataset_id": blank_id, "collected_at": 0},
                {"id": a2, "blank_dataset_id": blank_id, "collected_at": 1200,
                 "locked_rate": 0.09, "disabled": True},
            ]})
        self.assertEqual(len(out2["affected"]), 1)
        versions = db.list_batch_versions(conn, sample_id)
        rates = [c["result"]["blank"]["rate"] for c in versions[-1]["cycles"]]
        self.assertTrue(all(abs(r - 0.03) < 1e-9 for r in rates))
        conn.close()


class BlankPropagationTest(unittest.TestCase):
    """空白室新增/撤销已确认批次 → 序列 rev 与关联样本版本同步更新。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["RESPIRO_DB"] = self.path
        db.DB_PATH = self.path

    def tearDown(self):
        os.unlink(self.path)

    @staticmethod
    def _post(fn, payload):
        body = json.dumps(payload).encode()
        env = {"wsgi.input": io.BytesIO(body),
               "CONTENT_LENGTH": str(len(body))}
        out, err = fn(env)
        assert err is None, err
        return out

    def _blank_with_batch(self, conn, calib=None):
        """空白数据集 + 一个已确认批次，返回 (blank_id, cycles, mean)。"""
        pts = make_run([0.03])
        bid = db.create_dataset(conn, "blank", "", True, "b.csv", pts)
        _, segs = seg_of(pts)
        payload = {"dataset_id": bid, "resegment": False, "cycles": segs,
                   "save": True, "min_duration": 60}
        if calib:
            payload["calib_points"] = calib
        out = self._post(server.api_batch_analyze, payload)
        return bid, segs, out["summary"]["mean"]

    def _sample_with_series(self, conn, sid, rates=(0.4, 0.4)):
        pts = make_run(list(rates))
        sample_id = db.create_dataset(conn, "fish", "F", False, "s.csv", pts)
        _, segs = seg_of(pts)
        return sample_id, self._post(server.api_batch_analyze, {
            "dataset_id": sample_id, "resegment": False, "cycles": segs,
            "blank_series_id": sid, "save": True, "min_duration": 60})

    def test_new_blank_batch_propagates_to_samples(self):
        conn = db.connect()
        blank_id, segs, mean1 = self._blank_with_batch(conn)
        out = self._post(server.api_series_save, {
            "name": "背景", "method": "nearest", "max_gap_s": 0,
            "anchors": [{"blank_dataset_id": blank_id, "collected_at": 0}]})
        sid = out["series"]["id"]
        sample_id, out1 = self._sample_with_series(conn, sid)
        rate_v1 = out1["cycles"][0]["result"]["blank"]["rate"]
        self.assertAlmostEqual(rate_v1, mean1, places=9)
        # 空白新增确认批次（校准点引入趋势 → 有效均值变化）
        out2 = self._post(server.api_batch_analyze, {
            "dataset_id": blank_id, "resegment": False, "cycles": segs,
            "calib_points": [[0, 8.6], [600, 8.4]],
            "save": True, "min_duration": 60})
        mean2 = out2["summary"]["mean"]
        self.assertNotAlmostEqual(mean1, mean2, places=6)
        # 传播发生且指向本序列
        prop = out2["blank_propagation"]
        self.assertEqual(len(prop), 1)
        self.assertEqual(prop[0]["series_id"], sid)
        self.assertEqual(prop[0]["series_rev"], 2)
        self.assertEqual([a["dataset_id"] for a in prop[0]["affected"]],
                         [sample_id])
        # 序列 rev 提升
        self.assertEqual(db.get_series(conn, sid)["rev"], 2)
        # 样本生成关联新版本，blank_rate 同步为新均值；旧版本仍可查阅
        versions = db.list_batch_versions(conn, sample_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1]["params"]["series_rev"], 2)
        self.assertEqual(versions[-1]["params"]["auto_from_version"],
                         versions[0]["id"])
        rate_v2 = versions[-1]["cycles"][0]["result"]["blank"]["rate"]
        self.assertAlmostEqual(rate_v2, mean2, places=9)
        self.assertNotAlmostEqual(rate_v1, rate_v2, places=6)
        self.assertAlmostEqual(
            versions[0]["cycles"][0]["result"]["blank"]["rate"],
            rate_v1, places=9)
        # 序列当前解析值与已存校正结果一致
        listed = server.api_series_list({})
        cur = listed["series"][0]["anchors"][0]["rate"]
        self.assertAlmostEqual(cur, rate_v2, places=9)
        conn.close()

    def test_locked_anchor_immune_to_source_batch(self):
        conn = db.connect()
        blank_id, segs, _ = self._blank_with_batch(conn)
        out = self._post(server.api_series_save, {
            "name": "背景", "method": "nearest", "max_gap_s": 0,
            "anchors": [{"blank_dataset_id": blank_id, "collected_at": 0,
                         "locked_rate": 0.04}]})
        sid = out["series"]["id"]
        sample_id, _ = self._sample_with_series(conn, sid)
        # 空白新增确认批次（均值变化）→ 锁定锚点不受影响
        out2 = self._post(server.api_batch_analyze, {
            "dataset_id": blank_id, "resegment": False, "cycles": segs,
            "calib_points": [[0, 8.6], [600, 8.4]],
            "save": True, "min_duration": 60})
        self.assertEqual(out2["blank_propagation"], [])
        self.assertEqual(db.get_series(conn, sid)["rev"], 1)
        versions = db.list_batch_versions(conn, sample_id)
        self.assertEqual(len(versions), 1)
        self.assertAlmostEqual(
            versions[0]["cycles"][0]["result"]["blank"]["rate"], 0.04, places=9)
        conn.close()

    def test_unchanged_mean_no_propagation(self):
        conn = db.connect()
        blank_id, segs, _ = self._blank_with_batch(conn)
        out = self._post(server.api_series_save, {
            "name": "背景", "method": "nearest", "max_gap_s": 0,
            "anchors": [{"blank_dataset_id": blank_id, "collected_at": 0}]})
        sid = out["series"]["id"]
        sample_id, _ = self._sample_with_series(conn, sid)
        # 相同参数再保存一次 → 均值未变 → 无传播
        out2 = self._post(server.api_batch_analyze, {
            "dataset_id": blank_id, "resegment": False, "cycles": segs,
            "save": True, "min_duration": 60})
        self.assertEqual(out2["blank_propagation"], [])
        self.assertEqual(db.get_series(conn, sid)["rev"], 1)
        self.assertEqual(len(db.list_batch_versions(conn, sample_id)), 1)
        conn.close()

    def test_blank_batch_undo_propagates(self):
        conn = db.connect()
        blank_id, segs, mean1 = self._blank_with_batch(conn)
        out = self._post(server.api_series_save, {
            "name": "背景", "method": "nearest", "max_gap_s": 0,
            "anchors": [{"blank_dataset_id": blank_id, "collected_at": 0}]})
        sid = out["series"]["id"]
        sample_id, _ = self._sample_with_series(conn, sid)
        # 第二批（均值变化）→ rev 2，样本 v2
        self._post(server.api_batch_analyze, {
            "dataset_id": blank_id, "resegment": False, "cycles": segs,
            "calib_points": [[0, 8.6], [600, 8.4]],
            "save": True, "min_duration": 60})
        # 撤销空白第二批 → 有效均值回退 → 再次传播
        body = json.dumps({"dataset_id": blank_id}).encode()
        out = server.api_batch_undo(
            {"wsgi.input": io.BytesIO(body),
             "CONTENT_LENGTH": str(len(body))})
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["blank_propagation"]), 1)
        self.assertEqual(db.get_series(conn, sid)["rev"], 3)
        versions = db.list_batch_versions(conn, sample_id)
        self.assertEqual(len(versions), 3)
        self.assertEqual(versions[-1]["params"]["series_rev"], 3)
        self.assertAlmostEqual(
            versions[-1]["cycles"][0]["result"]["blank"]["rate"],
            mean1, places=9)
        conn.close()


if __name__ == "__main__":
    unittest.main()
