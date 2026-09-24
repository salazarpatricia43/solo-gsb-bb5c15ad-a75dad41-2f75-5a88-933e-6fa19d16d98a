"""失效槽位应急迁移测试: python -m unittest discover -s tests

核心断言: 按移动管数分层的完全枚举结果(最少移动数、最终 |U₁|、最优摆放
条数与按管号升序目标槽位序列的并列裁决)必须与独立的全枚举参考实现一致;
锁定试管不得移动、失效槽位不得占用; 无可行摆放时明确说明并给出移动上限
内的最优对照摆放; 超过 10 支试管、16 个启用槽位或移动上限 0~3 之外的
方案必须拒绝; 规划过程不修改入参(只读)。
"""
import copy
import itertools
import math
import random
import unittest

from app.evacuation import plan_slot_evacuation


def _plan(pid, placements, radius=100.0, slots_n=12, enabled=None, name=None):
    """placements: {槽号: (试管编号, 质量, 是否锁定)}; 角度按槽号均布。"""
    if enabled is None:
        enabled = set(range(slots_n))
    slot_state = [
        {
            "slot": i,
            "angle": 360.0 * i / slots_n,
            "enabled": i in enabled,
            "tube_id": next(
                (t for s, (t, m, lk) in placements.items() if s == i), None
            ),
            "locked": next(
                (lk for s, (t, m, lk) in placements.items() if s == i), False
            ),
        }
        for i in range(slots_n)
    ]
    pls = [
        {
            "tube_id": t,
            "mass": m,
            "slot": s,
            "angle": 360.0 * s / slots_n,
            "locked": lk,
        }
        for s, (t, m, lk) in placements.items()
    ]
    return {
        "id": pid,
        "name": name or f"方案{pid}",
        "created_at": "2026-09-24T00:00:00+00:00",
        "request": {"radius_mm": radius},
        "result": {
            "config": {"radius_mm": radius},
            "slot_state": slot_state,
            "placements": pls,
            "metrics": {"u1_mag_g_mm": 0.0},
        },
    }


def _brute(plan, failed, limit, max_moves):
    """独立全枚举参考(基于 itertools.permutations)。

    返回 ("ok", 最少移动数, 最终|U₁|, 目标槽序列, 最优摆放条数)、
    ("no", 对照摆放(|U₁|, 目标槽序列, 移动数) 或 None) 或 None(结构性不可行)。
    """
    res = plan["result"]
    radius = res["config"]["radius_mm"]
    info = {s["slot"]: s for s in res["slot_state"]}
    tubes = sorted(
        [p for p in res["placements"] if p["slot"] is not None],
        key=lambda p: p["tube_id"],
    )
    n = len(tubes)
    fset = set(failed)
    if any(t["locked"] and t["slot"] in fset for t in tubes):
        return None
    movable = [i for i, t in enumerate(tubes) if not t["locked"]]
    forced = [i for i in movable if tubes[i]["slot"] in fset]
    if len(forced) > max_moves:
        return None
    occ = {t["slot"] for t in tubes}
    empty = sorted(
        s for s, inf in info.items()
        if inf["enabled"] and s not in fset and s not in occ
    )

    def vec(i, slot):
        a = math.radians(info[slot]["angle"])
        return radius * tubes[i]["mass"] * complex(math.cos(a), math.sin(a))

    ovec = [vec(i, tubes[i]["slot"]) for i in range(n)]

    def key_of(assign):
        return tuple(assign.get(i, tubes[i]["slot"]) for i in range(n))

    best_eff = None
    for k in range(len(forced), max_moves + 1):
        leaves = []
        for T in itertools.combinations(movable, k):
            if not set(forced) <= set(T):
                continue
            tset = set(T)
            base = sum((ovec[i] for i in range(n) if i not in tset), 0j)
            pool = sorted(
                set(empty)
                | {tubes[i]["slot"] for i in T if i not in forced}
            )
            for tgt in itertools.permutations(pool, k):
                if any(tgt[x] == tubes[T[x]]["slot"] for x in range(k)):
                    continue
                u = base + sum((vec(T[x], tgt[x]) for x in range(k)), 0j)
                leaves.append((abs(u), key_of(dict(zip(T, tgt)))))
        if not leaves:
            continue
        bm = min(m for m, _ in leaves)
        tol = 1e-9 * max(1.0, bm)
        btied = [(m, kk) for m, kk in leaves if m <= bm + tol]
        bkey = min(kk for _, kk in btied)
        bmag = next(m for m, kk in btied if kk == bkey)
        if best_eff is None or bmag < best_eff[0] - 1e-9:
            best_eff = (bmag, bkey, k)
        feas = [
            (m, kk) for m, kk in leaves
            if m <= limit + 1e-9 * max(1.0, limit)
        ]
        if feas:
            fm = min(m for m, _ in feas)
            ftol = 1e-9 * max(1.0, fm)
            ftied = [(m, kk) for m, kk in feas if m <= fm + ftol]
            fkey = min(kk for _, kk in ftied)
            fmag = next(m for m, kk in ftied if kk == fkey)
            return ("ok", k, fmag, fkey, len(ftied))
    return ("no", best_eff)


class TestSlotEvacuation(unittest.TestCase):
    def test_single_forced_move_nearest_slot(self):
        # 对径等质量, 槽 0 失效: 管 1 必须迁走; 1 次移动最近的有效空槽为
        # 槽 1(30°)或槽 11(330°), |U₁| = 100·|50·e^{i30°}+50·e^{i180°}|,
        # 并列按目标槽序列升序取槽 1
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, False)})
        r = plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=3000.0, max_moves=1)
        self.assertTrue(r["feasible"])
        self.assertTrue(r["within_limit"])
        self.assertEqual(r["moves_required"], 1)
        self.assertEqual(r["optimal_arrangement_count"], 2)
        self.assertTrue(r["tie_broken"])
        self.assertEqual(len(r["moves"]), 1)
        mv = r["moves"][0]
        self.assertEqual((mv["tube_id"], mv["from_slot"], mv["to_slot"]),
                         (1, 0, 1))
        expect = 100.0 * abs(
            50.0 * complex(math.cos(math.radians(30)),
                           math.sin(math.radians(30))) - 50.0
        )
        self.assertAlmostEqual(r["final_u1"]["u1_mag_g_mm"], expect, places=6)
        self.assertAlmostEqual(r["margin_g_mm"], 3000.0 - expect, places=6)
        self.assertEqual(r["excess_g_mm"], 0.0)
        # 逐管明细: 管 2 未动, 失效槽未被占用
        rows = {t["tube_id"]: t for t in r["tubes"]}
        self.assertEqual(rows[1]["final_slot"], 1)
        self.assertTrue(rows[1]["moved"])
        self.assertEqual(rows[2]["final_slot"], 6)
        self.assertFalse(rows[2]["moved"])
        self.assertNotIn(0, {t["final_slot"] for t in r["tubes"]})
        self.assertAlmostEqual(r["original_u1"]["u1_mag_g_mm"], 0.0, places=9)

    def test_two_move_perfect_swap(self):
        # 限值 0: 1 次移动无法完全平衡, 2 次移动可组成新的对径对
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, False)})
        r = plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=0.0, max_moves=3)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["moves_required"], 2)
        self.assertAlmostEqual(r["final_u1"]["u1_mag_g_mm"], 0.0, places=7)
        # 完美对: {1,7},{2,8},{3,9},{4,10},{5,11} × 两种分配 = 10 组并列,
        # 目标槽序列升序最小为 (1, 7): 管 1→槽 1, 管 2→槽 7
        self.assertEqual(r["optimal_arrangement_count"], 10)
        got = [(m["tube_id"], m["to_slot"]) for m in r["moves"]]
        self.assertEqual(got, [(1, 1), (2, 7)])

    def test_empty_failed_slot_zero_moves(self):
        # 失效槽本来空闲且原 |U₁| 已满足限值: 0 次移动即最优
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, False)})
        r = plan_slot_evacuation(p, failed_slots=[3],
                                 u1_limit_g_mm=0.0, max_moves=0)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["moves_required"], 0)
        self.assertEqual(r["optimal_arrangement_count"], 1)
        self.assertFalse(r["tie_broken"])
        self.assertEqual(r["moves"], [])
        self.assertAlmostEqual(r["final_u1"]["u1_mag_g_mm"], 0.0, places=9)

    def test_infeasible_limit_with_best_effort(self):
        # 限值 1300 < 1 次移动可达的最小 2588.19: 不可行, 对照为 1 次移动
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, False)})
        r = plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=1300.0, max_moves=1)
        self.assertFalse(r["feasible"])
        self.assertFalse(r["within_limit"])
        self.assertIsNone(r["moves_required"])
        self.assertIn("没有", r["reason"])
        be = r["best_effort"]
        self.assertEqual(be["moves_used"], 1)
        self.assertAlmostEqual(be["final_u1"]["u1_mag_g_mm"],
                               2588.1904510252075, places=6)
        self.assertAlmostEqual(r["excess_g_mm"],
                               2588.1904510252075 - 1300.0, places=6)
        # 顶层同样给出对照摆放
        self.assertEqual(r["moves"][0]["to_slot"], 1)

    def test_locked_on_failed_slot_infeasible(self):
        p = _plan(1, {0: (1, 50.0, True), 6: (2, 50.0, False)})
        r = plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=1e9, max_moves=3)
        self.assertFalse(r["feasible"])
        self.assertIsNone(r["final_u1"])
        self.assertIsNone(r["best_effort"])
        self.assertEqual(r["locked_on_failed_slots"][0]["tube_id"], 1)
        self.assertIn("锁定试管", r["reason"])
        # 逐管明细目标槽为空(不存在任何摆放)
        self.assertTrue(all(t["final_slot"] is None for t in r["tubes"]))

    def test_forced_moves_exceed_cap(self):
        p = _plan(1, {0: (1, 50.0, False), 2: (2, 50.0, False),
                      4: (3, 50.0, False), 8: (4, 50.0, False)},
                  slots_n=16)
        r = plan_slot_evacuation(p, failed_slots=[0, 2],
                                 u1_limit_g_mm=100.0, max_moves=1)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["forced_move_count"], 2)
        self.assertIsNone(r["best_effort"])
        self.assertIn("超过最多可移动管数", r["reason"])

    def test_no_valid_target_slot(self):
        # 唯一非锁定管必须迁移, 但其余槽位全部禁用: 无合法摆放
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, True)},
                  enabled={0, 6})
        r = plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=1e9, max_moves=1)
        self.assertFalse(r["feasible"])
        self.assertIsNone(r["best_effort"])
        self.assertIn("有效空槽不足", r["reason"])

    def test_locked_tubes_never_move(self):
        # 锁定管不在失效槽上: 必须保持原槽, 且其槽位不可作为迁移目标
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, True),
                      3: (3, 30.0, False)})
        r = plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=1e9, max_moves=3)
        self.assertTrue(r["feasible"])
        rows = {t["tube_id"]: t for t in r["tubes"]}
        self.assertEqual(rows[2]["final_slot"], 6)
        self.assertFalse(rows[2]["moved"])
        self.assertNotIn(6, {m["to_slot"] for m in r["moves"]})

    def test_matches_brute_force_random(self):
        rng = random.Random(20260924)
        for _ in range(60):
            slots_n = rng.randint(6, 16)
            enabled = set(rng.sample(
                range(slots_n), rng.randint(max(3, slots_n - 4), slots_n)))
            n = rng.randint(1, min(8, len(enabled)))
            avail = sorted(enabled)
            rng.shuffle(avail)
            placements = {
                avail[i]: (i + 1, rng.uniform(5.0, 60.0), rng.random() < 0.3)
                for i in range(n)
            }
            radius = rng.uniform(50.0, 200.0)
            plan = _plan(1, placements, radius=radius, slots_n=slots_n,
                         enabled=enabled)
            failed = rng.sample(sorted(enabled),
                                rng.randint(1, min(3, len(enabled) - 1)))
            max_moves = rng.randint(0, 3)
            limit = rng.choice([0.0, rng.uniform(0.0, 8000.0)])
            got = plan_slot_evacuation(plan, failed_slots=failed,
                                       u1_limit_g_mm=limit,
                                       max_moves=max_moves)
            want = _brute(plan, failed, limit, max_moves)
            if want is None:
                self.assertFalse(got["feasible"])
                self.assertIsNone(got["final_u1"])
                continue
            if want[0] == "ok":
                _, k, mag, key, cnt = want
                self.assertTrue(got["feasible"])
                self.assertEqual(got["moves_required"], k)
                self.assertAlmostEqual(got["final_u1"]["u1_mag_g_mm"], mag,
                                       delta=1e-6 * max(1.0, mag))
                self.assertEqual(got["optimal_arrangement_count"], cnt)
                got_key = tuple(t["final_slot"] for t in got["tubes"])
                self.assertEqual(got_key, key)
                # 约束复核: 失效槽未占用、锁定管未动、移动数一致
                for t in got["tubes"]:
                    self.assertNotIn(t["final_slot"], set(failed))
                    if t["locked"]:
                        self.assertEqual(t["final_slot"], t["original_slot"])
                self.assertEqual(
                    sum(1 for t in got["tubes"] if t["moved"]), k)
            else:
                self.assertFalse(got["feasible"])
                be = want[1]
                if be is None:
                    self.assertIsNone(got["best_effort"])
                else:
                    bmag, bkey, bk = be
                    self.assertEqual(got["best_effort"]["moves_used"], bk)
                    self.assertAlmostEqual(
                        got["best_effort"]["final_u1"]["u1_mag_g_mm"], bmag,
                        delta=1e-6 * max(1.0, bmag))
                    got_be_key = tuple(
                        t["final_slot"] for t in got["best_effort"]["tubes"])
                    self.assertEqual(got_be_key, bkey)

    def test_rejections(self):
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, False)})
        # 空失效槽列表 / 不存在槽位 / 重复槽位
        for bad in ([], [99], [0, 0]):
            with self.assertRaises(ValueError):
                plan_slot_evacuation(p, failed_slots=bad,
                                     u1_limit_g_mm=1.0, max_moves=1)
        # 原本禁用的槽位不能作为新失效槽
        pd = _plan(1, {0: (1, 50.0, False)}, enabled=set(range(12)) - {5})
        with self.assertRaises(ValueError):
            plan_slot_evacuation(pd, failed_slots=[5],
                                 u1_limit_g_mm=1.0, max_moves=1)
        # 负限值 / 非有限限值 / 移动上限越界
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=-1.0, max_moves=1)
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p, failed_slots=[0],
                                 u1_limit_g_mm=float("nan"), max_moves=1)
        for bad_m in (-1, 4):
            with self.assertRaises(ValueError):
                plan_slot_evacuation(p, failed_slots=[0],
                                     u1_limit_g_mm=1.0, max_moves=bad_m)
        # 空方案
        with self.assertRaises(ValueError):
            plan_slot_evacuation(_plan(1, {}), failed_slots=[0],
                                 u1_limit_g_mm=1.0, max_moves=1)
        # 超过 10 支试管
        p11 = _plan(1, {i: (i + 1, 10.0, False) for i in range(11)},
                    slots_n=16)
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p11, failed_slots=[15],
                                 u1_limit_g_mm=1.0, max_moves=1)
        # 超过 16 个启用槽位
        p17 = _plan(1, {i: (i + 1, 10.0, False) for i in range(5)},
                    slots_n=24)
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p17, failed_slots=[23],
                                 u1_limit_g_mm=1.0, max_moves=1)
        # 摆放明细非法: 重复试管 / 重复槽位 / 质量非正 / 缺角度
        p_dup = _plan(1, {0: (1, 50.0, False), 1: (1, 40.0, False)})
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p_dup, failed_slots=[2],
                                 u1_limit_g_mm=1.0, max_moves=1)
        p_slot = _plan(1, {0: (1, 50.0, False), 1: (2, 40.0, False)})
        p_slot["result"]["placements"][1]["slot"] = 0
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p_slot, failed_slots=[2],
                                 u1_limit_g_mm=1.0, max_moves=1)
        p_mass = _plan(1, {0: (1, 0.0, False)})
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p_mass, failed_slots=[2],
                                 u1_limit_g_mm=1.0, max_moves=1)
        p_ang = _plan(1, {0: (1, 50.0, False)})
        p_ang["result"]["placements"][0]["angle"] = None
        with self.assertRaises(ValueError):
            plan_slot_evacuation(p_ang, failed_slots=[2],
                                 u1_limit_g_mm=1.0, max_moves=1)

    def test_read_only(self):
        p = _plan(1, {0: (1, 50.0, False), 6: (2, 50.0, True),
                      3: (3, 30.0, False)})
        snap = copy.deepcopy(p)
        plan_slot_evacuation(p, failed_slots=[0], u1_limit_g_mm=100.0,
                             max_moves=3)
        self.assertEqual(p, snap)

    def test_plan_info_and_note(self):
        p = _plan(9, {0: (1, 50.0, False)}, name="迁移测试")
        r = plan_slot_evacuation(p, failed_slots=[3],
                                 u1_limit_g_mm=1e9, max_moves=2)
        self.assertEqual(r["plan"]["id"], 9)
        self.assertEqual(r["plan"]["name"], "迁移测试")
        self.assertIn("只读", r["note"])
        self.assertIn("目标槽位序列", r["method"])
        self.assertEqual(r["failed_slots"][0]["slot"], 3)
        self.assertIsNone(r["failed_slots"][0]["occupied_by_tube"])
        self.assertEqual(r["radius_mm"], 100.0)
        self.assertEqual(r["max_moves"], 2)
        self.assertEqual(r["u1_limit_g_mm"], 1e9)


if __name__ == "__main__":
    unittest.main()
