"""已保存配平方案的称重误差校核(只读, 不改变求解与保存格式)。

问题模型
--------
方案确定后, 每支试管 k 固定在槽位 θ_k, 标称质量 m_k。称重允许的实际质量
在区间内独立地正负变化:

    m_k(actual) = m_k + δ_k,   -e_k ≤ δ_k ≤ e_k,  0 ≤ e_k ≤ m_k

一阶不平衡量(质径积):

    U₁ = R · Σ_k (m_k + δ_k) · e^{iθ_k}
       = U₁_nom + R · Σ_k δ_k · e^{iθ_k}

各管误差彼此独立, 因此 U₁ 的可达集合是以 U₁_nom 为中心、以
{R·e_k·e^{iθ_k}} 为生成元的二维 zonotope(中心对称凸多边形)。

精确最不利值
------------
凸多边形上 |z| = max_z <z, u/|u|> 的最大模必在某个顶点处取得
(支撑函数性质), 故只需枚举 zonotope 的顶点, 不需枚举 2^n 个误差组合:

1. 把每个生成元方向折到半平面 [0, π): 若 θ_k ∈ [π, 2π), 取方向 θ_k-π
   并同时翻转生成元符号(δ_k 区间对称, 翻转后误差变量等价换号);
2. 按方向角排序, 任取一条不与任何生成元平行的方向作为起点, 边界顶点
   上每个生成元都取端点(+1 或 -1), 沿多边形边界每跨过一个生成元方向,
   恰好一个生成元翻转端点;
3. 逐顶点求 |U₁|, 取最大值, 并记录该顶点对应的各管误差 δ_k, 即得到
   一组达到最不利 |U₁| 的实际质量。

复杂度 O(n log n)(排序), 顶点数至多 2n。注意: 最不利不平衡量是**向量和**
的模, 不能把各管 |δ_k|·R 直接标量相加——那相当于强行令全部误差投影
同向, 只有当所有占用槽位方向一致时才恰好等于真值。
"""
from __future__ import annotations

import math
from typing import Any, Optional


def worst_case_u1(placements: list[dict[str, Any]], radius_mm: float,
                  errors: dict[int, float],
                  u1_limit_g_mm: Optional[float] = None) -> dict[str, Any]:
    """在各管称重误差可独立正负变化的区间内, 精确求最不利一阶不平衡量。

    参数
    ----
    placements: 方案结果中的摆放明细, 每项需含 tube_id / slot / angle /
        mass(标称质量); 仅校核 slot 不为空的试管。
    radius_mm: 转子半径(mm), 取自方案, 调用方不允许改写。
    errors: {tube_id: e_k}, 非负质量误差上界(g), e_k ≤ m_k;
        未列出的试管按 e_k = 0 处理(标称质量无误差)。
    u1_limit_g_mm: 一阶不平衡量限值(g·mm), 可空; 非空时必须非负。

    返回
    ----
    含标称/最不利指标、一组达到最不利值的实际质量及逐管误差贡献的字典。
    """
    if radius_mm <= 0:
        raise ValueError("转子半径必须为正数。")

    limit = None
    if u1_limit_g_mm is not None:
        limit = float(u1_limit_g_mm)
        if limit < 0.0:
            raise ValueError("一阶不平衡量限值不能为负。")

    used = [p for p in placements if p.get("slot") is not None]
    if not used:
        raise ValueError("该方案没有已放置的试管, 无法进行误差校核。")

    tubes: list[dict[str, Any]] = []
    seen: set[int] = set()
    for p in used:
        tid = int(p["tube_id"])
        if tid in seen:
            raise ValueError(f"摆放明细中试管 {tid} 重复。")
        seen.add(tid)
        mass = float(p["mass"])
        if mass <= 0:
            raise ValueError(f"试管 {tid} 的标称质量必须为正数。")
        err = float(errors.get(tid, 0.0))
        if err < 0.0:
            raise ValueError(f"试管 {tid} 的质量误差上界不能为负。")
        if err > mass + 1e-9:
            raise ValueError(
                f"试管 {tid} 的质量误差上界({err:g} g)不能超过其标称质量"
                f"({mass:g} g)。"
            )
        angle = p.get("angle")
        if angle is None:
            raise ValueError(f"试管 {tid} 的摆放结果缺少槽位角度。")
        tubes.append({
            "tube_id": tid,
            "slot": int(p["slot"]),
            "angle_deg": float(angle),
            "mass": mass,
            "err": min(err, mass),
        })
    tubes.sort(key=lambda t: (t["slot"], t["tube_id"]))

    R = float(radius_mm)

    # 标称一阶向量(单位质量复数和), U = R * complex(...)
    nom_x = sum(t["mass"] * math.cos(math.radians(t["angle_deg"])) for t in tubes)
    nom_y = sum(t["mass"] * math.sin(math.radians(t["angle_deg"])) for t in tubes)

    # zonotope 生成元: g_k = e_k · (cosθ, sinθ), 折到方向角 β∈[0,π)
    gens: list[dict[str, Any]] = []
    for k, t in enumerate(tubes):
        theta = math.radians(t["angle_deg"])
        gx, gy = t["err"] * math.cos(theta), t["err"] * math.sin(theta)
        beta = math.atan2(gy, gx)
        sigma = 1.0
        if beta < 0.0:
            beta += math.pi
            sigma = -1.0
        gens.append({
            "idx": k,
            "beta": beta,
            # 折半平面后的生成元 = σ·e·e^{iθ}; 实际误差系数 = σ·s(s=±1)
            "gx": sigma * gx,
            "gy": sigma * gy,
            "sigma": sigma,
            "active": t["err"] > 0.0,
        })
    gens.sort(key=lambda g: g["beta"])

    # p 起点: 全部生成元取 -1 端点的 zonotope 顶点偏移(先全 -, 再扫到全 +,
    # 再沿后半圈回扫; 每跨一个方向角恰有一个生成元翻转, 覆盖至多 2n 个顶点)
    signs = [-1] * len(gens)
    px = -sum(g["gx"] for g in gens)
    py = -sum(g["gy"] for g in gens)

    best: Optional[dict[str, Any]] = None

    def evaluate() -> None:
        nonlocal best
        ux, uy = nom_x + px, nom_y + py
        mag2 = ux * ux + uy * uy
        if best is None or mag2 > best["mag2"] + 1e-12:
            best = {"mag2": mag2, "x": ux, "y": uy,
                    "signs": signs.copy()}

    if not gens:  # 理论上不会(已校验有摆放), 保险
        evaluate()
    else:
        evaluate()
        for i in range(len(gens)):
            px += 2.0 * gens[i]["gx"]
            py += 2.0 * gens[i]["gy"]
            signs[i] = 1
            evaluate()
        for i in range(len(gens)):
            px -= 2.0 * gens[i]["gx"]
            py -= 2.0 * gens[i]["gy"]
            signs[i] = -1
            if i < len(gens) - 1:
                evaluate()

    assert best is not None
    # 记录最不利顶点处各管实际误差系数: δ_k = σ_k·s_k·e_k
    coef_by_idx: dict[int, float] = {}
    for g, s in zip(gens, best["signs"]):
        coef_by_idx[g["idx"]] = g["sigma"] * s

    worst_x, worst_y = R * best["x"], R * best["y"]
    nom_x_r, nom_y_r = R * nom_x, R * nom_y
    nom_mag = math.hypot(nom_x_r, nom_y_r)
    worst_mag = math.hypot(worst_x, worst_y)
    # 标量直和: R·Σ e_k。它是误差向量模 |ΔU₁| 的三角不等式上界;
    # 把它直接叠加到标称模长上(|U₁_nom| + R·Σe_k)即“误差绝对值直接相加”
    # 的朴素最不利估计, 仅在最不利顶点处各误差贡献都与合力同向时才精确。
    abs_dev_bound = R * sum(t["err"] for t in tubes)
    naive_worst = nom_mag + abs_dev_bound

    # 最不利 U₁ 方向的单位向量: 逐管误差投影(可达顶点处恒为非负)
    if worst_mag > 1e-15:
        dir_x, dir_y = worst_x / worst_mag, worst_y / worst_mag
    else:
        dir_x, dir_y = 0.0, 0.0

    tube_rows: list[dict[str, Any]] = []
    for k, t in enumerate(tubes):
        coef = coef_by_idx[k]
        delta = coef * t["err"]
        actual = t["mass"] + delta
        theta = math.radians(t["angle_deg"])
        along = R * delta * (math.cos(theta) * dir_x + math.sin(theta) * dir_y)
        tube_rows.append({
            "tube_id": t["tube_id"],
            "slot": t["slot"],
            "angle_deg": t["angle_deg"],
            "nominal_mass_g": t["mass"],
            "mass_error_bound_g": t["err"],
            "error_sign_at_worst": (1 if coef > 0 else -1 if coef < 0 else 0),
            "delta_g": delta,
            "actual_mass_g": actual,
            # 该管误差在最不利 U₁ 方向上的投影(g·mm), 恒非负
            "u1_along_worst_g_mm": along,
        })

    def _block(x: float, y: float, mag: float) -> dict[str, Any]:
        return {
            "u1_mag_g_mm": mag,
            "u1_angle_deg": math.degrees(math.atan2(y, x)) % 360.0,
            "u1_x_g_mm": x,
            "u1_y_g_mm": y,
        }

    nominal = _block(nom_x_r, nom_y_r, nom_mag)
    worst = _block(worst_x, worst_y, worst_mag)
    within = None if limit is None else worst_mag <= limit + 1e-9
    return {
        "method": (
            "二维 zonotope 顶点枚举: 各管误差在 [-e_k, e_k] 独立变化, "
            "生成元按方向角排序后沿凸多边形边界逐顶点求模, O(n log n), "
            "不枚举 2^n 组合"
        ),
        "radius_mm": R,
        "tube_count": len(tubes),
        "vertex_count": 2 * sum(1 for g in gens if g["active"]) or 1,
        "nominal": nominal,
        "worst_case": worst,
        "actual_masses": [
            {"tube_id": r["tube_id"], "slot": r["slot"],
             "actual_mass_g": r["actual_mass_g"], "delta_g": r["delta_g"]}
            for r in tube_rows
        ],
        "tubes": tube_rows,
        "u1_limit_g_mm": limit,
        "within_limit": within,
        "excess_g_mm": (None if limit is None else max(0.0, worst_mag - limit)),
        "margin_g_mm": (None if limit is None else max(0.0, limit - worst_mag)),
        "worst_minus_nominal_g_mm": worst_mag - nom_mag,
        "naive_abs_worst_g_mm": naive_worst,
        "abs_dev_bound_g_mm": abs_dev_bound,
        "naive_equals_exact": abs(naive_worst - worst_mag) <= 1e-9 * max(1.0, worst_mag),
    }
