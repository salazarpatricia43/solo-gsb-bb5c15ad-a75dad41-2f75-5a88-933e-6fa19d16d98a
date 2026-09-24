"""配平引擎回归测试(仅依赖标准库): python -m unittest discover -s tests"""
import json
import math
import unittest

from app.balancer import run_plan


def base_payload(**over):
    p = {
        "slot_count": 12,
        "slots": [{"index": i, "angle": i * 30, "enabled": True} for i in range(12)],
        "radius_mm": 100.0,
        "rpm": 4000,
        "restarts": 6,
        "chain_steps": 900,
    }
    p.update(over)
    return p


class TestBalancer(unittest.TestCase):
    def six_tubes(self, **over):
        p = base_payload(**over)
        p["tubes"] = [
            {"id": 1, "mass": 50}, {"id": 2, "mass": 50},
            {"id": 3, "mass": 51}, {"id": 4, "mass": 49},
            {"id": 5, "mass": 30}, {"id": 6, "mass": 30},
        ]
        return p

    def test_uniform_12_slot_balance(self):
        r = run_plan(self.six_tubes())
        m = r["metrics"]
        # 该数据集的一阶最优约 3.85 g·mm; 启发式给出良好近似
        self.assertLess(m["u1_mag_g_mm"], 600.0)
        self.assertEqual(len(r["placements"]), 6)
        slots_used = [p["slot"] for p in r["placements"]]
        self.assertEqual(len(slots_used), len(set(slots_used)))

    def test_equal_masses_perfect_balance(self):
        p = base_payload()
        p["tubes"] = [{"id": i, "mass": 50} for i in range(1, 5)]
        r = run_plan(p)
        self.assertAlmostEqual(r["metrics"]["u1_mag_g_mm"], 0.0, places=6)
        self.assertAlmostEqual(r["metrics"]["u2_mag_g_mm"], 0.0, places=6)
        self.assertAlmostEqual(r["metrics"]["centrifugal_force_n"], 0.0, places=6)

    def test_three_equal_tubes_theoretical_min(self):
        p = base_payload()
        p["tubes"] = [{"id": i, "mass": 40} for i in range(1, 4)]
        r = run_plan(p)
        # 三支等质量在 30° 栅格 12 槽上最小一阶残余 = 40g*100mm = 4000 g·mm
        self.assertLessEqual(r["metrics"]["u1_mag_g_mm"], 4000.001)

    def test_disabled_slots_never_used(self):
        p = self.six_tubes()
        p["slots"][6]["enabled"] = False
        p["slots"][0]["enabled"] = False
        r = run_plan(p)
        for pl in r["placements"]:
            self.assertNotIn(pl["slot"], (0, 6))
        for ss in r["slot_state"]:
            if not ss["enabled"]:
                self.assertIsNone(ss["tube_id"])

    def test_locked_tube_respected(self):
        p = self.six_tubes()
        p["tubes"][0].update(locked=True, slot=0)
        r = run_plan(p)
        self.assertEqual(next(x for x in r["placements"] if x["tube_id"] == 1)["slot"], 0)

    def test_locked_on_disabled_raises(self):
        p = self.six_tubes()
        p["tubes"][0].update(locked=True, slot=6)
        p["slots"][6]["enabled"] = False
        with self.assertRaises(ValueError):
            run_plan(p)

    def test_too_many_tubes_raises(self):
        p = self.six_tubes()
        p["tubes"] = p["tubes"] * 3
        with self.assertRaises(ValueError):
            run_plan(p)

    def test_incremental_preserves_unmoved(self):
        p0 = self.six_tubes()
        p0["tubes"][0].update(locked=True, slot=0)
        r0 = run_plan(p0)
        p1 = json.loads(json.dumps(p0))
        p1["tubes"].append({"id": 7, "mass": 20})
        p1["previous"] = [
            {"tube_id": x["tube_id"], "slot": x["slot"]} for x in r0["placements"]
        ]
        r1 = run_plan(p1)
        before = {x["tube_id"]: x["slot"] for x in r0["placements"]}
        after = {x["tube_id"]: x["slot"] for x in r1["placements"]}
        moved = set(r1["moved_tube_ids"])
        for tid, slot in before.items():
            if tid != 1 and tid not in moved:
                self.assertEqual(after[tid], slot)
        # 新增试管已摆放
        self.assertIsNotNone(after.get(7))

    def test_incremental_remove(self):
        p0 = self.six_tubes()
        r0 = run_plan(p0)
        p1 = json.loads(json.dumps(p0))
        p1["tubes"] = [t for t in p1["tubes"] if t["id"] != 2]
        p1["previous"] = [
            {"tube_id": x["tube_id"], "slot": x["slot"]} for x in r0["placements"]
        ]
        r1 = run_plan(p1)
        self.assertEqual(r1["total_tubes"], 5)

    def test_deterministic(self):
        p = self.six_tubes()
        a = run_plan(json.loads(json.dumps(p)))
        b = run_plan(json.loads(json.dumps(p)))
        la = [(x["tube_id"], x["slot"]) for x in a["placements"]]
        lb = [(x["tube_id"], x["slot"]) for x in b["placements"]]
        self.assertEqual(la, lb)

    def test_replay_steps_consistent(self):
        r = run_plan(self.six_tubes())
        self.assertTrue(r["steps"])
        last = r["steps"][-1]
        self.assertAlmostEqual(last["vectors"]["u1_mag"], r["metrics"]["u1_mag_g_mm"], places=6)
        self.assertAlmostEqual(last["vectors"]["u2_mag"], r["metrics"]["u2_mag_g_mm"], places=6)
        for st in r["steps"]:
            self.assertEqual(len(st["placements"]), 6)
            self.assertIn("reason", st)

    def test_metric_relationships(self):
        p = base_payload(rpm=6000, radius_mm=120)
        p["tubes"] = [{"id": 1, "mass": 100}, {"id": 2, "mass": 100}]
        r = run_plan(p)
        m = r["metrics"]
        omega = 2 * math.pi * 6000 / 60
        # F = omega^2 * |U1| * 1e-6
        self.assertAlmostEqual(m["centrifugal_force_n"],
                               omega ** 2 * m["u1_mag_g_mm"] * 1e-6, places=6)
        self.assertAlmostEqual(m["resultant_moment_n_m"],
                               m["centrifugal_force_n"] * 0.120, places=6)

    def test_irregular_angles(self):
        p = {
            "slot_count": 4,
            "slots": [
                {"index": 0, "angle": 0}, {"index": 1, "angle": 95},
                {"index": 2, "angle": 180}, {"index": 3, "angle": 272},
            ],
            "radius_mm": 100, "rpm": 3000,
            "restarts": 4, "chain_steps": 600,
        }
        p["tubes"] = [{"id": 1, "mass": 100}, {"id": 2, "mass": 100}]
        r = run_plan(p)
        # 最接近对径的是 0/180
        slots = sorted(x["slot"] for x in r["placements"])
        self.assertEqual(slots, [0, 2])

    def test_pair_and_singleton_explanations(self):
        r = run_plan(self.six_tubes())
        for pr in r["pairs"]:
            self.assertIn("reason", pr)
            self.assertGreaterEqual(pr["u1_cancel_pct"], 0.0)
        for sg in r["singletons"]:
            self.assertIn("reason", sg)


if __name__ == "__main__":
    unittest.main()
