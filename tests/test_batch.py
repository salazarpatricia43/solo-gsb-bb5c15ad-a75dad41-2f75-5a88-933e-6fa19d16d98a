"""批次换装顺序规划测试: python -m unittest discover -s tests

核心断言: Held-Karp 子集 DP 求得的最小总操作数、最优次序与最优次序条数,
必须与全排列暴力枚举一致(含同成本时按方案 ID 序列升序裁决); 逐批卸装
清单的卸下/装入计数之和等于累计操作数; 几何或同编号试管质量不一致必须
拒绝; 规划过程不修改入参(只读)。
"""
import copy
import itertools
import random
import unittest

from app.batch import plan_batch_sequence


def _slots(n=12, disabled=()):
    return [
        {"index": i, "angle": i * (360.0 / n), "enabled": i not in disabled}
        for i in range(n)
    ]


def _plan(pid, placements, radius=100.0, slots=None, name=None):
    """placements: {槽号: (试管编号, 质量)}。"""
    if slots is None:
        slots = _slots()
    return {
        "id": pid,
        "name": name or f"方案{pid}",
        "created_at": "2026-09-24T00:00:00+00:00",
        "request": {
            "slot_count": len(slots),
            "slots": slots,
            "radius_mm": radius,
        },
        "result": {
            "placements": [
                {"tube_id": t, "slot": s, "mass": m, "angle": slots[s]["angle"]}
                for s, (t, m) in placements.items()
            ],
        },
    }


def _brute_best(plans):
    """全排列参考实现: (最小总操作数, 升序裁决后的方案 ID 序列, 最优次序条数)。"""
    occ = []
    for p in plans:
        m = {}
        for pl in p["result"]["placements"]:
            if pl["slot"] is not None:
                m[int(pl["slot"])] = int(pl["tube_id"])
        occ.append(m)
    ids = [p["id"] for p in plans]
    best = None
    best_seq = None
    count = 0
    for perm in itertools.permutations(range(len(plans))):
        total = 0
        prev = {}
        for idx in perm:
            cur = occ[idx]
            keep = sum(1 for s, t in prev.items() if cur.get(s) == t)
            total += len(prev) + len(cur) - 2 * keep
            prev = cur
        seq = tuple(ids[i] for i in perm)
        if best is None or total < best:
            best, best_seq, count = total, seq, 1
        elif total == best:
            count += 1
            best_seq = min(best_seq, seq)
    return best, list(best_seq), count


class TestBatchSequence(unittest.TestCase):
    def test_optimal_order_unique(self):
        # 手工核算: 最小总操作数 7, 唯一最优次序 [3, 1, 2]
        p1 = _plan(1, {0: (1, 50.0), 1: (2, 50.0)})
        p2 = _plan(2, {0: (1, 50.0), 1: (2, 50.0), 2: (3, 40.0)})
        p3 = _plan(3, {6: (4, 30.0), 7: (5, 30.0)})
        r = plan_batch_sequence([p1, p2, p3])
        self.assertEqual(r["total_operations"], 7)
        self.assertEqual(r["order"], [3, 1, 2])
        self.assertEqual(r["optimal_order_count"], 1)
        self.assertFalse(r["tie_broken"])
        self.assertEqual(r["plan_count"], 3)
        self.assertEqual([p["id"] for p in r["plans"]], [3, 1, 2])
        # 逐批: 批次1 装 2; 批次2 卸 2 装 2; 批次3 装 1(管 1/2 保留)
        b = r["batches"]
        self.assertEqual([x["operations"] for x in b], [2, 4, 1])
        self.assertEqual([x["cumulative_operations"] for x in b], [2, 6, 7])
        self.assertEqual(b[0]["unload"], [])
        self.assertEqual({x["tube_id"] for x in b[0]["load"]}, {4, 5})
        self.assertEqual({x["tube_id"] for x in b[1]["unload"]}, {4, 5})
        self.assertEqual({x["tube_id"] for x in b[1]["load"]}, {1, 2})
        self.assertEqual({x["tube_id"] for x in b[2]["load"]}, {3})
        self.assertEqual({x["tube_id"] for x in b[2]["kept"]}, {1, 2})
        self.assertEqual(b[2]["kept_count"], 2)
        # 清单条目带槽位/角度/质量
        row = next(x for x in b[0]["load"] if x["tube_id"] == 4)
        self.assertEqual(row["slot"], 6)
        self.assertAlmostEqual(row["angle"], 180.0, places=9)
        self.assertEqual(row["mass"], 30.0)

    def test_tie_break_by_plan_id_sequence(self):
        # 最小成本 3 的次序有两条: [2,9,5] 与 [5,9,2], 取 ID 序列升序
        p2 = _plan(2, {6: (2, 50.0)})
        p5 = _plan(5, {0: (1, 50.0)})
        p9 = _plan(9, {0: (1, 50.0), 6: (2, 50.0)})
        r = plan_batch_sequence([p9, p2, p5])  # 传入顺序打乱不影响结果
        self.assertEqual(r["total_operations"], 3)
        self.assertEqual(r["optimal_order_count"], 2)
        self.assertTrue(r["tie_broken"])
        self.assertEqual(r["order"], [2, 9, 5])

    def test_move_same_tube_costs_two_ops(self):
        # 同一支试管换槽 = 卸下 + 装入 = 2 次操作
        p1 = _plan(1, {0: (1, 50.0)})
        p2 = _plan(2, {3: (1, 50.0)})
        r = plan_batch_sequence([p1, p2])
        self.assertEqual(r["total_operations"], 3)  # 装 1 + 卸 1 + 装 1
        b = r["batches"][1]
        self.assertEqual(b["unload_count"], 1)
        self.assertEqual(b["load_count"], 1)
        self.assertEqual(b["kept_count"], 0)
        self.assertEqual(b["unload"][0]["slot"], 0)
        self.assertEqual(b["load"][0]["slot"], 3)

    def test_empty_plan_allowed(self):
        # 空方案先执行(0 次)再装 2 支, 优于先装后全卸(4 次)
        p1 = _plan(1, {})
        p2 = _plan(2, {0: (1, 50.0), 1: (2, 50.0)})
        r = plan_batch_sequence([p1, p2])
        self.assertEqual(r["order"], [1, 2])
        self.assertEqual(r["total_operations"], 2)
        self.assertEqual(r["batches"][0]["operations"], 0)

    def test_random_against_brute_force(self):
        rng = random.Random(20260924)
        for _ in range(40):
            k = rng.randint(2, 6)
            n_slots = 8
            slots = _slots(n_slots)
            ids = rng.sample(range(1, 100), k)
            plans = []
            for pid in ids:
                n_tubes = rng.randint(1, 5)
                occ_slots = rng.sample(range(n_slots), n_tubes)
                tube_ids = rng.sample(range(1, 50), n_tubes)
                pl = {s: (t, 50.0) for s, t in zip(occ_slots, tube_ids)}
                plans.append(_plan(pid, pl, slots=slots))
            r = plan_batch_sequence(plans)
            best, seq, cnt = _brute_best(plans)
            self.assertEqual(r["total_operations"], best)
            self.assertEqual(r["order"], seq)
            self.assertEqual(r["optimal_order_count"], cnt)
            self.assertEqual(sum(b["operations"] for b in r["batches"]), best)
            self.assertEqual(r["batches"][-1]["cumulative_operations"], best)
            for b in r["batches"]:
                self.assertEqual(
                    b["operations"], b["unload_count"] + b["load_count"])
                self.assertEqual(b["unload_count"], len(b["unload"]))
                self.assertEqual(b["load_count"], len(b["load"]))

    def test_read_only_inputs_unchanged(self):
        p1 = _plan(1, {0: (1, 50.0), 2: (2, 40.0)})
        p2 = _plan(2, {1: (3, 45.0)})
        p3 = _plan(3, {0: (1, 50.0), 5: (4, 35.0)})
        plans = [p1, p2, p3]
        snap = copy.deepcopy(plans)
        plan_batch_sequence(plans)
        self.assertEqual(plans, snap)


class TestBatchValidation(unittest.TestCase):
    def test_plan_count_bounds(self):
        with self.assertRaises(ValueError):
            plan_batch_sequence([_plan(1, {0: (1, 50.0)})])
        with self.assertRaises(ValueError):
            plan_batch_sequence(
                [_plan(i, {0: (1, 50.0)}) for i in range(1, 12)])

    def test_duplicate_ids_rejected(self):
        p1 = _plan(1, {0: (1, 50.0)})
        p2 = _plan(2, {1: (2, 50.0)})
        with self.assertRaisesRegex(ValueError, "重复"):
            plan_batch_sequence([p1, p2, p1])

    def test_radius_mismatch_rejected(self):
        p1 = _plan(1, {0: (1, 50.0)}, radius=100.0)
        p2 = _plan(2, {1: (2, 50.0)}, radius=120.0)
        with self.assertRaisesRegex(ValueError, "半径"):
            plan_batch_sequence([p1, p2])

    def test_slot_angle_mismatch_rejected(self):
        s1 = _slots()
        s2 = _slots()
        s2[3] = dict(s2[3], angle=45.0)
        p1 = _plan(1, {0: (1, 50.0)}, slots=s1)
        p2 = _plan(2, {1: (2, 50.0)}, slots=s2)
        with self.assertRaisesRegex(ValueError, "槽位 3"):
            plan_batch_sequence([p1, p2])

    def test_slot_enabled_mismatch_rejected(self):
        p1 = _plan(1, {0: (1, 50.0)}, slots=_slots())
        p2 = _plan(2, {1: (2, 50.0)}, slots=_slots(disabled=(5,)))
        with self.assertRaisesRegex(ValueError, "槽位 5"):
            plan_batch_sequence([p1, p2])

    def test_slot_set_mismatch_rejected(self):
        p1 = _plan(1, {0: (1, 50.0)}, slots=_slots(12))
        p2 = _plan(2, {0: (1, 50.0)}, slots=_slots(10))
        with self.assertRaisesRegex(ValueError, "槽位构成"):
            plan_batch_sequence([p1, p2])

    def test_tube_mass_conflict_rejected(self):
        p1 = _plan(1, {0: (1, 50.0)})
        p2 = _plan(2, {1: (1, 55.0)})
        with self.assertRaisesRegex(ValueError, "试管 1"):
            plan_batch_sequence([p1, p2])

    def test_same_tube_same_mass_accepted(self):
        # 同编号同质量: 合法, 且同槽保留不计操作
        p1 = _plan(1, {0: (1, 50.0), 1: (2, 40.0)})
        p2 = _plan(2, {0: (1, 50.0), 3: (3, 45.0)})
        r = plan_batch_sequence([p1, p2])
        self.assertEqual(r["total_operations"], 2 + 2)  # 装2 + 卸1装1
        self.assertEqual(r["batches"][1]["kept_count"], 1)


if __name__ == "__main__":
    unittest.main()
