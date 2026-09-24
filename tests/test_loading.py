"""逐管安全装载次序规划测试: python -m unittest discover -s tests

核心断言: 子集动态规划求得的最小全过程峰值 |U₁|、装载次序与最优次序条数,
必须与全排列暴力枚举一致(含并列时按试管编号序列升序裁决); 逐步明细中
每步恰装一支、槽位与方案一致、累计峰值单调不减且末值等于最小峰值; 最终
|U₁| 与全部试管装入后的向量和一致; 超过 16 支、空方案、负限值、非法摆放
必须拒绝; 规划过程不修改入参(只读)。
"""
import copy
import itertools
import math
import random
import unittest

from app.loading import plan_loading_sequence


def _plan(pid, placements, radius=100.0, name=None):
    """placements: {槽号: (试管编号, 质量, 角度°)}。"""
    return {
        "id": pid,
        "name": name or f"方案{pid}",
        "created_at": "2026-09-24T00:00:00+00:00",
        "request": {"radius_mm": radius},
        "result": {
            "config": {"radius_mm": radius},
            "placements": [
                {"tube_id": t, "slot": s, "mass": m, "angle": a}
                for s, (t, m, a) in placements.items()
            ],
        },
    }


def _brute(placements, radius):
    """全排列参考实现: (最小峰值, 升序裁决后的试管编号序列, 最优次序条数)。"""
    vecs = {}
    for s, (t, m, a) in placements.items():
        r = math.radians(a)
        vecs[t] = radius * m * complex(math.cos(r), math.sin(r))
    ids = sorted(vecs)
    best = None
    seq = None
    cnt = 0
    for perm in itertools.permutations(ids):
        ux = uy = 0.0
        peak = 0.0
        for tid in perm:
            v = vecs[tid]
            ux += v.real
            uy += v.imag
            m2 = math.hypot(ux, uy)
            if m2 > peak:
                peak = m2
        if best is None or peak < best - 1e-9 * max(1.0, best):
            best, seq, cnt = peak, perm, 1
        elif peak <= best + 1e-9 * max(1.0, best):
            cnt += 1
            seq = min(seq, perm)
    return best, list(seq), cnt


class TestLoadingSequence(unittest.TestCase):
    def test_two_tubes_opposite(self):
        # 严格对径等质量: 第一步 5000, 第二步对消为 0, 峰值 5000
        p = _plan(1, {0: (1, 50.0, 0.0), 6: (2, 50.0, 180.0)})
        r = plan_loading_sequence(p)
        self.assertAlmostEqual(r["peak_u1_g_mm"], 5000.0, places=9)
        self.assertAlmostEqual(r["final_u1_g_mm"], 0.0, places=9)
        self.assertEqual(r["order"], [1, 2])
        self.assertEqual(r["optimal_order_count"], 2)
        self.assertTrue(r["tie_broken"])
        self.assertEqual(r["tube_count"], 2)
        self.assertEqual(r["radius_mm"], 100.0)
        s1, s2 = r["steps"]
        self.assertEqual((s1["seq"], s1["tube_id"], s1["slot"]), (1, 1, 0))
        self.assertAlmostEqual(s1["u1_mag_g_mm"], 5000.0, places=9)
        self.assertAlmostEqual(s1["peak_so_far_g_mm"], 5000.0, places=9)
        self.assertEqual((s2["seq"], s2["tube_id"], s2["slot"]), (2, 2, 6))
        self.assertAlmostEqual(s2["u1_mag_g_mm"], 0.0, places=9)
        self.assertAlmostEqual(s2["peak_so_far_g_mm"], 5000.0, places=9)
        self.assertIsNone(r["within_limit"])
        self.assertIsNone(r["u1_limit_g_mm"])
        self.assertEqual(r["exceeded_steps"], [])
        self.assertEqual(r["exceeded_count"], 0)

    def test_tie_break_by_tube_id_sequence(self):
        # 两对对径等质量: 最优峰值 5000 的次序共 8 条, 取编号升序 [1,2,3,4]
        p = _plan(1, {0: (1, 50.0, 0.0), 6: (2, 50.0, 180.0),
                      3: (3, 30.0, 90.0), 9: (4, 30.0, 270.0)})
        r = plan_loading_sequence(p)
        self.assertAlmostEqual(r["peak_u1_g_mm"], 5000.0, places=9)
        self.assertEqual(r["optimal_order_count"], 8)
        self.assertTrue(r["tie_broken"])
        self.assertEqual(r["order"], [1, 2, 3, 4])
        for want, s in zip([5000.0, 0.0, 3000.0, 0.0], r["steps"]):
            self.assertAlmostEqual(want, s["u1_mag_g_mm"], places=9)

    def test_matches_brute_force_random(self):
        rng = random.Random(20260924)
        for _ in range(40):
            n = rng.randint(1, 8)
            slots = rng.sample(range(24), n)
            placements = {}
            for i, s in enumerate(slots):
                placements[s] = (i + 1, rng.uniform(5.0, 60.0),
                                 rng.uniform(0.0, 360.0))
            radius = rng.uniform(50.0, 200.0)
            plan = _plan(1, placements, radius=radius)
            r = plan_loading_sequence(plan)
            best, seq, cnt = _brute(placements, radius)
            self.assertAlmostEqual(r["peak_u1_g_mm"], best,
                                   delta=1e-6 * max(1.0, best))
            self.assertEqual(r["order"], seq)
            self.assertEqual(r["optimal_order_count"], cnt)
            self.assertEqual(r["tie_broken"], cnt > 1)
            # 逐步明细: 每步恰装一支, 与最优次序一致
            self.assertEqual(len(r["steps"]), n)
            self.assertEqual([s["tube_id"] for s in r["steps"]], seq)
            self.assertEqual([s["seq"] for s in r["steps"]],
                             list(range(1, n + 1)))
            self.assertEqual(sorted(s["slot"] for s in r["steps"]),
                             sorted(placements))
            # 累计峰值单调不减, 末值等于最小峰值
            peaks = [s["peak_so_far_g_mm"] for s in r["steps"]]
            self.assertEqual(peaks, sorted(peaks))
            self.assertAlmostEqual(peaks[-1], r["peak_u1_g_mm"], places=6)
            # 当步 |U₁| 与独立复算一致; 最终态与全部装入的向量和一致
            vecs = {}
            for s, (t, m, a) in placements.items():
                rad = math.radians(a)
                vecs[t] = radius * m * complex(math.cos(rad), math.sin(rad))
            ux = uy = 0.0
            for s in r["steps"]:
                v = vecs[s["tube_id"]]
                ux += v.real
                uy += v.imag
                self.assertAlmostEqual(s["u1_mag_g_mm"],
                                       math.hypot(ux, uy), places=6)
                self.assertAlmostEqual(s["u1_x_g_mm"], ux, places=6)
                self.assertAlmostEqual(s["u1_y_g_mm"], uy, places=6)
            self.assertAlmostEqual(r["final_u1_g_mm"],
                                   math.hypot(ux, uy), places=6)

    def test_limit_judgement(self):
        p = _plan(1, {0: (1, 50.0, 0.0), 6: (2, 50.0, 180.0),
                      3: (3, 30.0, 90.0), 9: (4, 30.0, 270.0)})
        # 限值 4000: 第 1 步 5000 超限, 其余步不超限
        r = plan_loading_sequence(p, u1_limit_g_mm=4000.0)
        self.assertFalse(r["within_limit"])
        self.assertEqual(r["exceeded_steps"], [1])
        self.assertEqual(r["exceeded_count"], 1)
        self.assertEqual([s["exceeds_limit"] for s in r["steps"]],
                         [True, False, False, False])
        self.assertAlmostEqual(r["excess_g_mm"], 1000.0, places=9)
        self.assertEqual(r["margin_g_mm"], 0.0)
        # 限值恰等于峰值: 合格, 余量 0
        r2 = plan_loading_sequence(p, u1_limit_g_mm=5000.0)
        self.assertTrue(r2["within_limit"])
        self.assertEqual(r2["exceeded_steps"], [])
        self.assertAlmostEqual(r2["margin_g_mm"], 0.0, places=9)
        # 不填限值: 只计算不判定
        r3 = plan_loading_sequence(p)
        self.assertIsNone(r3["within_limit"])
        self.assertIsNone(r3["excess_g_mm"])
        self.assertIsNone(r3["margin_g_mm"])
        self.assertTrue(all(s["exceeds_limit"] is None for s in r3["steps"]))

    def test_sixteen_tubes_accepted(self):
        rng = random.Random(7)
        placements = {i: (i + 1, rng.uniform(5.0, 60.0), i * (360.0 / 16))
                      for i in range(16)}
        r = plan_loading_sequence(_plan(1, placements))
        self.assertEqual(r["tube_count"], 16)
        self.assertEqual(len(r["steps"]), 16)
        self.assertEqual(sorted(r["order"]), list(range(1, 17)))
        self.assertAlmostEqual(r["steps"][-1]["peak_so_far_g_mm"],
                               r["peak_u1_g_mm"], places=6)

    def test_seventeen_tubes_rejected(self):
        placements = {i: (i + 1, 10.0, i * (360.0 / 17)) for i in range(17)}
        with self.assertRaises(ValueError):
            plan_loading_sequence(_plan(1, placements))

    def test_empty_plan_rejected(self):
        with self.assertRaises(ValueError):
            plan_loading_sequence(_plan(1, {}))

    def test_negative_limit_rejected(self):
        p = _plan(1, {0: (1, 50.0, 0.0)})
        with self.assertRaises(ValueError):
            plan_loading_sequence(p, u1_limit_g_mm=-0.5)

    def test_invalid_placements_rejected(self):
        # 重复试管编号
        p1 = _plan(1, {0: (1, 50.0, 0.0), 1: (1, 40.0, 30.0)})
        with self.assertRaises(ValueError):
            plan_loading_sequence(p1)
        # 重复槽位(dict 键唯一, 直接改摆放明细构造)
        p2 = _plan(1, {0: (1, 50.0, 0.0), 1: (2, 40.0, 30.0)})
        p2["result"]["placements"][1]["slot"] = 0
        with self.assertRaises(ValueError):
            plan_loading_sequence(p2)
        # 质量非正
        p3 = _plan(1, {0: (1, 0.0, 0.0)})
        with self.assertRaises(ValueError):
            plan_loading_sequence(p3)
        # 缺少槽位角度
        p4 = _plan(1, {0: (1, 50.0, 0.0)})
        p4["result"]["placements"][0]["angle"] = None
        with self.assertRaises(ValueError):
            plan_loading_sequence(p4)

    def test_read_only(self):
        p = _plan(1, {0: (1, 50.0, 0.0), 6: (2, 50.0, 180.0),
                      3: (3, 30.0, 90.0)})
        snap = copy.deepcopy(p)
        plan_loading_sequence(p, u1_limit_g_mm=1000.0)
        self.assertEqual(p, snap)

    def test_plan_info_and_note(self):
        p = _plan(9, {0: (1, 50.0, 0.0)}, name="装载测试")
        r = plan_loading_sequence(p)
        self.assertEqual(r["plan"]["id"], 9)
        self.assertEqual(r["plan"]["name"], "装载测试")
        self.assertIn("只读", r["note"])
        self.assertIn("子集动态规划", r["method"])
        # 单支试管: 峰值即其质径积, 次序唯一
        self.assertEqual(r["order"], [1])
        self.assertEqual(r["optimal_order_count"], 1)
        self.assertFalse(r["tie_broken"])
        self.assertAlmostEqual(r["peak_u1_g_mm"], 5000.0, places=9)


if __name__ == "__main__":
    unittest.main()
