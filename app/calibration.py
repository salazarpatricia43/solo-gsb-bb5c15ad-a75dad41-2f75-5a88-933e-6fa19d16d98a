"""已保存配平方案的实测振动校准(只读, 不改变方案与保存格式)。

单平面影响系数法(单面动平衡)
---------------------------

在**同一转速**下测取两组振动读数(同一测点、同一相位参考):

    V0 = A0 · e^(iφ0)     试重前(原始)振动复数向量
    V1 = A1 · e^(iφ1)     装入试重后振动复数向量

试重质量 m_t 放在槽位 θ_t(该槽在方案中必须**空闲且启用**), 其质径积为:

    U_t = m_t · R · e^(iθ_t)         (g·mm)

线性假设下振动变化只由该试重引起, 影响系数:

    α = (V1 − V0) / U_t              (振动单位 / g·mm)

试重**视为已取下**, 因此在任一空闲启用槽位 s 装入单枚可用配重 m 后的预测振动:

    V(m, s) = V0 + α · m · R · e^(iθ_s)

逐空闲槽位 × 逐可用配重质量求 |V(m, s)|, 按残余幅值升序排序, 首项即推荐。
连续意义下的理想配重(仅供对照, 不受槽位/离散质量限制):

    U* = −V0 / α,  m* = |U*| / R,  θ* = arg(U*)

拒绝条件
--------
- 读数幅值/相位与质量必须有限且非负, 质量(试重与可用配重)必须严格大于零;
- 试重槽位必须存在、启用且未被方案占用;
- 试重前后振动向量差的模相对读数尺度接近零(|V1−V0| ≈ 0)时, 影响系数近零,
  此时试重没有产生可辨识的响应(读数无变化或系统已不可观测), **明确拒绝**
  给出配重推荐, 而不是外推一个数值上不稳定的结果。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# 影响系数近零判据: |ΔV| / max(|V0|, |V1|) 不超过该相对容差即视为近零
_NEAR_ZERO_REL = 1e-9


def _require_finite(name: str, value: float) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name}必须为有限数值(不能为 NaN 或无穷大)。")
    return v


def _require_nonneg(name: str, value: float) -> float:
    v = _require_finite(name, value)
    if v < 0.0:
        raise ValueError(f"{name}不能为负(当前 {v:g})。")
    return v


def _require_positive(name: str, value: float) -> float:
    v = _require_finite(name, value)
    if v <= 0.0:
        raise ValueError(f"{name}必须大于零(当前 {v:g})。")
    return v


def _phasor(angle_deg: float, magnitude: float) -> complex:
    a = math.radians(angle_deg % 360.0)
    return complex(magnitude * math.cos(a), magnitude * math.sin(a))


def _vec_block(z: complex) -> dict[str, Any]:
    return {
        "x": z.real,
        "y": z.imag,
        "amplitude": abs(z),
        "phase_deg": math.degrees(math.atan2(z.imag, z.real)) % 360.0,
    }


def predict_single_weight_balancing(
    slot_state: list[dict[str, Any]],
    radius_mm: float,
    *,
    initial_amplitude: float,
    initial_phase_deg: float,
    trial_amplitude: float,
    trial_phase_deg: float,
    trial_mass_g: float,
    trial_slot: int,
    available_masses: list[float],
) -> dict[str, Any]:
    """基于试重前后的复数振动读数求影响系数, 并逐空槽预测单枚配重效果。

    参数
    ----
    slot_state: 方案结果中的槽位状态, 每项含 slot / angle / enabled / tube_id;
        tube_id 为空即空闲槽。
    radius_mm: 转子半径(mm), 取自方案, 调用方不允许改写。
    initial_amplitude / initial_phase_deg: 试重前振动幅值与相位(°), 非负有限。
    trial_amplitude / trial_phase_deg: 装入试重后振动幅值与相位(°), 非负有限。
    trial_mass_g: 试重质量(g), 必须 > 0。
    trial_slot: 试重所在槽位索引, 必须存在、启用且空闲。
    available_masses: 候选单枚配重质量(g)列表, 每个都必须 > 0, 至少一个。

    返回
    ----
    含输入读数向量、影响系数、理想连续配重、以及按残余幅值排序的逐槽逐质量
    预测明细(首项为推荐); 不修改入参对应的方案数据。
    """
    R = _require_positive("转子半径", radius_mm)
    a0 = _require_nonneg("试重前振动幅值", initial_amplitude)
    p0 = _require_nonneg("试重前振动相位", initial_phase_deg)
    a1 = _require_nonneg("试重后振动幅值", trial_amplitude)
    p1 = _require_nonneg("试重后振动相位", trial_phase_deg)
    mt = _require_positive("试重质量", trial_mass_g)

    if not isinstance(available_masses, (list, tuple)) or len(available_masses) < 1:
        raise ValueError("请至少提供一个可用配重质量。")
    masses: list[float] = []
    for i, raw in enumerate(available_masses):
        m = _require_positive(f"可用配重质量 #{i + 1}", raw)
        if m not in masses:  # 精确去重, 保持录入顺序
            masses.append(m)

    # ---- 方案槽位(只读): 试重槽必须存在/启用/空闲 ----
    slots: dict[int, dict[str, Any]] = {}
    for item in slot_state:
        idx = int(item["slot"])
        angle = _require_finite(f"槽位 {idx} 角度", float(item["angle"]))
        slots[idx] = {
            "angle": angle,
            "enabled": bool(item.get("enabled", True)),
            "occupied": item.get("tube_id") is not None,
        }
    if not slots:
        raise ValueError("方案中没有槽位信息, 无法进行实测振动校准。")

    ts = slots.get(int(trial_slot))
    if ts is None:
        raise ValueError(f"试重槽位 {trial_slot} 不存在。")
    if not ts["enabled"]:
        raise ValueError(f"试重槽位 {trial_slot} 已被禁用, 试重槽位必须是启用槽位。")
    if ts["occupied"]:
        raise ValueError(
            f"试重槽位 {trial_slot} 已被方案中的试管占用, 试重槽位必须空闲。"
        )

    free_slots = [
        {"slot": idx, "angle_deg": s["angle"]}
        for idx, s in slots.items()
        if s["enabled"] and not s["occupied"]
    ]
    free_slots.sort(key=lambda f: f["slot"])

    # ---- 复数读数与影响系数 ----
    v0 = _phasor(p0, a0)
    v1 = _phasor(p1, a1)
    dv = v1 - v0
    reading_scale = max(abs(v0), abs(v1))
    if reading_scale <= 0.0 or abs(dv) <= _NEAR_ZERO_REL * reading_scale:
        raise ValueError(
            "试重前、后振动向量几乎没有变化(|V₁−V₀|≈"
            f"{abs(dv):.3g}), 影响系数接近零: 试重未产生可辨识的振动响应, "
            "无法可靠反推影响系数, 明确拒绝给出配重推荐。"
            "请检查试重是否过轻、两次读数是否同一转速/测点/相位参考。"
        )

    u_trial = mt * R * _phasor(ts["angle"], 1.0)
    alpha = dv / u_trial

    # ---- 连续理想配重(仅供对照) ----
    u_star = -v0 / alpha if abs(alpha) > 0.0 else 0j
    ideal = {
        "unbalance_g_mm": abs(u_star),
        "direction_deg": (
            math.degrees(math.atan2(u_star.imag, u_star.real)) % 360.0
        ),
        "equivalent_mass_g": abs(u_star) / R,
    }

    # ---- 逐空闲启用槽位 × 逐可用质量: 试重已取下, 基线是 V0 ----
    candidates: list[dict[str, Any]] = []
    for f in free_slots:
        u_dir = _phasor(f["angle_deg"], 1.0)
        for m in masses:
            vp = v0 + alpha * (m * R * u_dir)
            residual = abs(vp)
            improvement = a0 - residual
            candidates.append({
                "slot": f["slot"],
                "angle_deg": f["angle_deg"],
                "mass_g": m,
                "predicted": _vec_block(vp),
                "improvement": improvement,
                "improvement_pct": (
                    improvement / a0 * 100.0 if a0 > 0.0 else None
                ),
            })
    candidates.sort(key=lambda c: (c["predicted"]["amplitude"], c["slot"], c["mass_g"]))
    for rank, c in enumerate(candidates, start=1):
        c["rank"] = rank

    best: Optional[dict[str, Any]] = candidates[0] if candidates else None
    return {
        "method": (
            "单平面影响系数法: α=(V₁−V₀)/(m_t·R·e^{iθ_t}); "
            "试重视为已取下, V(m,s)=V₀+α·m·R·e^{iθ_s}, "
            "逐空闲启用槽位与可用配重质量枚举单枚配重并按残余幅值排序"
        ),
        "radius_mm": R,
        "readings": {
            "initial": _vec_block(v0),
            "with_trial": _vec_block(v1),
            "delta": _vec_block(dv),
        },
        "trial": {
            "slot": int(trial_slot),
            "angle_deg": ts["angle"],
            "mass_g": mt,
            "unbalance_g_mm": abs(u_trial),
        },
        "influence_coefficient": {
            "x": alpha.real,
            "y": alpha.imag,
            "magnitude": abs(alpha),
            "phase_deg": (
                math.degrees(math.atan2(alpha.imag, alpha.real)) % 360.0
            ),
            "units": "振动单位/(g·mm)",
        },
        "free_enabled_slots": free_slots,
        "ideal_correction": ideal,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "best": best,
        "note": (
            "试重已视为取下: 预测以试重前读数 V₀ 为基线, 仅叠加单枚配重"
            "m·R·e^{iθ_s} 的线性影响; 结果只读, 不修改原方案。"
        ),
    }
