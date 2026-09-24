"""实测振动校准逻辑测试: python -m unittest discover -s tests

核心断言:
- 合成线性模型下, 影响系数必须精确还原, 且最优预测在理想配重方向处残余最小;
- 预测基线是试重前读数(试重视为已取下), 不含试重贡献;
- 试重槽必须空闲启用; 读数有限非负、质量严格为正;
- 试重前后振动向量差近零时必须明确拒绝(影响系数近零)。
"""
import math
import unittest

from app.calibration import predict_single_weight_balancing


def slot_state(n=12, occupied=(), disabled=()):
    return [
        {
            "slot": i,
            "angle": 30.0 * i,
            "enabled": i not in disabled,
            "tube_id": (100 + i if i in occupied else None),
        }
        for i in range(n)
    ]


def cvec(amp, phase_deg):
    a = math.radians(phase_deg)
    return complex(amp * math.cos(a), amp * math.sin(a))


class TestVibrationCalibration(unittest.TestCase):
    def test_alpha_recovered_and_best_slot_is_ideal_direction(self):
        # 合成线性系统: 原始振动 5.0∠30°, α = 1e-4∠60° (单位/g·mm)
        R = 100.0
        a0, p0 = 5.0, 30.0
        alpha = cvec(1e-4, 60.0)
        # 试重 5 g 放在槽 9 (270°)
        mt, ts = 5.0, 9
        ut = mt * R * cvec(1.0, 270.0)
        v0 = cvec(a0, p0)
        v1 = v0 + alpha * ut
        r = predict_single_weight_balancing(
            slot_state(occupied=(1, 4, 7)),
            radius_mm=R,
            initial_amplitude=a0, initial_phase_deg=p0,
            trial_amplitude=abs(v1),
            trial_phase_deg=math.degrees(math.atan2(v1.imag, v1.real)) % 360.0,
            trial_mass_g=mt, trial_slot=ts,
            available_masses=[2.0, 5.0, 8.0],
        )
        # 影响系数精确还原
        got = complex(r["influence_coefficient"]["x"], r["influence_coefficient"]["y"])
        self.assertAlmostEqual(abs(got - alpha), 0.0, places=12)
        # 试重槽在空闲启用列表中
        self.assertIn(ts, [f["slot"] for f in r["free_enabled_slots"]])
        # 排序: 候选按残余幅值升序, 首项为 best
        amps = [c["predicted"]["amplitude"] for c in r["candidates"]]
        self.assertEqual(amps, sorted(amps))
        best = r["best"]
        self.assertEqual(best["rank"], 1)
        self.assertLessEqual(best["predicted"]["amplitude"],
                             amps[1] + 1e-12)
        # 理想配重方向 = -V0/α 的方向; 最佳槽位是几何上最接近该方向的空槽
        u_star = -v0 / alpha
        ideal_ang = math.degrees(math.atan2(u_star.imag, u_star.real)) % 360.0
        self.assertAlmostEqual(r["ideal_correction"]["direction_deg"],
                               ideal_ang, places=9)
        free_angles = {f["slot"]: f["angle_deg"]
                       for f in r["free_enabled_slots"]}

        def ang_diff(a, b):
            return abs((a - b + 180.0) % 360.0 - 180.0)

        nearest = min(free_angles, key=lambda s: ang_diff(free_angles[s], ideal_ang))
        self.assertEqual(best["slot"], nearest)
        # 最佳候选的改善量 = 原幅值 - 残余幅值
        self.assertAlmostEqual(
            best["improvement"],
            a0 - best["predicted"]["amplitude"], places=9)
        self.assertAlmostEqual(
            best["improvement_pct"],
            (a0 - best["predicted"]["amplitude"]) / a0 * 100.0, places=9)

    def test_prediction_uses_initial_not_trial_baseline(self):
        # 试重已取下: 在试重槽放一枚同质量配重, 预测残余 = V1;
        # 若基线误用 V1 会得到 V1 + 试重贡献(错误)
        R = 100.0
        v0 = cvec(4.0, 100.0)
        alpha = cvec(2e-4, -40.0)
        mt, ts = 3.0, 2
        ut = mt * R * cvec(1.0, 60.0)
        v1 = v0 + alpha * ut
        r = predict_single_weight_balancing(
            slot_state(),
            radius_mm=R,
            initial_amplitude=abs(v0),
            initial_phase_deg=100.0,
            trial_amplitude=abs(v1),
            trial_phase_deg=math.degrees(math.atan2(v1.imag, v1.real)) % 360.0,
            trial_mass_g=mt, trial_slot=ts,
            available_masses=[mt],
        )
        cand = next(c for c in r["candidates"] if c["slot"] == ts)
        self.assertAlmostEqual(cand["predicted"]["amplitude"], abs(v1), places=9)
        self.assertAlmostEqual(cand["predicted"]["phase_deg"],
                               math.degrees(math.atan2(v1.imag, v1.real)) % 360.0,
                               places=9)

    def test_trial_slot_must_be_free_and_enabled(self):
        kw = dict(
            initial_amplitude=5.0, initial_phase_deg=0.0,
            trial_amplitude=6.0, trial_phase_deg=10.0,
            trial_mass_g=2.0, available_masses=[1.0],
        )
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(occupied=(3,)), radius_mm=100.0,
                trial_slot=3, **kw)               # 已占用
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(disabled=(3,)), radius_mm=100.0,
                trial_slot=3, **kw)               # 已禁用
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(), radius_mm=100.0,
                trial_slot=99, **kw)              # 不存在

    def test_non_finite_and_negative_readings_rejected(self):
        for bad in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(ValueError):
                predict_single_weight_balancing(
                    slot_state(), radius_mm=100.0,
                    initial_amplitude=bad, initial_phase_deg=0.0,
                    trial_amplitude=6.0, trial_phase_deg=10.0,
                    trial_mass_g=2.0, trial_slot=0,
                    available_masses=[1.0])

    def test_mass_must_be_positive_and_finite(self):
        for mt in (0.0, -1.0, float("inf")):
            with self.assertRaises(ValueError):
                predict_single_weight_balancing(
                    slot_state(), radius_mm=100.0,
                    initial_amplitude=5.0, initial_phase_deg=0.0,
                    trial_amplitude=6.0, trial_phase_deg=10.0,
                    trial_mass_g=mt, trial_slot=0,
                    available_masses=[1.0])
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(), radius_mm=100.0,
                initial_amplitude=5.0, initial_phase_deg=0.0,
                trial_amplitude=6.0, trial_phase_deg=10.0,
                trial_mass_g=2.0, trial_slot=0,
                available_masses=[1.0, 0.0])      # 可用配重为 0
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(), radius_mm=100.0,
                initial_amplitude=5.0, initial_phase_deg=0.0,
                trial_amplitude=6.0, trial_phase_deg=10.0,
                trial_mass_g=2.0, trial_slot=0,
                available_masses=[])               # 空列表

    def test_near_zero_delta_rejected(self):
        # 试重前后读数完全一致 -> 影响系数近零, 必须拒绝
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(), radius_mm=100.0,
                initial_amplitude=5.0, initial_phase_deg=42.0,
                trial_amplitude=5.0, trial_phase_deg=42.0,
                trial_mass_g=2.0, trial_slot=0,
                available_masses=[1.0])
        # 两个读数都为零同样拒绝
        with self.assertRaises(ValueError):
            predict_single_weight_balancing(
                slot_state(), radius_mm=100.0,
                initial_amplitude=0.0, initial_phase_deg=0.0,
                trial_amplitude=0.0, trial_phase_deg=0.0,
                trial_mass_g=2.0, trial_slot=0,
                available_masses=[1.0])

    def test_zero_initial_amplitude_pct_is_none(self):
        # 原读数为 0 但试重后非零: 可以求 α; 改善百分比不可定义, 应为 None
        v0 = 0j
        alpha = cvec(1e-4, 0.0)
        ut = 2.0 * 100.0 * cvec(1.0, 0.0)
        v1 = alpha * ut
        r = predict_single_weight_balancing(
            slot_state(), radius_mm=100.0,
            initial_amplitude=0.0, initial_phase_deg=0.0,
            trial_amplitude=abs(v1), trial_phase_deg=0.0,
            trial_mass_g=2.0, trial_slot=0,
            available_masses=[1.0])
        self.assertTrue(all(c["improvement_pct"] is None
                            for c in r["candidates"]))

    def test_disabled_and_occupied_slots_excluded(self):
        r = predict_single_weight_balancing(
            slot_state(occupied=(0, 1), disabled=(2, 3)),
            radius_mm=100.0,
            initial_amplitude=5.0, initial_phase_deg=0.0,
            trial_amplitude=6.0, trial_phase_deg=20.0,
            trial_mass_g=2.0, trial_slot=4,
            available_masses=[1.0, 2.0])
        free = {f["slot"] for f in r["free_enabled_slots"]}
        self.assertEqual(free, set(range(4, 12)))
        # 每个候选槽位都是空闲启用槽
        self.assertTrue(all(c["slot"] in free for c in r["candidates"]))
        # 候选数 = 空闲启用槽 × 去重后质量数
        self.assertEqual(r["candidate_count"], 8 * 2)

    def test_duplicate_masses_deduplicated(self):
        r = predict_single_weight_balancing(
            slot_state(), radius_mm=100.0,
            initial_amplitude=5.0, initial_phase_deg=0.0,
            trial_amplitude=6.0, trial_phase_deg=20.0,
            trial_mass_g=2.0, trial_slot=0,
            available_masses=[1.5, 1.5, 3.0])
        self.assertEqual(r["candidate_count"], 12 * 2)


if __name__ == "__main__":
    unittest.main()
