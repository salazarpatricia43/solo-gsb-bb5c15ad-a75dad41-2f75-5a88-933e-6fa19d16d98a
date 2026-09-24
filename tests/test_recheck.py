"""现场装管复核测试: python -m unittest discover -s tests

核心断言: 子集 DP 求得的最小总绝对质量差、最优匹配条数, 以及每个实测槽
在**所有最优匹配**中可能对应的试管集合, 必须与全排列暴力枚举一致; 候选
唯一时按原方案槽位判定正确/错槽, 多解时标为身份不确定; 读数数量与方案
管数不等、不存在完整匹配时明确说明原因; 读数精度(0.01 g)、份数上限、
槽位合法性、偏差非负等非法输入必须拒绝; 复核过程不修改入参(只读)。
"""
import copy
import itertools
import random
import unittest

from app.recheck import recheck_tube_loading


def _plan(pid, placements, slots_n=12, enabled=None, name=None):
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
        "request": {"radius_mm": 100.0},
        "result": {
            "config": {"radius_mm": 100.0},
            "slot_state": slot_state,
            "placements": pls,
            "metrics": {"u1_mag_g_mm": 0.0},
        },
    }


def _brute(tubes, readings, tol):
    """全排列参考实现。

    tubes: [(tube_id, plan_slot, mass)]; readings: [(slot, mass)]。
    返回 (最小总绝对质量差或 None, 最优匹配条数, {读数序号: {试管序号}},
    {试管序号: {读数序号}}); 不存在完整匹配时首项为 None。
    """
    n = len(tubes)
    cost = [[None] * n for _ in range(n)]
    for k, (_, _, mk) in enumerate(tubes):
        for j, (_, rj) in enumerate(readings):
            d = abs(mk - rj)
            if d <= tol + 1e-9 * max(1.0, tol):
                cost[k][j] = d
    best = None
    count = 0
    cand_r = [set() for _ in range(n)]
    cand_t = [set() for _ in range(n)]
    for perm in itertools.permutations(range(n)):
        # perm[k] = 试管 k 匹配的读数序号
        tot = 0.0
        ok = True
        for k in range(n):
            c = cost[k][perm[k]]
            if c is None:
                ok = False
                break
            tot += c
        if not ok:
            continue
        if best is None or tot < best - 1e-9 * max(1.0, best):
            best, count = tot, 1
            cand_r = [set() for _ in range(n)]
            cand_t = [set() for _ in range(n)]
            for k in range(n):
                cand_r[perm[k]].add(k)
                cand_t[k].add(perm[k])
        elif tot <= best + 1e-9 * max(1.0, best):
            count += 1
            for k in range(n):
                cand_r[perm[k]].add(k)
                cand_t[k].add(perm[k])
    return best, count, cand_r, cand_t


class TestRecheck(unittest.TestCase):
    def test_all_correct_unique(self):
        # 质量互不相同, 实测与标称一致: 唯一匹配, 全部正确
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0), 9: (4, 53.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.0}, {"slot": 3, "mass": 51.0},
            {"slot": 6, "mass": 52.0}, {"slot": 9, "mass": 53.0},
        ], 0.05)
        self.assertTrue(r["complete"])
        self.assertEqual(r["optimal_matching_count"], 1)
        self.assertAlmostEqual(r["total_abs_deviation_g"], 0.0, places=12)
        self.assertEqual([x["status"] for x in r["readings"]],
                         ["correct"] * 4)
        self.assertEqual(r["summary"],
                         {"correct": 4, "wrong_slot": 0, "uncertain": 0})
        self.assertEqual([x["status"] for x in r["tubes"]], ["correct"] * 4)
        self.assertIn("全部正确", r["reason"])

    def test_wrong_slot_swap_detected(self):
        # 管 1(50.00)与管 2(50.05)装槽互换: 总差最小的匹配唯一, 两槽均错槽
        p = _plan(1, {0: (1, 50.0), 3: (2, 50.05)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.05}, {"slot": 3, "mass": 50.0},
        ], 0.1)
        self.assertTrue(r["complete"])
        self.assertEqual(r["optimal_matching_count"], 1)
        self.assertAlmostEqual(r["total_abs_deviation_g"], 0.0, places=12)
        st = {x["slot"]: x for x in r["readings"]}
        self.assertEqual(st[0]["status"], "wrong_slot")
        self.assertEqual(st[0]["matched_tube_id"], 2)
        self.assertEqual(st[3]["status"], "wrong_slot")
        self.assertEqual(st[3]["matched_tube_id"], 1)
        self.assertIn("错槽", st[0]["detail"])
        # 对称的逐管视图: 两管均不在方案槽位
        tt = {x["tube_id"]: x for x in r["tubes"]}
        self.assertEqual(tt[1]["status"], "wrong_slot")
        self.assertEqual(tt[1]["candidate_slots"], [3])
        self.assertEqual(tt[2]["candidate_slots"], [0])
        self.assertEqual(r["summary"]["wrong_slot"], 2)

    def test_identical_masses_uncertain(self):
        # 两支等质量: 2 种最优匹配, 两槽均身份不确定, 不得断言错放
        p = _plan(1, {0: (1, 50.0), 6: (2, 50.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.0}, {"slot": 6, "mass": 50.0},
        ], 0.05)
        self.assertTrue(r["complete"])
        self.assertEqual(r["optimal_matching_count"], 2)
        self.assertEqual([x["status"] for x in r["readings"]],
                         ["uncertain"] * 2)
        self.assertEqual(r["readings"][0]["candidate_count"], 2)
        self.assertIsNone(r["readings"][0]["matched_tube_id"])
        self.assertIn("不能任取一解", r["readings"][0]["detail"])
        self.assertIn("身份不确定", r["reason"])
        # 逐管视图同样不确定(候选含方案槽)
        self.assertEqual([x["status"] for x in r["tubes"]],
                         ["uncertain"] * 2)

    def test_mixed_correct_wrong_uncertain(self):
        # 管 1(53)唯一正确; 管 2(50)/管 3(50)等质量互换装槽: 最优匹配唯一
        # (互换后总差 0), 两槽唯一对应但都错槽
        p = _plan(1, {0: (1, 53.0), 3: (2, 50.0), 6: (3, 50.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 53.0}, {"slot": 3, "mass": 50.0},
            {"slot": 6, "mass": 50.0},
        ], 0.05)
        self.assertTrue(r["complete"])
        # 管 2/3 等质量: 两种最优匹配, 槽 3/6 均不确定; 槽 0 唯一正确
        self.assertEqual(r["optimal_matching_count"], 2)
        st = {x["slot"]: x["status"] for x in r["readings"]}
        self.assertEqual(st[0], "correct")
        self.assertEqual(st[3], "uncertain")
        self.assertEqual(st[6], "uncertain")
        self.assertEqual(r["summary"],
                         {"correct": 1, "wrong_slot": 0, "uncertain": 2})

    def test_count_mismatch_explained(self):
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.0}, {"slot": 3, "mass": 51.0},
        ], 0.05)
        self.assertFalse(r["complete"])
        self.assertIn("不等", r["reason"])
        self.assertIn("2", r["reason"])
        self.assertIsNone(r["total_abs_deviation_g"])
        self.assertEqual(r["readings"], [])
        # 只读: 数量不等时也不修改方案
        r2 = recheck_tube_loading(p, [
            {"slot": i, "mass": 50.0} for i in range(4)
        ], 0.05)
        self.assertFalse(r2["complete"])

    def test_no_complete_matching_explained(self):
        # 管 2(60 g)在偏差内无任何兼容读数; 读数槽 3(55 g)无兼容试管
        p = _plan(1, {0: (1, 50.0), 3: (2, 60.0), 6: (3, 52.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.0}, {"slot": 3, "mass": 55.0},
            {"slot": 6, "mass": 52.0},
        ], 1.0)
        self.assertFalse(r["complete"])
        self.assertEqual(r["max_matched_pairs"], 2)
        self.assertEqual([t["tube_id"] for t in r["tubes_without_candidate"]],
                         [2])
        self.assertEqual([x["slot"] for x in r["readings_without_candidate"]],
                         [3])
        self.assertIn("不存在完整的一对一匹配", r["reason"])
        self.assertIn("2/3", r["reason"])

    def test_tolerance_boundary(self):
        # 质量差恰等于偏差: 允许匹配(边界含等号)
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.05}, {"slot": 3, "mass": 50.95},
        ], 0.05)
        self.assertTrue(r["complete"])
        self.assertAlmostEqual(r["total_abs_deviation_g"], 0.1, places=9)
        # 偏差再小 0.01: 两条边都断开, 不存在完整匹配
        r2 = recheck_tube_loading(p, [
            {"slot": 0, "mass": 50.05}, {"slot": 3, "mass": 50.95},
        ], 0.04)
        self.assertFalse(r2["complete"])
        self.assertEqual(r2["max_matched_pairs"], 0)

    def test_reading_on_plan_empty_slot(self):
        # 实测槽是方案中的空槽(启用但未占用): 合法录入, 且可判错槽
        p = _plan(1, {0: (1, 50.0), 6: (2, 60.0)})
        r = recheck_tube_loading(p, [
            {"slot": 0, "mass": 60.0}, {"slot": 6, "mass": 50.0},
        ], 0.05)
        self.assertTrue(r["complete"])
        self.assertEqual([x["status"] for x in r["readings"]],
                         ["wrong_slot"] * 2)

    def test_matches_brute_force_random(self):
        rng = random.Random(20260924)
        for _ in range(60):
            n = rng.randint(1, 7)
            slots = rng.sample(range(12), n)
            masses = [round(rng.uniform(20.0, 60.0), 2) for _ in range(n)]
            placements = {s: (i + 1, masses[i]) for i, s in enumerate(slots)}
            plan = _plan(1, placements)
            # 现场: 随机打乱槽位(模拟错装)并加 0.01 精度噪声
            perm_slots = slots[:]
            rng.shuffle(perm_slots)
            tol = rng.choice([0.0, 0.02, 0.05, 0.1, 0.3, 1.0])
            readings = []
            for i, s in enumerate(perm_slots):
                noise = round(rng.uniform(-tol, tol), 2) if tol > 0 else 0.0
                m = round(masses[i] + noise, 2)
                readings.append({"slot": s, "mass": max(0.01, m)})
            r = recheck_tube_loading(plan, readings, tol)
            tubes = sorted(
                ((p["tube_id"], p["slot"], p["mass"])
                 for p in plan["result"]["placements"]),
                key=lambda x: x[0],
            )
            rd = sorted((x["slot"], x["mass"]) for x in readings)
            best, count, cand_r, cand_t = _brute(tubes, rd, tol)
            if best is None:
                self.assertFalse(r["complete"])
                continue
            self.assertTrue(r["complete"])
            self.assertAlmostEqual(r["total_abs_deviation_g"], best, places=6)
            self.assertEqual(r["optimal_matching_count"], count)
            # 逐实测槽候选集合与暴力枚举一致
            for j, row in enumerate(sorted(r["readings"],
                                           key=lambda x: x["slot"])):
                want = {tubes[k][0] for k in cand_r[j]}
                got = {c["tube_id"] for c in row["candidates"]}
                self.assertEqual(got, want)
                # 判定规则: 唯一按方案槽位, 多解不确定
                if len(want) == 1:
                    tid = next(iter(want))
                    pslot = next(t[1] for t in tubes if t[0] == tid)
                    want_status = ("correct" if pslot == row["slot"]
                                   else "wrong_slot")
                else:
                    want_status = "uncertain"
                self.assertEqual(row["status"], want_status)
            # 逐管候选槽位集合与暴力枚举一致
            for k, trow in enumerate(r["tubes"]):
                want_slots = sorted(rd[j][0] for j in cand_t[k])
                self.assertEqual(trow["candidate_slots"], want_slots)
            # 汇总计数与逐槽判定一致
            self.assertEqual(
                r["summary"]["correct"] + r["summary"]["wrong_slot"]
                + r["summary"]["uncertain"], n)

    def test_max_readings_accepted(self):
        # 12 管 / 12 读数上限内可复核
        placements = {i: (i + 1, 40.0 + i) for i in range(12)}
        p = _plan(1, placements)
        r = recheck_tube_loading(
            p, [{"slot": i, "mass": 40.0 + i} for i in range(12)], 0.05)
        self.assertTrue(r["complete"])
        self.assertEqual(r["reading_count"], 12)
        self.assertEqual(r["summary"]["correct"], 12)

    def test_validation_errors(self):
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0)}, enabled={0, 3})
        good = [{"slot": 0, "mass": 50.0}, {"slot": 3, "mass": 51.0}]
        # 空读数
        with self.assertRaises(ValueError):
            recheck_tube_loading(p, [], 0.05)
        # 超过 12 份
        with self.assertRaises(ValueError):
            recheck_tube_loading(p, [{"slot": 0, "mass": 1.0}] * 13, 0.05)
        # 槽位重复
        with self.assertRaises(ValueError):
            recheck_tube_loading(
                p, [{"slot": 0, "mass": 50.0}, {"slot": 0, "mass": 51.0}], 0.05)
        # 槽位不存在
        with self.assertRaises(ValueError):
            recheck_tube_loading(
                p, [{"slot": 99, "mass": 50.0}, {"slot": 3, "mass": 51.0}], 0.05)
        # 槽位为禁用槽(槽 1 未启用)
        with self.assertRaises(ValueError):
            recheck_tube_loading(
                p, [{"slot": 1, "mass": 50.0}, {"slot": 3, "mass": 51.0}], 0.05)
        # 实测质量非正
        with self.assertRaises(ValueError):
            recheck_tube_loading(
                p, [{"slot": 0, "mass": 0.0}, {"slot": 3, "mass": 51.0}], 0.05)
        # 实测质量不符合 0.01 g 精度
        with self.assertRaises(ValueError):
            recheck_tube_loading(
                p, [{"slot": 0, "mass": 50.005}, {"slot": 3, "mass": 51.0}],
                0.05)
        # 负偏差 / 非有限偏差
        with self.assertRaises(ValueError):
            recheck_tube_loading(p, good, -0.1)
        with self.assertRaises(ValueError):
            recheck_tube_loading(p, good, float("nan"))
        # 空方案
        with self.assertRaises(ValueError):
            recheck_tube_loading(_plan(1, {}), good, 0.05)

    def test_read_only(self):
        p = _plan(1, {0: (1, 50.0), 3: (2, 51.0), 6: (3, 52.0)})
        readings = [{"slot": 0, "mass": 50.0}, {"slot": 3, "mass": 51.0},
                    {"slot": 6, "mass": 52.0}]
        snap_p, snap_r = copy.deepcopy(p), copy.deepcopy(readings)
        recheck_tube_loading(p, readings, 0.05)
        self.assertEqual(p, snap_p)
        self.assertEqual(readings, snap_r)

    def test_plan_info_and_note(self):
        p = _plan(9, {0: (1, 50.0)}, name="复核测试")
        r = recheck_tube_loading(p, [{"slot": 0, "mass": 50.0}], 0.05)
        self.assertEqual(r["plan"]["id"], 9)
        self.assertEqual(r["plan"]["name"], "复核测试")
        self.assertIn("只读", r["note"])
        self.assertIn("一对一匹配", r["method"])
        self.assertEqual(r["tolerance_g"], 0.05)
        self.assertEqual(r["plan_tube_count"], 1)
        self.assertEqual(r["reading_count"], 1)
        self.assertEqual(r["readings"][0]["status_label"], "正确")


if __name__ == "__main__":
    unittest.main()
