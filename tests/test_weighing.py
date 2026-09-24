"""称重误差校核逻辑测试: python -m unittest discover -s tests

核心断言: zonotope 顶点枚举得到的最不利 |U₁| 必须与 2^n 全组合枚举一致;
返回的一组实际质量必须真正达到该最不利向量; 标量直和(把各管误差绝对值
相加)在多数几何下不等于(且高估)向量最不利值。
"""
import math
import random
import unittest

from app.weighing import worst_case_u1


def _pl(tid, slot, angle, mass):
    return {"tube_id": tid, "slot": slot, "angle": angle, "mass": mass}


def brute_force(placements, radius, errors):
    """2^n 全组合参考实现。"""
    n = len(placements)
    best = -1.0
    for mask in range(1 << n):
        ux = uy = 0.0
        for i, p in enumerate(placements):
            e = errors.get(p["tube_id"], 0.0)
            s = 1.0 if (mask >> i) & 1 else -1.0
            m = p["mass"] + s * e
            a = math.radians(p["angle"])
            ux += m * math.cos(a)
            uy += m * math.sin(a)
        best = max(best, math.hypot(radius * ux, radius * uy))
    return best


class TestWorstCaseU1(unittest.TestCase):
    def test_single_tube_endpoints(self):
        r = worst_case_u1([_pl(1, 0, 0.0, 50)], 100.0, {1: 1.0})
        self.assertAlmostEqual(r["nominal"]["u1_mag_g_mm"], 5000.0, places=9)
        self.assertAlmostEqual(r["worst_case"]["u1_mag_g_mm"], 5100.0, places=9)
        self.assertAlmostEqual(r["worst_case"]["u1_angle_deg"], 0.0, places=9)
        self.assertEqual(r["actual_masses"][0]["actual_mass_g"], 51.0)
        # 单管时朴素标量估计 |U_nom| + R·Σe 恰好等于真值
        self.assertTrue(r["naive_equals_exact"])
        self.assertAlmostEqual(r["abs_dev_bound_g_mm"], 100.0, places=9)

    def test_opposite_pair_worst_is_difference(self):
        # 严格对径: 标称完全对消, 最不利情形是两误差反向叠加(2e)
        pl = [_pl(1, 0, 0.0, 50), _pl(2, 6, 180.0, 50)]
        r = worst_case_u1(pl, 100.0, {1: 1.0, 2: 1.0})
        self.assertAlmostEqual(r["nominal"]["u1_mag_g_mm"], 0.0, places=9)
        self.assertAlmostEqual(r["worst_case"]["u1_mag_g_mm"], 200.0, places=9)
        # 一管 +e 另一管 -e
        d = {row["tube_id"]: row["delta_g"] for row in r["tubes"]}
        self.assertAlmostEqual(abs(d[1]) + abs(d[2]), 2.0, places=9)
        self.assertAlmostEqual(d[1], -d[2], places=9)

    def test_ninety_degree_pair_vector_not_scalar(self):
        pl = [_pl(1, 0, 0.0, 50), _pl(2, 3, 90.0, 50)]
        r = worst_case_u1(pl, 100.0, {1: 1.0, 2: 1.0})
        # 向量最不利值 = 100*51*sqrt(2)
        self.assertAlmostEqual(
            r["worst_case"]["u1_mag_g_mm"], 100.0 * 51.0 * math.sqrt(2.0),
            places=6,
        )
        self.assertAlmostEqual(r["worst_case"]["u1_angle_deg"], 45.0, places=6)
        # “标称模长 + 各管误差绝对值标量直和”在方向不一致时严格高估:
        # 7071.07 + 200 = 7271.07 > 精确向量值 7212.49
        self.assertFalse(r["naive_equals_exact"])
        self.assertGreater(r["naive_abs_worst_g_mm"],
                           r["worst_case"]["u1_mag_g_mm"] + 1.0)
        self.assertAlmostEqual(
            r["naive_abs_worst_g_mm"],
            r["nominal"]["u1_mag_g_mm"] + r["abs_dev_bound_g_mm"], places=9)

    def test_actual_masses_reproduce_worst_vector(self):
        pl = [_pl(1, 0, 15.0, 50), _pl(2, 3, 100.0, 47),
              _pl(3, 7, 205.0, 33), _pl(4, 10, 300.0, 20.5)]
        errors = {1: 0.8, 2: 1.2, 3: 0.3, 4: 0.05}
        r = worst_case_u1(pl, 100.0, errors)
        ux = uy = 0.0
        for row in r["tubes"]:
            self.assertGreaterEqual(row["actual_mass_g"],
                                    row["nominal_mass_g"] - row["mass_error_bound_g"] - 1e-9)
            self.assertLessEqual(row["actual_mass_g"],
                                 row["nominal_mass_g"] + row["mass_error_bound_g"] + 1e-9)
            a = math.radians(row["angle_deg"])
            ux += row["actual_mass_g"] * math.cos(a)
            uy += row["actual_mass_g"] * math.sin(a)
        mag = 100.0 * math.hypot(ux, uy)
        self.assertAlmostEqual(mag, r["worst_case"]["u1_mag_g_mm"], places=6)
        self.assertAlmostEqual(
            math.degrees(math.atan2(100.0 * uy, 100.0 * ux)) % 360.0,
            r["worst_case"]["u1_angle_deg"], places=6,
        )

    def test_matches_brute_force_random(self):
        rng = random.Random(20260923)
        for _ in range(120):
            n = rng.randint(1, 9)
            angles = sorted(rng.uniform(0.0, 360.0) for _ in range(n))
            pl, errors = [], {}
            for i, ang in enumerate(angles):
                mass = rng.uniform(5.0, 60.0)
                e = rng.uniform(0.0, mass)
                tid = i + 1
                pl.append(_pl(tid, i, ang, mass))
                if rng.random() < 0.85:
                    errors[tid] = e
            radius = rng.uniform(50.0, 200.0)
            r = worst_case_u1(pl, radius, errors)
            expect = brute_force(pl, radius, errors)
            self.assertAlmostEqual(
                r["worst_case"]["u1_mag_g_mm"], expect,
                delta=1e-6 * max(1.0, expect),
            )

    def test_zero_errors_worst_equals_nominal(self):
        pl = [_pl(1, 0, 0.0, 50), _pl(2, 6, 180.0, 50.5)]
        r = worst_case_u1(pl, 100.0, {})
        self.assertAlmostEqual(r["worst_case"]["u1_mag_g_mm"],
                               r["nominal"]["u1_mag_g_mm"], places=9)
        for row in r["tubes"]:
            self.assertEqual(row["mass_error_bound_g"], 0.0)
            self.assertAlmostEqual(row["actual_mass_g"], row["nominal_mass_g"], places=9)

    def test_missing_tube_error_defaults_zero(self):
        pl = [_pl(1, 0, 0.0, 50), _pl(2, 3, 90.0, 50)]
        r = worst_case_u1(pl, 100.0, {2: 2.0})
        rows = {x["tube_id"]: x for x in r["tubes"]}
        self.assertEqual(rows[1]["mass_error_bound_g"], 0.0)
        self.assertEqual(rows[2]["mass_error_bound_g"], 2.0)
        # 仅管2 有误差时, 最不利模 = 100*hypot(50, 52)
        self.assertAlmostEqual(
            r["worst_case"]["u1_mag_g_mm"], 100.0 * math.hypot(50.0, 52.0), places=6)

    def test_limit_verdict(self):
        pl = [_pl(1, 0, 0.0, 50)]
        r_ok = worst_case_u1(pl, 100.0, {1: 1.0}, u1_limit_g_mm=6000.0)
        self.assertTrue(r_ok["within_limit"])
        self.assertEqual(r_ok["excess_g_mm"], 0.0)
        self.assertAlmostEqual(r_ok["margin_g_mm"], 900.0, places=6)
        r_bad = worst_case_u1(pl, 100.0, {1: 1.0}, u1_limit_g_mm=5050.0)
        self.assertFalse(r_bad["within_limit"])
        self.assertAlmostEqual(r_bad["excess_g_mm"], 50.0, places=6)
        self.assertEqual(r_bad["margin_g_mm"], 0.0)
        r_no_limit = worst_case_u1(pl, 100.0, {1: 1.0})
        self.assertIsNone(r_no_limit["within_limit"])
        self.assertIsNone(r_no_limit["excess_g_mm"])

    def test_error_bound_cannot_exceed_nominal_mass(self):
        with self.assertRaises(ValueError):
            worst_case_u1([_pl(1, 0, 0.0, 50)], 100.0, {1: 50.1})

    def test_negative_error_rejected(self):
        with self.assertRaises(ValueError):
            worst_case_u1([_pl(1, 0, 0.0, 50)], 100.0, {1: -0.01})

    def test_negative_limit_rejected(self):
        with self.assertRaises(ValueError):
            worst_case_u1([_pl(1, 0, 0.0, 50)], 100.0, {1: 0.0},
                          u1_limit_g_mm=-1.0)

    def test_error_at_full_nominal_mass_means_zero_allowed(self):
        # e == m 时实际质量区间 [0, 2m], 0 质量允许(称重下界)
        r = worst_case_u1([_pl(1, 0, 0.0, 50)], 100.0, {1: 50.0})
        self.assertAlmostEqual(r["worst_case"]["u1_mag_g_mm"], 10000.0, places=6)


if __name__ == "__main__":
    unittest.main()
