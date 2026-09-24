"""复称规划测试: python -m unittest discover -s tests

核心断言:
- 对每个复称组合, 模块输出必须与对现有最不利一阶不平衡量计算
  (app.weighing.worst_case_u1) 的直接枚举一致;
- 先找能达标的最少复称管数, 再在该管数内选最不利值最低的组合,
  平局按管号序列升序, 最优组合条数统计正确;
- 次数内均无法达标时 feasible=false, 不误报达标, 给出次数内最优组合
  与超限量;
- 复称只改误差上界(降至 min(e_k, e*)): 标称质量、落槽、复称前最不利
  值保持不变(只读, 不修改入参);
- 非法输入(误差上界为负/超限、次数越界、管数超上限、管号不存在等)
  必须拒绝。
"""
import copy
import itertools
import random
import unittest

from app.reweigh import plan_reweigh, MAX_TUBES, MAX_REWEIGHS
from app.weighing import worst_case_u1


def _plan(pid, placements, slots_n=12, enabled=None, radius=100.0, name=None):
    """placements: {槽号: (试管编号, 标称质量)}; 角度按槽号均布。"""
    if enabled is None:
        enabled = set(range(slots_n))
    slot_state = [
        {
            "slot": i,
            "angle": 360.0 * i / slots_n,
            "enabled": i in enabled,
            "tube_id": next(
                (t for s, (t, m) in placements.items() if s == i), None
            ),
            "locked": False,
        }
        for i in range(slots_n)
    ]
    pls = [
        {"tube_id": t, "mass": m, "slot": s, "angle": 360.0 * s / slots_n}
        for s, (t, m) in placements.items()
    ]
    return {
        "id": pid,
        "name": name or f"方案{pid}",
        "created_at": "2026-09-24T00:00:00+00:00",
        "request": {"radius_mm": radius},
        "result": {
            "config": {"radius_mm": radius, "rpm": 4000.0},
            "slot_state": slot_state,
            "placements": pls,
            "metrics": {"u1_mag_g_mm": 0.0},
        },
    }


def _brute_layers(p, errs, post, M):
    """直接对每个复称组合调用 worst_case_u1, 返回 {k: (最优值, 组合, 条数)}。"""
    pls = p["result"]["placements"]
    R = p["result"]["config"]["radius_mm"]
    ids = sorted(int(x["tube_id"]) for x in pls)
    edict = {tid: errs.get(tid, 0.0) for tid in ids}
    cands = sorted(tid for tid in ids if edict[tid] > post)
    layers = {}
    for k in range(0, min(M, len(cands)) + 1):
        vals = []
        for combo in itertools.combinations(cands, k):
            e2 = {tid: (post if tid in combo else edict[tid]) for tid in ids}
            vals.append((
                worst_case_u1(pls, R, e2)["worst_case"]["u1_mag_g_mm"],
                tuple(combo),
            ))
        bm = min(v for v, _ in vals)
        tied = [(v, c) for v, c in vals if v <= bm + 1e-9 * max(1.0, bm)]
        layers[k] = (bm, min(c for _, c in tied), len(tied))
    return layers, ids, edict


class TestReweigh(unittest.TestCase):
    def test_already_within_limit_needs_zero(self):
        # 限值极宽松: 最少复称管数 0, 判定达标
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0), 9: (4, 49.0)})
        r = plan_reweigh(
            p,
            [{"tube_id": i, "mass_error_bound_g": 1.0} for i in range(1, 5)],
            unified_error_bound_g=0.1,
            u1_limit_g_mm=1.0e9,
            max_reweighs=3,
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweighs_required"], 0)
        self.assertEqual(r["reweighed_tubes"], [])
        self.assertEqual(r["excess_g_mm"], 0.0)
        self.assertIn("无需复称", r["reason"])
        # 复称前后相同
        self.assertAlmostEqual(
            r["before"]["worst_case"]["u1_mag_g_mm"],
            r["after"]["worst_case"]["u1_mag_g_mm"], places=9)

    def test_minimum_count_then_lowest_value_then_lex_order(self):
        # 构造: k=1 不达标, k=2 达标; k=2 内取最不利值最低组合,
        # 平局按管号序列升序
        p = _plan(1, {
            0: (1, 50.0), 3: (2, 50.0), 6: (3, 50.0), 9: (4, 50.0),
            1: (5, 30.0), 7: (6, 30.0),
        })
        errs = {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0, 5: 0.5, 6: 0.5}
        post = 0.05
        M = 3
        layers, ids, _ = _brute_layers(p, errs, post, M)
        # 选一个限值: 只让 k>=2 达标(若该数据不满足分层, 直接用暴力结果选)
        k1 = layers[1][0]
        k2 = layers[2][0]
        if k2 < k1:
            limit = (k1 + k2) / 2.0
            expect_k = 2
        else:
            self.skipTest("数据未形成分层, 由随机用例覆盖该性质")
        r = plan_reweigh(
            p,
            [{"tube_id": t, "mass_error_bound_g": e} for t, e in errs.items()],
            post, limit, M,
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweighs_required"], expect_k)
        bm, bid, cnt = layers[expect_k]
        self.assertEqual(tuple(r["reweighed_tubes"]), bid)
        self.assertEqual(r["optimal_combination_count"], cnt)
        self.assertAlmostEqual(
            r["after"]["worst_case"]["u1_mag_g_mm"], bm, places=6)

    def test_tie_lex_smallest(self):
        # 对称布局 + 对称误差: 多个组合最不利值完全相等, 必须取管号序列最小
        # 4 管均布且质量/误差相同: 单管复称的最不利值对任一支都相同
        p = _plan(1, {0: (1, 50.0), 3: (2, 50.0), 6: (3, 50.0), 9: (4, 50.0)})
        errs = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
        post = 0.1
        layers, _, _ = _brute_layers(p, errs, post, 3)
        # k=1 四支完全对称: 4 种组合并列, 裁决为 (1,)
        bm, bid, cnt = layers[1]
        self.assertEqual(bid, (1,))
        self.assertEqual(cnt, 4)
        limit = bm  # 恰好达标(含容差)
        r = plan_reweigh(
            p,
            [{"tube_id": t, "mass_error_bound_g": 1.0} for t in range(1, 5)],
            post, limit, 3,
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["reweighs_required"], 1)
        self.assertEqual(r["reweighed_tubes"], [1])
        self.assertEqual(r["optimal_combination_count"], 4)
        self.assertTrue(r["tie_broken"])

    def test_infeasible_reports_best_effort_and_excess_no_false_pass(self):
        # 限值极低(0): 任何复称都不能让最不利 |U₁| 降到 0(标称值非零),
        # 必须不误报达标, 并给出 M 次内最优组合与超限量
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0), 9: (4, 49.0)})
        r = plan_reweigh(
            p,
            [{"tube_id": i, "mass_error_bound_g": 1.0} for i in range(1, 5)],
            unified_error_bound_g=0.01,
            u1_limit_g_mm=0.0,
            max_reweighs=3,
        )
        self.assertFalse(r["feasible"])
        self.assertIsNone(r["reweighs_required"])
        self.assertFalse(r["within_limit"])
        # 次数内最优(最多 3 次): 与分层暴力枚举的全局最优一致
        layers, _, _ = _brute_layers(p, {i: 1.0 for i in range(1, 5)}, 0.01, 3)
        global_best = min(
            (bm, bid) for bm, bid, _ in layers.values())
        self.assertAlmostEqual(
            r["after"]["worst_case"]["u1_mag_g_mm"], global_best[0], places=6)
        self.assertEqual(tuple(r["reweighed_tubes"]), global_best[1])
        final_mag = r["after"]["worst_case"]["u1_mag_g_mm"]
        self.assertGreater(final_mag, 0.0)
        self.assertAlmostEqual(r["excess_g_mm"], final_mag, places=9)
        self.assertIn("超限", r["reason"])
        # 与直接调用现有计算一致
        pls = p["result"]["placements"]
        direct = worst_case_u1(
            pls, 100.0,
            {tid: (0.01 if tid in global_best[1] else 1.0)
             for tid in range(1, 5)})["worst_case"]["u1_mag_g_mm"]
        self.assertAlmostEqual(final_mag, direct, places=9)

    def test_reweigh_only_lowers_error_bound_nominal_and_slot_unchanged(self):
        p = _plan(7, {0: (1, 50.0), 6: (2, 60.0), 9: (3, 40.0)})
        errs = {1: 1.5, 2: 0.8, 3: 0.2}
        r = plan_reweigh(
            p,
            [{"tube_id": t, "mass_error_bound_g": e} for t, e in errs.items()],
            unified_error_bound_g=0.3,
            u1_limit_g_mm=1.0e6,
            max_reweighs=2,
        )
        rows = {t["tube_id"]: t for t in r["tubes"]}
        # 候选只有 1、2(err>0.3); 3 号误差上界已低于统一值, 不进候选
        self.assertEqual(r["candidate_count"], 2)
        cand_ids = {c["tube_id"] for c in r["reweigh_candidates"]}
        self.assertEqual(cand_ids, {1, 2})
        # 复称管误差上界 = 0.3 = min(e_k, 0.3); 未复称管不变
        for tid, row in rows.items():
            self.assertEqual(row["nominal_mass_g"], {1: 50.0, 2: 60.0, 3: 40.0}[tid])
            self.assertEqual(row["slot"], {1: 0, 2: 6, 3: 9}[tid])
            if row["reweighed"]:
                self.assertAlmostEqual(row["error_after_g"], 0.3)
                self.assertAlmostEqual(
                    row["error_reduction_g"], errs[tid] - 0.3, places=9)
            else:
                self.assertEqual(row["error_after_g"], errs[tid])
                self.assertEqual(row["error_reduction_g"], 0.0)
        # 管 3 永不被复称
        self.assertNotIn(3, r["reweighed_tubes"])

    def test_matches_brute_force_random(self):
        rng = random.Random(20260924)
        for _ in range(120):
            n = rng.randint(1, 12)
            slots_n = 12
            slots = rng.sample(range(slots_n), n)
            masses = [round(rng.uniform(5.0, 60.0), 2) for _ in range(n)]
            placements = {s: (i + 1, masses[i]) for i, s in enumerate(slots)}
            radius = rng.choice([50.0, 100.0, 200.0])
            p = _plan(1, placements, slots_n=slots_n, radius=radius)
            errs = {
                i + 1: round(rng.uniform(0.0, min(2.0, masses[i])), 3)
                for i in range(n)
            }
            post = round(rng.uniform(0.0, 1.5), 3)
            M = rng.randint(0, 3)
            limit = rng.choice(
                [0.0, 100.0, 500.0, 1000.0, 3000.0, 8000.0, 20000.0, 1.0e12])
            layers, _, _ = _brute_layers(p, errs, post, M)
            r = plan_reweigh(
                p,
                [{"tube_id": t, "mass_error_bound_g": e} for t, e in errs.items()],
                post, limit, M,
            )
            winner_k = next(
                (k for k in sorted(layers)
                 if layers[k][0] <= limit + 1e-9 * max(1.0, limit)),
                None,
            )
            if winner_k is not None:
                bm, bid, cnt = layers[winner_k]
                self.assertTrue(r["feasible"])
                self.assertEqual(r["reweighs_required"], winner_k)
                self.assertEqual(tuple(r["reweighed_tubes"]), bid)
                self.assertAlmostEqual(
                    r["after"]["worst_case"]["u1_mag_g_mm"], bm, places=6)
                if winner_k > 0:
                    self.assertEqual(r["optimal_combination_count"], cnt)
            else:
                allbest = min(layers[k][0] for k in layers)
                self.assertFalse(r["feasible"])
                self.assertFalse(r["within_limit"])
                self.assertAlmostEqual(
                    r["after"]["worst_case"]["u1_mag_g_mm"], allbest, places=6)
                self.assertGreaterEqual(
                    r["excess_g_mm"], max(0.0, allbest - limit) - 1e-6)
            # 层结果与暴力一致
            for L in r["layers"]:
                bb = layers[L["reweigh_count"]]
                self.assertAlmostEqual(L["worst_u1_g_mm"], bb[0], places=6)
                self.assertEqual(tuple(L["reweighed_tubes"]), bb[1])

    def test_monotone_more_reweighs_never_worse(self):
        # 随机性抽查: 复称集合扩大时最不利 |U₁| 单调不增(zonotope 内含)
        rng = random.Random(11)
        p = _plan(1, {0: (1, 50.0), 2: (2, 53.0), 5: (3, 47.0), 8: (4, 51.0)})
        for _ in range(20):
            errs = {i: round(rng.uniform(0.1, 2.0), 3) for i in range(1, 5)}
            post = round(rng.uniform(0.0, 0.5), 3)
            r = plan_reweigh(
                p,
                [{"tube_id": t, "mass_error_bound_g": e} for t, e in errs.items()],
                post, 0.0, 3,
            )
            mags = [L["worst_u1_g_mm"] for L in r["layers"]]
            for a, b in zip(mags, mags[1:]):
                self.assertLessEqual(b, a + 1e-6)

    def test_max_reweighs_zero(self):
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0), 9: (4, 49.0)})
        # M=0: 只评估基线, 不达标即超限, 不给出复称管
        r = plan_reweigh(
            p,
            [{"tube_id": i, "mass_error_bound_g": 1.0} for i in range(1, 5)],
            unified_error_bound_g=0.01, u1_limit_g_mm=0.0, max_reweighs=0,
        )
        self.assertFalse(r["feasible"])
        self.assertEqual(r["reweighed_tubes"], [])
        self.assertEqual(r["evaluated_combinations"], 1)
        self.assertEqual(len(r["layers"]), 1)

    def test_validation_errors(self):
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0)})
        good_errs = [
            {"tube_id": 1, "mass_error_bound_g": 0.5},
            {"tube_id": 2, "mass_error_bound_g": 0.5},
        ]
        kw = dict(unified_error_bound_g=0.1, u1_limit_g_mm=1000.0,
                  max_reweighs=3)
        # 负统一上界 / 非有限
        with self.assertRaises(ValueError):
            plan_reweigh(p, good_errs, unified_error_bound_g=-0.1, **{
                k: v for k, v in kw.items() if k != "unified_error_bound_g"})
        with self.assertRaises(ValueError):
            plan_reweigh(p, good_errs, unified_error_bound_g=float("nan"), **{
                k: v for k, v in kw.items() if k != "unified_error_bound_g"})
        # 负限值
        with self.assertRaises(ValueError):
            plan_reweigh(p, good_errs, **{**kw, "u1_limit_g_mm": -1.0})
        # 次数越界
        with self.assertRaises(ValueError):
            plan_reweigh(p, good_errs, **{**kw, "max_reweighs": 4})
        with self.assertRaises(ValueError):
            plan_reweigh(p, good_errs, **{**kw, "max_reweighs": -1})
        # 误差上界为负
        with self.assertRaises(ValueError):
            plan_reweigh(
                p, [{"tube_id": 1, "mass_error_bound_g": -0.1},
                     {"tube_id": 2, "mass_error_bound_g": 0.5}], **kw)
        # 误差上界超过标称质量
        with self.assertRaises(ValueError):
            plan_reweigh(
                p, [{"tube_id": 1, "mass_error_bound_g": 99.0},
                     {"tube_id": 2, "mass_error_bound_g": 0.5}], **kw)
        # 管号不存在
        with self.assertRaises(ValueError):
            plan_reweigh(
                p, [{"tube_id": 1, "mass_error_bound_g": 0.5},
                     {"tube_id": 99, "mass_error_bound_g": 0.5}], **kw)
        # 重复管号
        with self.assertRaises(ValueError):
            plan_reweigh(
                p, [{"tube_id": 1, "mass_error_bound_g": 0.5},
                     {"tube_id": 1, "mass_error_bound_g": 0.3}], **kw)
        # 空方案
        with self.assertRaises(ValueError):
            plan_reweigh(_plan(1, {}), [], **kw)

    def test_too_many_tubes_rejected(self):
        placements = {i: (i + 1, 40.0 + (i % 5))
                      for i in range(MAX_TUBES + 1)}
        p = _plan(1, placements, slots_n=64)
        with self.assertRaises(ValueError):
            plan_reweigh(
                p,
                [{"tube_id": i + 1, "mass_error_bound_g": 0.5}
                 for i in range(MAX_TUBES + 1)],
                unified_error_bound_g=0.1, u1_limit_g_mm=1000.0,
                max_reweighs=MAX_REWEIGHS,
            )

    def test_missing_tube_errors_treated_as_zero(self):
        # 只给管 1 误差上界; 管 2 默认 0(无误差), 复称它无效
        p = _plan(1, {0: (1, 50.0), 6: (2, 60.0)})
        r = plan_reweigh(
            p,
            [{"tube_id": 1, "mass_error_bound_g": 1.0}],
            unified_error_bound_g=0.1, u1_limit_g_mm=1.0e12,
            max_reweighs=3,
        )
        self.assertEqual(r["candidate_count"], 1)
        self.assertEqual({c["tube_id"] for c in r["reweigh_candidates"]}, {1})
        rows = {t["tube_id"]: t for t in r["tubes"]}
        self.assertEqual(rows[2]["error_before_g"], 0.0)
        self.assertFalse(rows[2]["reweighed"])

    def test_read_only(self):
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0)})
        errs = [
            {"tube_id": 1, "mass_error_bound_g": 1.0},
            {"tube_id": 2, "mass_error_bound_g": 0.8},
            {"tube_id": 3, "mass_error_bound_g": 0.6},
        ]
        snap_p, snap_e = copy.deepcopy(p), copy.deepcopy(errs)
        plan_reweigh(p, errs, unified_error_bound_g=0.2,
                     u1_limit_g_mm=1000.0, max_reweighs=3)
        self.assertEqual(p, snap_p)
        self.assertEqual(errs, snap_e)

    def test_plan_info_and_note(self):
        p = _plan(9, {0: (1, 50.0)}, name="复称测试")
        r = plan_reweigh(
            p, [{"tube_id": 1, "mass_error_bound_g": 0.5}],
            unified_error_bound_g=0.1, u1_limit_g_mm=1.0e9,
            max_reweighs=3,
        )
        self.assertEqual(r["plan"]["id"], 9)
        self.assertEqual(r["plan"]["name"], "复称测试")
        self.assertEqual(r["unified_error_bound_g"], 0.1)
        self.assertEqual(r["u1_limit_g_mm"], 1.0e9)
        self.assertEqual(r["max_reweighs"], 3)
        self.assertIn("只读", r["note"])
        self.assertIn("zonotope", r["method"])
        # 复称前块复用现有计算结构
        self.assertIn("nominal", r["before"])
        self.assertIn("worst_case", r["before"])
        self.assertIn("actual_masses", r["after"])


if __name__ == "__main__":
    unittest.main()
