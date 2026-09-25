"""已保存配平方案的复称规划(只读, 不改变求解与保存格式)。

问题模型
--------
方案确定后, 每支试管 k 固定在槽位 θ_k, 标称质量 m_k, 并有当前称重质量
误差上界 e_k(0 ≤ e_k ≤ m_k; 未录入的试管按 0 处理)。实际质量在
[m_k − e_k, m_k + e_k] 内独立正负变化, 最不利一阶不平衡量由
app.weighing.worst_case_u1 精确求得(二维 zonotope 顶点枚举)。

复称(重新称量)试管 k 只把该管的误差上界降为**两者较小值** min(e_k, b)
(b 为复称后的统一误差上界), 标称质量与落槽不变。在最多复称 M 支
(0~3)的限制内, 寻找一组复称试管, 使复称后的最不利 |U₁| 不超过一阶
不平衡量限值 L。

精确搜索(按复称管数分层完全枚举)
--------------------------------
只有当前误差上界 e_k > b 的试管, 复称后误差上界才会真正下降(有效候选,
共 c 支); 复称其余试管不改变任何误差上界, 不可能改善结果, 也不会出现在
最优组合中, 因此只对有效候选枚举大小不超过 M 的子集:

    子集数 = Σ_{k=0..M} C(c, k)        (n ≤ 24, M ≤ 3 时至多 2325 个)

每个子集把被复称管的误差上界改为 min(e_k, b) 后, 调用现有的
worst_case_u1(O(n log n)) 得到该复称组合下精确的最不利 |U₁|。

目标排序(字典序, 确定性)
------------------------
1. 能达标(复称后最不利 |U₁| ≤ L)的**最少复称管数** k* —— 从 k=0 起
   逐层枚举, 首个存在达标组合的 k 即 k*;
2. 在大小为 k* 的达标组合中取复称后最不利 |U₁| **最低**者;
3. 数值容差(1e-9 相对容差)内并列时, 按**管号序列升序**(字典序)裁决,
   并统计达到该最优结果的组合数。

若 0..M 内均无法达标, 给出次数内最不利 |U₁| 最低的组合(并列时先取复称
管数少者, 再按管号序列升序)及超限量, 明确判定**不达标**, 不误报达标。

拒绝条件(ValueError → HTTP 400)
-------------------------------
- 限值或复称后统一误差上界不是有限非负数, 或最多复称次数不在 0~3;
- 方案没有已放置试管, 或已放置试管超过 24 支(精确枚举规模上限);
- 误差上界为负、非有限或超过该管标称质量; 试管编号不在摆放明细中;
- 摆放明细中试管编号重复、质量非正、缺少槽位角度; 半径非法。
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Optional

from .weighing import worst_case_u1

# 精确枚举规模上限: 已放置试管 24 支
# (组合数 Σ_{k=0..3} C(24, k) = 2325, 每组合一次 O(n log n) 最不利值计算)
MAX_TUBES = 24
# 最多复称次数上限
MAX_REWEIGH = 3
# 与 app/evacuation.py / app/loading.py 一致的数值容差
_NUM_TOL = 1e-9


def plan_reweigh(plan: dict[str, Any],
                 tube_errors: dict[int, float],
                 reweighed_error_bound_g: float,
                 u1_limit_g_mm: float,
                 max_reweigh_count: int) -> dict[str, Any]:
    """对已保存方案精确规划复称组合(只读)。

    参数
    ----
    plan: GET /api/plan/{id} 返回结构的方案(含 id/name/request/result),
        仅读取, 不修改。
    tube_errors: {tube_id: e_k}, 各管当前非负质量误差上界(g),
        e_k ≤ m_k; 未列出的试管按 0 处理。
    reweighed_error_bound_g: 复称后的统一误差上界(g), 有限非负数;
        复称某管只把该管上界降至 min(e_k, b)。
    u1_limit_g_mm: 一阶不平衡量限值(g·mm), 必填的有限非负数。
    max_reweigh_count: 最多复称次数, 0~3。

    返回
    ----
    含复称管号、复称前后最不利 |U₁|、达标判定(或次数内最优组合与
    超限量)及逐管误差上界变化的字典; ``feasible=False`` 时不误报达标。
    """
    plan_id = plan.get("id")

    # ---- 限值 / 复称后统一上界 / 次数上限 ----
    limit = float(u1_limit_g_mm)
    if not math.isfinite(limit):
        raise ValueError("一阶不平衡量限值必须为有限数值。")
    if limit < 0.0:
        raise ValueError("一阶不平衡量限值不能为负。")
    bound = float(reweighed_error_bound_g)
    if not math.isfinite(bound):
        raise ValueError("复称后的统一误差上界必须为有限数值。")
    if bound < 0.0:
        raise ValueError("复称后的统一误差上界不能为负。")
    M = int(max_reweigh_count)
    if not (0 <= M <= MAX_REWEIGH):
        raise ValueError(f"最多复称次数必须在 0~{MAX_REWEIGH} 之间(当前 {M})。")

    # ---- 半径 ----
    result = plan.get("result") or {}
    cfg = result.get("config") or {}
    radius = cfg.get("radius_mm")
    if radius is None:
        radius = (plan.get("request") or {}).get("radius_mm", 100.0)
    radius = float(radius)
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError("方案中的转子半径必须为正数。")

    # ---- 摆放明细与逐管当前误差上界 ----
    used = [p for p in (result.get("placements") or [])
            if p.get("slot") is not None]
    if not used:
        raise ValueError(
            f"方案 {plan_id} 没有已放置的试管, 无法进行复称规划。"
        )
    if len(used) > MAX_TUBES:
        raise ValueError(
            f"方案 {plan_id} 有 {len(used)} 支已放置试管, 超过复称规划上限 "
            f"{MAX_TUBES} 支(精确枚举的组合数随管数增长), 明确拒绝。"
        )

    errors: dict[int, float] = {}
    for raw_tid, raw_e in (tube_errors or {}).items():
        tid = int(raw_tid)
        e = float(raw_e)
        if not math.isfinite(e):
            raise ValueError(f"试管 {tid} 的质量误差上界必须为有限数值。")
        if e < 0.0:
            raise ValueError(f"试管 {tid} 的质量误差上界不能为负。")
        if tid in errors:
            raise ValueError(f"试管 {tid} 的误差上界重复填写。")
        errors[tid] = e

    masses: dict[int, float] = {}
    info: dict[int, dict[str, Any]] = {}
    for p in used:
        tid = int(p["tube_id"])
        if tid in masses:
            raise ValueError(f"摆放明细中试管 {tid} 重复。")
        mass = float(p["mass"])
        if not math.isfinite(mass) or mass <= 0.0:
            raise ValueError(f"试管 {tid} 的标称质量必须为正数。")
        angle = p.get("angle")
        if angle is None or not math.isfinite(float(angle)):
            raise ValueError(f"试管 {tid} 的摆放结果缺少有效槽位角度。")
        masses[tid] = mass
        info[tid] = {"slot": int(p["slot"]), "angle_deg": float(angle)}
    for tid, e in errors.items():
        if tid not in masses:
            raise ValueError(
                f"试管 {tid} 不在方案 {plan_id} 的摆放明细中。"
            )
        if e > masses[tid] + 1e-9:
            raise ValueError(
                f"试管 {tid} 的质量误差上界({e:g} g)不能超过其标称质量"
                f"({masses[tid]:g} g)。"
            )
    tube_ids = sorted(masses)
    n = len(tube_ids)
    base_errors = {tid: min(errors.get(tid, 0.0), masses[tid])
                   for tid in tube_ids}

    # 有效候选: 复称后误差上界确实下降(e_k > b)的试管; 复称其余试管
    # 不改变任何误差上界, 不可能改善结果
    candidates = [tid for tid in tube_ids if base_errors[tid] > bound]
    candidate_set = set(candidates)

    limit_tol = _NUM_TOL * max(1.0, limit)
    placements = result.get("placements") or []

    def _errors_for(combo: tuple[int, ...]) -> dict[int, float]:
        chosen = set(combo)
        return {
            tid: (min(base_errors[tid], bound)
                  if tid in chosen else base_errors[tid])
            for tid in tube_ids
        }

    def _worst_mag(combo: tuple[int, ...]) -> float:
        check = worst_case_u1(
            placements=placements,
            radius_mm=radius,
            errors=_errors_for(combo),
        )
        return float(check["worst_case"]["u1_mag_g_mm"])

    # 复称前(k=0, 空组合)的精确最不利值, 即现有称重误差校核结果
    base_check = worst_case_u1(
        placements=placements, radius_mm=radius, errors=dict(base_errors),
    )
    base_mag = float(base_check["worst_case"]["u1_mag_g_mm"])

    # ---- 按复称管数从少到多精确搜索 ----
    evaluated = 0
    winner: Optional[dict[str, Any]] = None       # 首个达标 k 上的最优组合
    best_effort: Optional[dict[str, Any]] = None  # 0..M 内最不利值最低的组合
    for k in range(0, M + 1):
        leaves: list[tuple[float, tuple[int, ...]]] = []
        for combo in combinations(candidates, k):
            mag = base_mag if not combo else _worst_mag(combo)
            evaluated += 1
            leaves.append((mag, combo))
        if not leaves:
            continue
        # 该复称管数下的全局最优(用于无法达标时的对照)
        k_best_mag = min(m for m, _ in leaves)
        k_tol = _NUM_TOL * max(1.0, k_best_mag)
        k_tied = [(m, c) for m, c in leaves if m <= k_best_mag + k_tol]
        k_best_key = min(c for _, c in k_tied)
        k_best_mag = next(m for m, c in k_tied if c == k_best_key)
        if best_effort is None or k_best_mag < best_effort["mag"] - _NUM_TOL:
            best_effort = {"k": k, "mag": k_best_mag, "key": k_best_key,
                           "count": len(k_tied)}
        # 达标组合(第一个存在达标组合的 k 即最少复称管数)
        feasible_leaves = [(m, c) for m, c in leaves
                           if m <= limit + limit_tol]
        if feasible_leaves:
            best_mag = min(m for m, _ in feasible_leaves)
            mag_tol = _NUM_TOL * max(1.0, best_mag)
            optimal = [(m, c) for m, c in feasible_leaves
                       if m <= best_mag + mag_tol]
            opt_key = min(c for _, c in optimal)
            opt_mag = next(m for m, c in optimal if c == opt_key)
            winner = {"k": k, "mag": opt_mag, "key": opt_key,
                      "count": len(optimal)}
            break

    # k=0 的空组合必然被评估, 故 best_effort 一定存在
    assert best_effort is not None
    chosen = winner if winner is not None else best_effort
    chosen_key: tuple[int, ...] = chosen["key"]
    chosen_set = set(chosen_key)

    # 采用推荐组合复称后的精确最不利值(空组合即复称前结果, 直接复用)
    if chosen_key:
        final_check = worst_case_u1(
            placements=placements, radius_mm=radius,
            errors=_errors_for(chosen_key),
        )
    else:
        final_check = base_check
    final_mag = float(final_check["worst_case"]["u1_mag_g_mm"])

    # 逐管误差上界变化(推荐组合); 标称质量与落槽不变
    tube_rows: list[dict[str, Any]] = []
    for tid in tube_ids:
        before = base_errors[tid]
        after = min(before, bound) if tid in chosen_set else before
        tube_rows.append({
            "tube_id": tid,
            "slot": info[tid]["slot"],
            "angle_deg": info[tid]["angle_deg"],
            "nominal_mass_g": masses[tid],
            "error_bound_before_g": before,
            "error_bound_after_g": after,
            "reweighed": tid in chosen_set,
            "candidate": tid in candidate_set,
        })

    feasible = winner is not None
    within = final_mag <= limit + limit_tol
    base_within = base_mag <= limit + limit_tol
    excess = max(0.0, final_mag - limit)
    margin = max(0.0, limit - final_mag)
    ids_txt = ", ".join(str(t) for t in chosen_key)

    if feasible and not chosen_key:
        reason = (
            f"复称前最不利 |U₁|={base_mag:.6g} g·mm 已不超过限值 "
            f"{limit:g} g·mm, 无需复称(复称 0 支)。"
        )
    elif feasible:
        reason = (
            f"复称 {chosen['k']} 支试管(管号 {ids_txt})即可达标: "
            f"复称后最不利 |U₁|={final_mag:.6g} g·mm ≤ 限值 {limit:g} g·mm"
            f"(复称前 {base_mag:.6g} g·mm); 这是能达标的最少复称管数。"
        )
    else:
        reason = (
            f"在最多复称 {M} 支的限制内共精确评估 {evaluated} 种复称组合, "
            f"没有使最不利 |U₁| ≤ 限值 {limit:g} g·mm 的组合; "
            f"次数内最不利值最低的组合为复称 {chosen['k']} 支"
            f"(管号 {ids_txt or '无'}), 复称后最不利 |U₁|={final_mag:.6g} "
            f"g·mm, 仍超出 {excess:.6g} g·mm, 判定不达标。"
        )
    if not candidates:
        reason += (
            " 注意: 复称后统一误差上界不低于任何试管的当前误差上界, "
            "复称不能降低任何管的误差上界。"
        )

    return {
        "method": (
            "按复称管数分层的完全枚举(有效候选子集, "
            f"管数≤{MAX_TUBES}、复称上限 0~{MAX_REWEIGH}): "
            "每个复称组合调用二维 zonotope 顶点枚举的精确最不利 |U₁| "
            "计算; 先求能达标的最少复称管数, 再在该管数内取最不利值最低"
            "的组合, 并列按管号序列升序裁决; 次数内无法达标时给出最不利"
            "值最低的组合与超限量, 不误报达标"
        ),
        "feasible": feasible,
        "plan": {
            "id": plan.get("id"),
            "name": plan.get("name"),
            "created_at": plan.get("created_at"),
        },
        "radius_mm": radius,
        "tube_count": n,
        "candidate_tube_ids": candidates,
        "u1_limit_g_mm": limit,
        "reweighed_error_bound_g": bound,
        "max_reweigh_count": M,
        "evaluated_combinations": evaluated,
        "baseline": {
            "nominal": base_check["nominal"],
            "worst_case": base_check["worst_case"],
            "within_limit": base_within,
            "excess_g_mm": max(0.0, base_mag - limit),
            "margin_g_mm": max(0.0, limit - base_mag),
        },
        "reweigh_count": chosen["k"],
        "reweighed_tube_ids": sorted(chosen_key),
        "optimal_combination_count": chosen["count"],
        "tie_broken": chosen["count"] > 1,
        "tubes": tube_rows,
        "final": {
            "nominal": final_check["nominal"],
            "worst_case": final_check["worst_case"],
            "actual_masses": final_check["actual_masses"],
        },
        "within_limit": within,
        "excess_g_mm": excess,
        "margin_g_mm": margin,
        "improvement_g_mm": base_mag - final_mag,
        "reason": reason,
        "note": (
            "复称规划为只读规划结果, 未修改任何已保存方案; "
            "复称只把被复称管的误差上界降至两者较小值, "
            "标称质量与落槽不变。"
        ),
    }
