"""互斥试管隔离精确搜索测试: python -m unittest discover -s tests

核心断言:
- 互斥对两支试管不得落在按角度环形排序后相邻的物理槽位, 禁用槽仍占据
  相邻关系;
- 含互斥对时结果必须与独立的全排列暴力枚举一致(最小评分、平分时按管号
  升序的槽位序列裁决、合法摆放计数);
- 含互斥对限至多 7 支试管、10 个启用槽、12 对; 自互斥/重复/未知编号拒绝;
- 锁定管互斥相邻或无合法摆放时抛 ValueError(不可行), 与既有 API 约定
  一致(HTTP 400, 不落库);
- 无互斥对时沿用启发式求解; 结果与保存请求都保留互斥对。
"""
import itertools
import json
import math
import random
import unittest

from app.balancer import Solver, Slot, Tube, run_plan

TIE_TOL = 1e-9


def make_payload(tubes, pairs=(), disabled=(), n_slots=12, angles=None, **ov):
    if angles is None:
        angles = [360.0 * i / n_slots for i in range(n_slots)]
    p = {
        "slot_count": n_slots,
        "slots": [
            {"index": i, "angle": angles[i], "enabled": i not in disabled}
            for i in range(n_slots)
        ],
        "tubes": tubes,
        "radius_mm": 100.0,
        "rpm": 4000.0,
        "w1": 1.0,
        "w2": 1e-6,
        "restarts": 4,
        "chain_steps": 300,
        "mutex_pairs": [{"tube_a": a, "tube_b": b} for a, b in pairs],
    }
    p.update(ov)
    return p


def ring_adjacency(payload):
    order = sorted(payload["slots"], key=lambda s: (s["angle"] % 360, s["index"]))
    adj = {s["index"]: set() for s in payload["slots"]}
    m = len(order)
    for i in range(m):
        a, b = order[i]["index"], order[(i + 1) % m]["index"]
        if a != b:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def brute_force(payload):
    """全排列参考: (最优评分, 管号升序槽位序列, 合法摆放数); 无合法摆放返回 (None, None, 0)。"""
    slots = payload["slots"]
    tubes = sorted(payload["tubes"], key=lambda t: t["id"])
    pairs = [tuple(sorted((q["tube_a"], q["tube_b"])))
             for q in payload.get("mutex_pairs", [])]
    adj = ring_adjacency(payload)
    fix = {t["id"]: t["slot"] for t in tubes if t.get("locked")}
    for a, b in pairs:
        if a in fix and b in fix and fix[b] in adj[fix[a]]:
            return None, None, 0
    free_slots = [s["index"] for s in slots
                  if s["enabled"] and s["index"] not in fix.values()]
    free_t = [t for t in tubes if t["id"] not in fix]
    w2 = float(payload.get("w2", 1e-6))
    prev = {x["tube_id"]: x["slot"] for x in payload.get("previous", []) or []}
    move_weight = float(payload.get("move_weight", 0.0))
    best = best_key = None
    count = 0
    for comb in itertools.permutations(free_slots, len(free_t)):
        loc = dict(fix)
        loc.update({free_t[i]["id"]: comb[i] for i in range(len(free_t))})
        if any(loc[b] in adj[loc[a]] for a, b in pairs):
            continue
        count += 1
        u1 = u2 = 0j
        for t in tubes:
            a = math.radians(slots[loc[t["id"]]]["angle"])
            m = t["mass"]
            u1 += m * complex(math.cos(a), math.sin(a))
            u2 += m * complex(math.cos(2 * a), math.sin(2 * a))
        moved = sum(
            1 for t in tubes
            if t["id"] not in fix and prev.get(t["id"]) is not None
            and prev[t["id"]] != loc[t["id"]]
        )
        sc = abs(u1) ** 2 + w2 * abs(u2) ** 2 + move_weight * moved
        key = tuple(loc[t["id"]] for t in tubes)
        tol = TIE_TOL * (1.0 if best is None else max(1.0, abs(best)))
        if best is None or sc < best - tol:
            best, best_key = sc, key
        elif abs(sc - best) <= tol and key < best_key:
            best_key = key
    return best, best_key, count


def engine_loc(result):
    return {p["tube_id"]: p["slot"] for p in result["placements"]}


class TestMutexPairs(unittest.TestCase):
    def assertMatchesBrute(self, payload):
        result = run_plan(json.loads(json.dumps(payload)))
        loc = engine_loc(result)
        adj = ring_adjacency(payload)
        for q in payload["mutex_pairs"]:
            a, b = q["tube_a"], q["tube_b"]
            self.assertNotIn(loc[b], adj[loc[a]])
        best, best_key, count = brute_force(payload)
        self.assertGreater(count, 0)
        self.assertEqual(result["exact_search"]["evaluated_arrangements"], count)
        tubes_sorted = sorted(payload["tubes"], key=lambda t: t["id"])
        self.assertEqual(tuple(loc[t["id"]] for t in tubes_sorted), best_key)
        return result, loc, best

    def test_exact_matches_brute_force_basic(self):
        p = make_payload(
            [{"id": 1, "mass": 50}, {"id": 2, "mass": 50},
             {"id": 3, "mass": 51}, {"id": 4, "mass": 49}],
            pairs=((1, 2),), disabled=(10, 11),
        )
        r, loc, best = self.assertMatchesBrute(p)
        self.assertEqual(r["config"]["variant"], "exact-mutex")
        self.assertEqual(r["config"]["mutex_pair_count"], 1)
        self.assertEqual(r["exact_search"]["mutex_pairs"], [[1, 2]])
        # 评分与一阶残量自洽
        u1 = r["metrics"]["u1_mag_g_mm"] / 100.0
        self.assertAlmostEqual(u1 ** 2 + 1e-6 * (r["metrics"]["u2_mag_g_mm"] / 100.0) ** 2,
                               best, places=9)

    def test_disabled_slots_still_occupy_adjacency(self):
        # 仅槽 0、2 启用, 槽 1 禁用介于其间 -> 0/2 物理上不相邻, 可行
        p = make_payload(
            [{"id": 1, "mass": 50}, {"id": 2, "mass": 50}], pairs=((1, 2),),
        )
        p["slots"] = [{"index": i, "angle": i * 30, "enabled": i in (0, 2)}
                      for i in range(12)]
        r, loc, _ = self.assertMatchesBrute(p)
        self.assertEqual(set(loc.values()), {0, 2})

        # 仅槽 0、1 启用 -> 物理相邻, 2 支互斥管无合法摆放
        p2 = make_payload(
            [{"id": 1, "mass": 50}, {"id": 2, "mass": 50}], pairs=((1, 2),),
        )
        p2["slots"] = [{"index": i, "angle": i * 30, "enabled": i in (0, 1)}
                       for i in range(12)]
        self.assertEqual(brute_force(p2)[2], 0)
        with self.assertRaises(ValueError):
            run_plan(p2)

        # 禁用槽不缩短环: 槽 9、0 之间隔着禁用槽 10、11 时不再相邻
        p3 = make_payload(
            [{"id": 1, "mass": 50}, {"id": 2, "mass": 50},
             {"id": 3, "mass": 50}, {"id": 4, "mass": 50}],
            pairs=((1, 2),), disabled=(10, 11),
        )
        r3, loc3, _ = self.assertMatchesBrute(p3)
        adj3 = ring_adjacency(p3)
        self.assertNotIn(0, adj3[9])

    def test_locked_mutex_conflict_raises(self):
        p = make_payload(
            [{"id": 1, "mass": 50, "locked": True, "slot": 0},
             {"id": 2, "mass": 50, "locked": True, "slot": 1}],
            pairs=((1, 2),), disabled=(10, 11),
        )
        with self.assertRaises(ValueError):
            run_plan(p)
        # 锁定在非相邻槽则可行
        p["tubes"][1]["slot"] = 2
        r, _, _ = self.assertMatchesBrute(p)
        self.assertEqual(
            next(x for x in r["placements"] if x["tube_id"] == 1)["slot"], 0)

    def test_infeasible_when_free_tube_has_no_legal_slot(self):
        # 3 槽均布: 管 1 锁定 0、管 2 锁定 2, 管 3 与两者都互斥 -> 槽 1
        # 与 0、2 都相邻, 无合法摆放
        p = make_payload(
            [{"id": 1, "mass": 50, "locked": True, "slot": 0},
             {"id": 2, "mass": 50, "locked": True, "slot": 2},
             {"id": 3, "mass": 50}],
            pairs=((1, 3), (2, 3)), n_slots=3,
        )
        self.assertEqual(brute_force(p)[2], 0)
        with self.assertRaises(ValueError):
            run_plan(p)

    def test_limits_tubes_slots_pairs(self):
        ok_slots = [{"index": i, "angle": i * 30, "enabled": i not in (10, 11)}
                    for i in range(12)]
        # 8 支试管
        with self.assertRaises(ValueError):
            run_plan(make_payload(
                [{"id": i, "mass": 10} for i in range(1, 9)],
                pairs=((1, 2),), disabled=(10, 11)))
        # 11 个启用槽
        with self.assertRaises(ValueError):
            run_plan(make_payload(
                [{"id": 1, "mass": 10}, {"id": 2, "mass": 10}], pairs=((1, 2),),
                disabled=(11,)))
        # 13 对
        many = [(1, i) for i in range(2, 8)] + [(2, 3), (2, 4), (2, 5),
                                                (2, 6), (2, 7), (3, 4), (3, 5)]
        self.assertEqual(len(many), 13)
        with self.assertRaises(ValueError):
            run_plan(make_payload(
                [{"id": i, "mass": 10} for i in range(1, 8)],
                pairs=many, disabled=(10, 11)))

    def test_invalid_pair_shapes(self):
        base_tubes = [{"id": 1, "mass": 10}, {"id": 2, "mass": 10}]
        for pairs in (
            ((1, 1),),            # 自互斥
            ((1, 2), (2, 1)),     # 无序重复
            ((1, 9),),            # 未知编号
        ):
            with self.assertRaises(ValueError):
                run_plan(make_payload(base_tubes, pairs=pairs, disabled=(10, 11)))

    def test_incremental_exact_preserves_previous(self):
        p0 = make_payload(
            [{"id": i, "mass": 50} for i in range(1, 5)],
            pairs=((1, 2), (3, 4)), disabled=(10, 11), move_weight=5.0,
        )
        r0 = run_plan(p0)
        p1 = json.loads(json.dumps(p0))
        p1["tubes"].append({"id": 5, "mass": 50})
        p1["previous"] = [
            {"tube_id": x["tube_id"], "slot": x["slot"]} for x in r0["placements"]
        ]
        self.assertMatchesBrute(p1)
        r1 = run_plan(json.loads(json.dumps(p1)))
        before = {x["tube_id"]: x["slot"] for x in r0["placements"]}
        after = {x["tube_id"]: x["slot"] for x in r1["placements"]}
        moved = set(r1["moved_tube_ids"])
        for tid, slot in before.items():
            if tid not in moved:
                self.assertEqual(after[tid], slot)
        # 末步快照等于最终指标, 且所有快照互斥约束成立
        last = r1["steps"][-1]
        self.assertAlmostEqual(last["vectors"]["u1_mag"],
                               r1["metrics"]["u1_mag_g_mm"], places=6)
        adj = ring_adjacency(p1)
        for st in r1["steps"]:
            sm = {x["tube_id"]: x["slot"] for x in st["placements"]
                  if x["slot"] is not None}
            for a, b in ((1, 2), (3, 4)):
                if a in sm and b in sm:
                    self.assertNotIn(sm[b], adj[sm[a]])

    def test_result_and_payload_keep_mutex_pairs(self):
        p = make_payload(
            [{"id": 2, "mass": 50}, {"id": 1, "mass": 50},
             {"id": 3, "mass": 49}],
            pairs=((2, 1),), disabled=(10, 11),  # 乱序输入, 结果规范化为 [1,2]
        )
        r = run_plan(p)
        self.assertEqual(r["exact_search"]["mutex_pairs"], [[1, 2]])
        partners = {x["tube_id"]: x["mutex_with"] for x in r["placements"]}
        self.assertEqual(partners[1], [2])
        self.assertEqual(partners[2], [1])
        self.assertEqual(partners[3], [])
        # 物理相邻边包含禁用槽连接
        edges = {tuple(e) for e in r["exact_search"]["adjacent_slot_pairs"]}
        self.assertIn((9, 10), edges)
        self.assertIn((10, 11), edges)
        self.assertIn((0, 11), edges)

    def test_all_locked_feasible_exact_path(self):
        # 全部锁定且互不相邻: 唯一摆放必须保留, 叶子数 1
        p = make_payload(
            [{"id": 1, "mass": 50, "locked": True, "slot": 0},
             {"id": 2, "mass": 49, "locked": True, "slot": 2}],
            pairs=((1, 2),), disabled=(10, 11),
        )
        r, loc, _ = self.assertMatchesBrute(p)
        self.assertEqual(loc, {1: 0, 2: 2})
        self.assertEqual(r["exact_search"]["evaluated_arrangements"], 1)

    def test_no_free_tubes_locked_pair_conflict_only(self):
        # 全部锁定且相邻 -> 结构性不可行(不走 DFS)
        p = make_payload(
            [{"id": 1, "mass": 50, "locked": True, "slot": 3},
             {"id": 2, "mass": 50, "locked": True, "slot": 4}],
            pairs=((1, 2),), disabled=(10, 11),
        )
        with self.assertRaises(ValueError):
            run_plan(p)

    def test_replay_steps_mutex_annotations(self):
        p = make_payload(
            [{"id": 1, "mass": 50}, {"id": 2, "mass": 50},
             {"id": 3, "mass": 50}],
            pairs=((1, 2),), disabled=(10, 11),
        )
        r = run_plan(p)
        exact_steps = [s for s in r["steps"] if s["kind"] == "exact"]
        self.assertEqual(len(exact_steps), 3)
        self.assertEqual([s["refs"] for s in exact_steps], [[1], [2], [3]])
        final = r["steps"][-1]
        self.assertIn(str(r["exact_search"]["evaluated_arrangements"]),
                      final["reason"])

    def test_compare_detects_mutex_difference(self):
        from app.compare import compare_plans
        slots = [{"index": i, "angle": i * 30, "enabled": i not in (10, 11)}
                 for i in range(12)]
        tubes = [{"id": i, "mass": 50} for i in range(1, 4)]

        def saved(pairs, name, pid):
            payload = make_payload(tubes, pairs=pairs, disabled=(10, 11))
            payload["name"] = name
            res = run_plan(payload)
            return {
                "id": pid, "name": name, "created_at": "t",
                "request": payload, "result": res,
            }

        a = saved(((1, 2),), "A", 1)
        b = saved((), "B", 2)
        cmp = compare_plans(a, b)
        self.assertFalse(cmp["same_conditions"])
        self.assertIn("solver", cmp["differing_groups"])
        labels = [d["label"] for d in cmp["input_diff"]["solver"]]
        self.assertIn("互斥试管对", labels)

        # 互斥对相同 -> 不产生差异
        c = saved(((1, 2),), "C", 3)
        cmp2 = compare_plans(a, c)
        self.assertTrue(cmp2["same_conditions"], cmp2["input_diff"]["solver"])

    def test_no_mutex_pair_uses_heuristic(self):
        p = make_payload(
            [{"id": 1, "mass": 50}, {"id": 2, "mass": 50},
             {"id": 3, "mass": 51}, {"id": 4, "mass": 49}],
        )
        r = run_plan(p)
        self.assertNotIn("exact_search", r)
        self.assertEqual(r["config"]["mutex_pair_count"], 0)
        self.assertNotEqual(r["config"]["variant"], "exact-mutex")

    def test_fuzz_against_brute_force(self):
        rng = random.Random(20260924)
        checked = 0
        for _ in range(120):
            n_slots = rng.choice([3, 4, 5, 6, 8, 10])
            disabled = set(rng.sample(range(n_slots), rng.randint(0, n_slots - 2)))
            n_en = n_slots - len(disabled)
            if n_en > 10:
                continue
            n_tube = rng.randint(2, min(7, n_en))
            tubes = [{"id": i, "mass": rng.choice([10, 49, 50, 51, 100])}
                     for i in range(1, n_tube + 1)]
            k = rng.randint(1, min(3, n_tube * (n_tube - 1) // 2))
            cand = list(itertools.combinations(range(1, n_tube + 1), 2))
            rng.shuffle(cand)
            pairs = cand[:k]
            enabled_ids = [i for i in range(n_slots) if i not in disabled]
            for t in tubes:
                if rng.random() < 0.1:
                    t["locked"] = True
                    t["slot"] = rng.choice(enabled_ids)
            # 跳过同槽多锁等无效输入(由既有校验覆盖)
            locked_slots = [t["slot"] for t in tubes if t.get("locked")]
            if len(set(locked_slots)) != len(locked_slots):
                continue
            p = make_payload(tubes, pairs=pairs, disabled=disabled, n_slots=n_slots)
            try:
                r = run_plan(json.loads(json.dumps(p)))
            except ValueError:
                self.assertEqual(brute_force(p)[2], 0)
                continue
            self.assertMatchesBrute(p)
            checked += 1
        self.assertGreater(checked, 50)


class TestRingAdjacency(unittest.TestCase):
    def _solver(self, angles_enabled):
        slots = [Slot(i, ang, en) for i, (ang, en) in enumerate(angles_enabled)]
        return Solver(slots=slots, tubes=[Tube(1, 1.0)], mutex_pairs=[])

    def test_two_slots_single_edge(self):
        s = self._solver([(0.0, True), (180.0, True)])
        adj = s._ring_adjacency()
        self.assertEqual(adj[0], {1})
        self.assertEqual(adj[1], {0})

    def test_irregular_order_uses_angles_not_indices(self):
        s = self._solver([(350.0, True), (10.0, True), (120.0, True)])
        adj = s._ring_adjacency()
        # 角度顺序 10(槽1) - 120(槽2) - 350(槽0) 环绕
        self.assertEqual(adj[0], {1, 2})
        self.assertEqual(adj[1], {0, 2})
        self.assertEqual(adj[2], {0, 1})

    def test_single_slot_no_self_edge(self):
        s = self._solver([(0.0, True)])
        self.assertEqual(s._ring_adjacency(), {0: set()})


if __name__ == "__main__":
    unittest.main()
