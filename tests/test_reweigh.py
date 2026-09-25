"""复称规划测试: python -m unittest discover -s tests

核心断言: 按复称管数分层的完全枚举结果(最少复称管数、复称后最不利 |U₁|、
最优组合条数与按管号序列升序的并列裁决)必须与独立参考实现一致——参考实现
对每个复称组合用 2^n 全组合枚举求最不利值; 复称只把该管误差上界降至两者
较小值, 标称质量与落槽不变; 次数内无法达标时必须给出最不利值最低的组合
及超限量且不误报达标; 超过 24 支试管或复称上限 0~3 之外的请求必须拒绝;
规划过程不修改入参(只读)。
"""
import copy
import itertools
import math
import random
import unittest

from app.reweigh import plan_reweigh
from app.weighing import worst_case_u1


def _plan(pid, placements, radius=100.0, slots_n=12, name=None):
    """placements: {槽号: (试管编号, 质量)}; 角度按槽号均布。"""
    pls = [
        {
            "tube_id": t,
            "mass": m,
            "slot": s,
            "angle": 360.0 * s / slots_n,
            "locked": False,
        }
        for s, (t, m) in placements.items()
    ]
    return {
        "id": pid,
        "name": name or f"方案{pid}",
        "created_at": "2026-09-25T00:00:00+00:00",
        "request": {"radius_mm": radius},
        "result": {
            "config": {"radius_mm": radius},
            "placements": pls,
            "metrics": {"u1_mag_g_mm": 0.0},
        },
    }


def _cross_plan(pid=1):
    """四支 50 g 试管在槽 0/3/6/9(0°/90°/180°/270°), 标称完全对消。"""
    return _plan(pid, {0: (1, 50.0), 3: (2, 50.0),
                       6: (3, 50.0), 9: (4, 50.0)})


def _brute_worst(tubes, errors, radius):
    """2^n 全组合参考: tubes 为 (编号, 角度, 质量) 列表。"""
    n = len(tubes)
    best = 0.0
    for mask in range(1 << n):
        ux = uy = 0.0
        for i, (tid, ang, mass) in enumerate(tubes):
            e = errors.get(tid, 0.0)
            s = 1.0 if (mask >> i) & 1 else -1.0
            m = mass + s * e
            a = math.radians(ang)
            ux += m * math.cos(a)
            uy += m * math.sin(a)
        best = max(best, radius * math.hypot(ux, uy))
    return best


def _brute_reweigh(tubes, errors, bound, limit, max_count, radius):
    """独立全枚举参考(选择规则与被测模块一致, 最不利值用 2^n 枚举)。

    返回 ("ok", 最少复称管数, 复称后最不利值, 管号序列, 最优组合条数)
    或 ("no", (最不利值, 管号序列, 复称管数, 并列条数))。
    """
    ids = sorted(t[0] for t in tubes)
    cand = [t for t in ids if errors.get(t, 0.0) > bound]
    lim_tol = 1e-9 * max(1.0, limit)

    def worst(combo):
        es = {
            t: (min(errors.get(t, 0.0), bound) if t in combo
                else errors.get(t, 0.0))
            for t in ids
        }
        return _brute_worst(tubes, es, radius)

    best_effort = None
    for k in range(0, max_count + 1):
        leaves = [(worst(c), c) for c in itertools.combinations(cand, k)]
        if not leaves:
            continue
        bm = min(m for m, _ in leaves)
        btol = 1e-9 * max(1.0, bm)
        btied = [(m, c) for m, c in leaves if m <= bm + btol]
        bkey = min(c for _, c in btied)
        bmag = next(m for m, c in btied if c == bkey)
        if best_effort is None or bmag < best_effort[0] - 1e-9:
            best_effort = (bmag, bkey, k, len(btied))
        feas = [(m, c) for m, c in leaves if m <= limit + lim_tol]
        if feas:
            fm = min(m for m, _ in feas)
            ftol = 1e-9 * max(1.0, fm)
            ftied = [(m, c) for m, c in feas if m <= fm + ftol]
            fkey = min(c for _, c in ftied)
            fmag = next(m for m, c in ftied if c == fkey)
            return ("ok", k, fmag, fkey, len(ftied))
    return ("no", best_effort)


class TestReweighPlan(unittest.TestCase):
    def test_baseline_already_within_limit(self):
        # 复称前最不利值 100·2√2 ≈ 282.84 ≤ 300: 无需复称
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=300.0, max_reweigh_count=3)
        self.assertTrue(r["feasible"])
        self.assertTrue(r["within_limit"])
        self.assertEqual(r["reweigh_count"], 0)
        self.assertEqual(r["reweighed_tube_ids"], [])
        self.assertEqual(r["optimal_combination_count"], 1)
        self.assertFalse(r["tie_broken"])
        base = 100.0 * 2.0 * math.sqrt(2.0)
        self.assertAlmostEqual(r["baseline"]["worst_case"]["u1_mag_g_mm"],
                               base, places=6)
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               base, places=9)
        self.assertAlmostEqual(r["improvement_g_mm"], 0.0, places=12)
        self.assertAlmostEqual(r["margin_g_mm"], 300.0 - base, places=6)
        self.assertEqual(r["excess_g_mm"], 0.0)
        self.assertIn("无需复称", r["reason"])
        # 复称 0 支: 各管上界不变
        for t in r["tubes"]:
            self.assertFalse(t["reweighed"])
            self.assertEqual(t["error_bound_after_g"],
                             t["error_bound_before_g"])

    def test_one_reweigh_meets_limit(self):
        # 限值 250: 复称 1 支即可(最不利值 100·√5.21 ≈ 228.25), 四管对称
        # 全部并列, 按管号序列升序取管 1
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=250.0, max_reweigh_count=3)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweigh_count"], 1)
        self.assertEqual(r["reweighed_tube_ids"], [1])
        self.assertEqual(r["optimal_combination_count"], 4)
        self.assertTrue(r["tie_broken"])
        expect = 100.0 * math.sqrt(5.21)
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               expect, places=6)
        self.assertAlmostEqual(r["margin_g_mm"], 250.0 - expect, places=6)
        rows = {t["tube_id"]: t for t in r["tubes"]}
        self.assertTrue(rows[1]["reweighed"])
        self.assertAlmostEqual(rows[1]["error_bound_after_g"], 0.1, places=12)
        self.assertFalse(rows[2]["reweighed"])
        self.assertAlmostEqual(rows[2]["error_bound_after_g"], 1.0, places=12)

    def test_two_reweighs_perpendicular_beats_opposite(self):
        # 限值 200: 复称 1 支不够(228.25), 复称 2 支取垂直对(155.56),
        # 而不是对径对(200.998, 仍超限); 垂直对共 4 组并列, 取 {1,2}
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=200.0, max_reweigh_count=3)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweigh_count"], 2)
        self.assertEqual(r["reweighed_tube_ids"], [1, 2])
        self.assertEqual(r["optimal_combination_count"], 4)
        expect = 100.0 * math.sqrt(2.42)
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               expect, places=6)
        self.assertLess(r["final"]["worst_case"]["u1_mag_g_mm"],
                        100.0 * math.sqrt(4.04))

    def test_three_reweighs_when_two_not_enough(self):
        # 限值 150: 2 支最优 155.56 仍超限, 3 支(100·√1.25 ≈ 111.80)达标
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=150.0, max_reweigh_count=3)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweigh_count"], 3)
        self.assertEqual(r["reweighed_tube_ids"], [1, 2, 3])
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               100.0 * math.sqrt(1.25), places=6)

    def test_infeasible_reports_best_effort_and_excess(self):
        # 限值 150、最多复称 2 次: 次数内最优 {1,2} 仍 155.56 > 150,
        # 必须给出该组合与超限量, 且不得误报达标
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=150.0, max_reweigh_count=2)
        self.assertFalse(r["feasible"])
        self.assertFalse(r["within_limit"])
        self.assertEqual(r["reweigh_count"], 2)
        self.assertEqual(r["reweighed_tube_ids"], [1, 2])
        expect = 100.0 * math.sqrt(2.42)
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               expect, places=6)
        self.assertAlmostEqual(r["excess_g_mm"], expect - 150.0, places=6)
        self.assertEqual(r["margin_g_mm"], 0.0)
        self.assertGreater(r["improvement_g_mm"], 0.0)
        self.assertIn("不达标", r["reason"])
        self.assertIn("超出", r["reason"])
        # 基线判定也超限
        self.assertFalse(r["baseline"]["within_limit"])

    def test_max_count_zero_only_baseline(self):
        # 最多复称 0 次: 只评估复称前状态, 无法改善
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=250.0, max_reweigh_count=0)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["reweigh_count"], 0)
        self.assertEqual(r["reweighed_tube_ids"], [])
        self.assertEqual(r["evaluated_combinations"], 1)
        base = 100.0 * 2.0 * math.sqrt(2.0)
        self.assertAlmostEqual(r["excess_g_mm"], base - 250.0, places=6)

    def test_bound_not_below_current_means_no_candidate(self):
        # 复称后统一上界 2.0 不低于任何当前上界(均 1.0): 复称无效果
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         reweighed_error_bound_g=2.0,
                         u1_limit_g_mm=250.0, max_reweigh_count=3)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["candidate_tube_ids"], [])
        self.assertEqual(r["reweighed_tube_ids"], [])
        self.assertEqual(r["evaluated_combinations"], 1)
        self.assertIn("复称不能降低", r["reason"])

    def test_min_of_two_bounds_and_mixed_candidates(self):
        # 各管当前上界不同: 只有 e > b 的管是有效候选; 复称后上界取两者较小值
        p = _cross_plan()
        errors = {1: 0.5, 2: 2.0, 3: 1.0, 4: 0.05}
        r = plan_reweigh(p, errors, reweighed_error_bound_g=0.8,
                         u1_limit_g_mm=170.0, max_reweigh_count=2)
        self.assertEqual(r["candidate_tube_ids"], [2, 3])
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweigh_count"], 2)
        self.assertEqual(r["reweighed_tube_ids"], [2, 3])
        # 复称 {2,3}: e=(0.5, 0.8, 0.8, 0.05), 最不利值 100·√(1.3²+0.85²)
        expect = 100.0 * math.sqrt(1.3 * 1.3 + 0.85 * 0.85)
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               expect, places=6)
        rows = {t["tube_id"]: t for t in r["tubes"]}
        self.assertAlmostEqual(rows[2]["error_bound_after_g"], 0.8, places=12)
        self.assertAlmostEqual(rows[3]["error_bound_after_g"], 0.8, places=12)
        self.assertAlmostEqual(rows[1]["error_bound_after_g"], 0.5, places=12)
        self.assertAlmostEqual(rows[4]["error_bound_after_g"], 0.05, places=12)
        self.assertTrue(rows[1]["candidate"] is False)
        self.assertTrue(rows[2]["candidate"] is True)

    def test_unlisted_tubes_default_zero_error(self):
        # 未录入误差上界的试管按 0 处理, 不参与候选
        p = _cross_plan()
        r = plan_reweigh(p, {1: 1.0}, reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=1e9, max_reweigh_count=3)
        rows = {t["tube_id"]: t for t in r["tubes"]}
        self.assertEqual(rows[2]["error_bound_before_g"], 0.0)
        self.assertEqual(r["candidate_tube_ids"], [1])

    def test_final_matches_weighing_module(self):
        # 复称后最不利值必须与现有称重误差校核在调整后的上界下一致;
        # 标称值复称前后不变(标称质量与落槽不动)
        p = _cross_plan()
        errors = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
        r = plan_reweigh(p, errors, reweighed_error_bound_g=0.1,
                         u1_limit_g_mm=200.0, max_reweigh_count=2)
        adjusted = {1: 0.1, 2: 0.1, 3: 1.0, 4: 1.0}
        check = worst_case_u1(p["result"]["placements"], 100.0, adjusted)
        self.assertAlmostEqual(r["final"]["worst_case"]["u1_mag_g_mm"],
                               check["worst_case"]["u1_mag_g_mm"], places=9)
        base_check = worst_case_u1(p["result"]["placements"], 100.0, errors)
        self.assertAlmostEqual(r["baseline"]["worst_case"]["u1_mag_g_mm"],
                               base_check["worst_case"]["u1_mag_g_mm"],
                               places=9)
        self.assertAlmostEqual(r["final"]["nominal"]["u1_mag_g_mm"],
                               r["baseline"]["nominal"]["u1_mag_g_mm"],
                               places=12)

    def test_matches_brute_force_random(self):
        rng = random.Random(20260925)
        for _ in range(60):
            slots_n = 12
            n = rng.randint(1, 8)
            slots = rng.sample(range(slots_n), n)
            placements = {}
            tubes = []
            for i, s in enumerate(slots):
                mass = rng.uniform(5.0, 60.0)
                placements[s] = (i + 1, mass)
                tubes.append((i + 1, 360.0 * s / slots_n, mass))
            radius = rng.uniform(50.0, 200.0)
            plan = _plan(1, placements, radius=radius, slots_n=slots_n)
            errors = {}
            for tid, _, mass in tubes:
                if rng.random() < 0.85:
                    errors[tid] = rng.uniform(0.0, mass)
            bound = rng.uniform(0.0, 1.0)
            limit = rng.uniform(0.0, 8000.0)
            max_count = rng.randint(0, 3)
            got = plan_reweigh(plan, errors, bound, limit, max_count)
            want = _brute_reweigh(tubes, errors, bound, limit, max_count,
                                  radius)
            if want[0] == "ok":
                _, k, mag, key, cnt = want
                self.assertTrue(got["feasible"])
                self.assertTrue(got["within_limit"])
                self.assertEqual(got["reweigh_count"], k)
                self.assertEqual(tuple(got["reweighed_tube_ids"]), key)
                self.assertEqual(got["optimal_combination_count"], cnt)
                self.assertAlmostEqual(
                    got["final"]["worst_case"]["u1_mag_g_mm"], mag,
                    delta=1e-6 * max(1.0, mag))
                self.assertEqual(got["excess_g_mm"], 0.0)
            else:
                _, (mag, key, k, cnt) = want
                self.assertFalse(got["feasible"])
                self.assertFalse(got["within_limit"])
                self.assertEqual(got["reweigh_count"], k)
                self.assertEqual(tuple(got["reweighed_tube_ids"]), key)
                self.assertAlmostEqual(
                    got["final"]["worst_case"]["u1_mag_g_mm"], mag,
                    delta=1e-6 * max(1.0, mag))
                self.assertAlmostEqual(
                    got["excess_g_mm"], max(0.0, mag - limit),
                    delta=1e-6 * max(1.0, mag))
            # 复称后各管上界: min(当前, 统一上界) 或不变
            for row in got["tubes"]:
                tid = row["tube_id"]
                before = min(errors.get(tid, 0.0),
                             row["nominal_mass_g"])
                self.assertAlmostEqual(row["error_bound_before_g"], before,
                                       places=12)
                if row["reweighed"]:
                    self.assertAlmostEqual(row["error_bound_after_g"],
                                           min(before, bound), places=12)
                else:
                    self.assertAlmostEqual(row["error_bound_after_g"], before,
                                           places=12)

    def test_rejections(self):
        p = _cross_plan()
        errors = {1: 1.0}
        # 负限值 / 非有限限值 / 负上界 / 非有限上界 / 次数越界
        with self.assertRaises(ValueError):
            plan_reweigh(p, errors, 0.1, -1.0, 1)
        with self.assertRaises(ValueError):
            plan_reweigh(p, errors, 0.1, float("nan"), 1)
        with self.assertRaises(ValueError):
            plan_reweigh(p, errors, -0.1, 100.0, 1)
        with self.assertRaises(ValueError):
            plan_reweigh(p, errors, float("inf"), 100.0, 1)
        for bad_m in (-1, 4):
            with self.assertRaises(ValueError):
                plan_reweigh(p, errors, 0.1, 100.0, bad_m)
        # 误差上界为负 / 非有限 / 超过标称质量 / 试管不在摆放明细中
        with self.assertRaises(ValueError):
            plan_reweigh(p, {1: -0.01}, 0.1, 100.0, 1)
        with self.assertRaises(ValueError):
            plan_reweigh(p, {1: float("nan")}, 0.1, 100.0, 1)
        with self.assertRaises(ValueError):
            plan_reweigh(p, {1: 50.1}, 0.1, 100.0, 1)
        with self.assertRaises(ValueError):
            plan_reweigh(p, {99: 0.1}, 0.1, 100.0, 1)
        # 空方案
        with self.assertRaises(ValueError):
            plan_reweigh(_plan(1, {}), {}, 0.1, 100.0, 1)
        # 超过 24 支试管
        big = _plan(1, {i: (i + 1, 10.0) for i in range(25)}, slots_n=32)
        with self.assertRaises(ValueError):
            plan_reweigh(big, {}, 0.1, 100.0, 1)
        # 摆放明细非法: 重复试管 / 质量非正 / 缺角度 / 半径非法
        p_dup = _plan(1, {0: (1, 50.0), 1: (1, 40.0)})
        with self.assertRaises(ValueError):
            plan_reweigh(p_dup, {}, 0.1, 100.0, 1)
        p_mass = _plan(1, {0: (1, 0.0)})
        with self.assertRaises(ValueError):
            plan_reweigh(p_mass, {}, 0.1, 100.0, 1)
        p_ang = _plan(1, {0: (1, 50.0)})
        p_ang["result"]["placements"][0]["angle"] = None
        with self.assertRaises(ValueError):
            plan_reweigh(p_ang, {}, 0.1, 100.0, 1)
        p_rad = _cross_plan()
        p_rad["result"]["config"]["radius_mm"] = 0.0
        p_rad["request"]["radius_mm"] = 0.0
        with self.assertRaises(ValueError):
            plan_reweigh(p_rad, {}, 0.1, 100.0, 1)

    def test_unplaced_tube_error_rejected(self):
        # 未放置(slot=None)的试管不参与规划, 其误差上界被拒绝
        p = _cross_plan()
        p["result"]["placements"].append(
            {"tube_id": 9, "mass": 10.0, "slot": None, "angle": None})
        with self.assertRaises(ValueError):
            plan_reweigh(p, {9: 0.1}, 0.1, 100.0, 1)
        # 但不报错时它也不影响结果
        r = plan_reweigh(p, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0},
                         0.1, 250.0, 3)
        self.assertEqual(r["tube_count"], 4)

    def test_read_only(self):
        p = _cross_plan()
        snap = copy.deepcopy(p)
        plan_reweigh(p, {1: 1.0, 2: 1.0}, 0.1, 200.0, 3)
        self.assertEqual(p, snap)

    def test_plan_info_and_note(self):
        p = _cross_plan(pid=7)
        p["name"] = "复称测试"
        r = plan_reweigh(p, {1: 1.0}, 0.1, 1e9, 2)
        self.assertEqual(r["plan"]["id"], 7)
        self.assertEqual(r["plan"]["name"], "复称测试")
        self.assertIn("只读", r["note"])
        self.assertIn("管号序列升序", r["method"])
        self.assertEqual(r["radius_mm"], 100.0)
        self.assertEqual(r["max_reweigh_count"], 2)
        self.assertEqual(r["u1_limit_g_mm"], 1e9)
        self.assertEqual(r["reweighed_error_bound_g"], 0.1)
        self.assertGreaterEqual(r["evaluated_combinations"], 1)


if __name__ == "__main__":
    unittest.main()
