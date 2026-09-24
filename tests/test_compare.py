"""方案比较逻辑测试: python -m unittest discover -s tests"""
import unittest

from app.compare import compare_plans


def _plan(pid, placements, request, u1=10.0, u1_ang=90.0, u2=5.0, u2_ang=0.0,
          grade="B", total_mass=200.0, force=1.0, moment=0.1, ecc=2.0):
    return {
        "id": pid,
        "name": f"方案{pid}",
        "created_at": "2026-09-23T00:00:00+00:00",
        "request": request,
        "result": {
            "metrics": {
                "u1_mag_g_mm": u1, "u1_angle_deg": u1_ang,
                "u2_mag_g_mm": u2, "u2_angle_deg": u2_ang,
                "centrifugal_force_n": force,
                "resultant_moment_n_m": moment,
                "equivalent_eccentricity_um": ecc,
                "total_mass_g": total_mass,
                "grade": grade,
            },
            "placements": placements,
        },
    }


def _req(rpm=4000, radius=100.0, w1=1.0, w2=1e-6, move_weight=0.0,
         restarts=10, chain_steps=2500, n_slots=12, slots=None, tubes=None):
    if slots is None:
        slots = [{"index": i, "angle": i * 30, "enabled": True}
                 for i in range(n_slots)]
    if tubes is None:
        tubes = [{"id": 1, "mass": 50}, {"id": 2, "mass": 50}]
    return {
        "slot_count": len(slots), "slots": slots, "tubes": tubes,
        "radius_mm": radius, "rpm": rpm, "w1": w1, "w2": w2,
        "move_weight": move_weight, "restarts": restarts,
        "chain_steps": chain_steps,
    }


def _pl(tid, slot, mass=50, locked=False, angle=None):
    return {"tube_id": tid, "slot": slot, "mass": mass, "locked": locked,
            "angle": slot * 30 if angle is None and slot is not None else angle}


class TestCompare(unittest.TestCase):
    def test_placement_statuses(self):
        # A: 管1 槽0, 管2 槽2, 管3 槽4 ; B: 管1 槽0(未变), 管2 槽6(换槽), 管4 槽8(新增)
        a = _plan(1, [_pl(1, 0), _pl(2, 2), _pl(3, 4)], _req())
        b = _plan(2, [_pl(1, 0), _pl(2, 6), _pl(4, 8)], _req())
        r = compare_plans(a, b)
        st = {row["tube_id"]: row["status"] for row in r["placements"]}
        self.assertEqual(st, {1: "unchanged", 2: "moved", 3: "removed", 4: "added"})
        self.assertEqual(r["placement_counts"],
                         {"added": 1, "removed": 1, "moved": 1, "unchanged": 1})
        moved = next(x for x in r["placements"] if x["tube_id"] == 2)
        self.assertEqual((moved["slot_a"], moved["slot_b"]), (2, 6))

    def test_metric_deltas_b_minus_a_and_angle_wrap(self):
        a = _plan(1, [_pl(1, 0)], _req(), u1=10.0, u1_ang=350.0)
        b = _plan(2, [_pl(1, 0)], _req(), u1=13.0, u1_ang=10.0)
        r = compare_plans(a, b)
        m = {x["key"]: x for x in r["metrics"]}
        self.assertAlmostEqual(m["u1_mag_g_mm"]["delta_b_minus_a"], 3.0, places=9)
        # 350° -> 10° 归一化为 +20°, 而非 +340°/-340°
        self.assertAlmostEqual(m["u1_angle_deg"]["delta_b_minus_a"], 20.0, places=9)

    def test_same_conditions_flag(self):
        req = _req(tubes=[{"id": 1, "mass": 50}, {"id": 2, "mass": 50}])
        a = _plan(1, [_pl(1, 0), _pl(2, 6)], req)
        b = _plan(2, [_pl(1, 2), _pl(2, 8)], req)
        r = compare_plans(a, b)
        # 输入完全相同, 仅摆放不同 -> 同条件
        self.assertTrue(r["same_conditions"])
        self.assertEqual(r["differing_groups"], [])
        self.assertFalse(r["input_diff"]["rotor"])
        self.assertFalse(r["input_diff"]["solver"])
        self.assertFalse(r["input_diff"]["tubes"])

    def test_solver_input_difference_flagged(self):
        a = _plan(1, [_pl(1, 0)], _req(rpm=4000))
        b = _plan(2, [_pl(1, 0)], _req(rpm=6000))
        r = compare_plans(a, b)
        self.assertFalse(r["same_conditions"])
        self.assertIn("solver", r["differing_groups"])
        rpm_row = next(x for x in r["input_diff"]["solver"] if x["label"] == "转速")
        self.assertEqual((rpm_row["value_a"], rpm_row["value_b"]), (4000.0, 6000.0))

    def test_rotor_difference_flagged(self):
        a = _plan(1, [_pl(1, 0)], _req(radius=100.0))
        b = _plan(2, [_pl(1, 0)], _req(radius=120.0))
        r = compare_plans(a, b)
        self.assertFalse(r["same_conditions"])
        self.assertIn("rotor", r["differing_groups"])
        labels = [x["label"] for x in r["input_diff"]["rotor"]]
        self.assertIn("转子半径 R", labels)

    def test_slot_geometry_difference(self):
        req_a = _req(slots=[{"index": i, "angle": i * 30, "enabled": True}
                            for i in range(12)])
        slots_b = [dict(s) for s in req_a["slots"]]
        slots_b[6]["enabled"] = False
        slots_b[1]["angle"] = 31.0
        req_b = _req(slots=slots_b)
        a = _plan(1, [_pl(1, 0)], req_a)
        b = _plan(2, [_pl(1, 0)], req_b)
        r = compare_plans(a, b)
        labels = {x["label"] for x in r["input_diff"]["rotor"]}
        self.assertIn("槽 1", labels)
        self.assertIn("槽 6", labels)

    def test_tube_list_difference_flagged(self):
        req_a = _req(tubes=[{"id": 1, "mass": 50}, {"id": 2, "mass": 50}])
        req_b = _req(tubes=[{"id": 1, "mass": 52}, {"id": 3, "mass": 40}])
        a = _plan(1, [_pl(1, 0), _pl(2, 6)], req_a)
        b = _plan(2, [_pl(1, 2), _pl(3, 8, mass=40)], req_b)
        r = compare_plans(a, b)
        self.assertFalse(r["same_conditions"])
        labels = {x["label"] for x in r["input_diff"]["tubes"]}
        self.assertIn("试管 1", labels)   # 质量变化
        self.assertIn("试管 2", labels)   # 移除
        self.assertIn("试管 3", labels)   # 新增

    def test_incremental_fields_ignored(self):
        # previous / parent_id 属于增量重排机制, 不属于“求解输入”比较
        req_a = _req()
        req_b = dict(_req())
        req_b["previous"] = [{"tube_id": 1, "slot": 3}]
        req_b["parent_id"] = 1
        a = _plan(1, [_pl(1, 0)], req_a)
        b = _plan(2, [_pl(1, 0)], req_b)
        r = compare_plans(a, b)
        self.assertTrue(r["same_conditions"])


if __name__ == "__main__":
    unittest.main()
