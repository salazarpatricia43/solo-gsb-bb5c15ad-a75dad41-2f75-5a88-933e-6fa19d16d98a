"""已保存配平方案的复称规划(只读, 不改变求解与保存格式)。

问题模型
--------
方案确定后, 每支试管 k 固定在槽位 θ_k, 标称质量 m_k 不变。现场已知各管
**当前质量误差上界** e_k(实际质量在 m_k ± e_k 内独立正负变化), 若对某管
重新精确称量(复称), 则该管误差上界降为

    e_k' = min(e_k, e*)

其中 e* 是复称后可保证的**统一误差上界**(所有复称管相同); 不复称的管
保持 e_k 不变。复称**不改变标称质量与落槽**。

对任一复称组合 S, 复称后的最不利一阶不平衡量直接调用
:func:`app.weighing.worst_case_u1`(二维 zonotope 顶点枚举)精确计算。

精确搜索(按复称管数分层的子集完全枚举)
--------------------------------------
设候选管 C = {k : e_k > e*}(e_k ≤ e* 的管复称后误差上界不变, 复称它是
无效果操作, 不可能出现在最少管数解中)。在至多 M 次复称(M ≤ 3)内:

1. 按复称管数 k = 0, 1, …, min(M, |C|) 分层, 枚举 C 的全部 k 元子集;
2. **第一个存在达标的组合(最不利 |U₁| ≤ L)的 k 即最少复称管数**;
3. 在该管数内取**复称后最不利 |U₁| 最低**的组合, 数值容差(1e-9 相对
   容差, 与应急迁移等精确模块一致)内并列时, 按**管号序列(升序 tuple)
   字典序**裁决, 并统计达到该最优结果的组合条数;
4. 各层都无法达标时, **不误报达标**: 返回 0..M 管内最不利值最低的组合
   及其超限量, 明确判定超限。

为在管数稍大时仍瞬时完成, 搜索层用与 ``worst_case_u1`` 同一 zonotope
顶点集合的增量顶点求值做筛选(复称只把对应生成元缩短为原来的
e*/e_k, 每个顶点位置 = 原顶点位置减去固定修正向量, 逐顶点求模取最大),
最终入选组合与基线再调用现有 ``worst_case_u1`` 做权威复算与结果输出
(含达到最不利值的一组实际质量)。zonotope 随误差上界缩小而内含缩小,
故最不利 |U₁| 对复称管集合单调不增。

拒绝条件(ValueError → HTTP 400)
-------------------------------
- 统一误差上界 / 一阶限值为负或非有限; 最多复称次数不在 0~3;
- 各管当前误差上界为负、非有限或超过其标称质量;
- 方案没有已放置试管、已放置试管超过 MAX_TUBES 支;
- 摆放明细编号/槽位重复、质量非正、缺少槽位角度; 半径非法。
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Optional

from .weighing import worst_case_u1

# 精确枚举规模上限: 至多枚举 C(MAX_TUBES, 3) 个复称组合
MAX_TUBES = 32
MAX_REWEIGHS = 3
# 与 app/evacuation.py / app/compare.py 一致的数值容差
_NUM_TOL = 1e-9


def plan_reweigh(plan: dict[str, Any],
                 tube_errors: list[dict[str, Any]],
                 unified_error_bound_g: float,
                 u1_limit_g_mm: float,
                 max_reweighs: int) -> dict[str, Any]:
    """对已保存方案规划复称组合(只读)。

    参数
    ----
    plan: GET /api/plan/{id} 返回结构的方案(含 id/name/request/result),
        仅读取, 不修改。
    tube_errors: 各管当前质量误差上界列表, 每项含 tube_id 与
        mass_error_bound_g(0 ≤ e_k ≤ m_k); 未列出的管按 0 处理。
    unified_error_bound_g: 复称后的统一误差上界 e*(g), 非负有限。
    u1_limit_g_mm: 一阶不平衡量限值 L(g·mm), 必填的有限非负数。
    max_reweighs: 最多复称次数 M(0~3)。

    返回
    ----
    含达标判定、最少复称管数与复称管号、复称前/后最不利结果、逐管误差
    上界变化与超限量的字典; 无法达标时 ``feasible=False`` 且给出次数内
    最优组合, 不误报达标。
    """
    plan_id = plan.get("id")

    # ---- 统一误差上界 / 限值 / 次数 ----
    post_bound = float(unified_error_bound_g)
    if not math.isfinite(post_bound):
        raise ValueError("复称后的统一误差上界必须为有限数值。")
    if post_bound < 0.0:
        raise ValueError("复称后的统一误差上界不能为负。")
    limit = float(u1_limit_g_mm)
    if not math.isfinite(limit):
        raise ValueError("一阶不平衡量限值必须为有限数值。")
    if limit < 0.0:
        raise ValueError("一阶不平衡量限值不能为负。")
    M = int(max_reweighs)
    if not (0 <= M <= MAX_REWEIGHS):
        raise ValueError(f"最多复称次数必须在 0~{MAX_REWEIGHS} 之间(当前 {M})。")

    # ---- 方案摆放明细(只读) ----
    result = plan.get("result") or {}
    cfg = result.get("config") or {}
    radius = cfg.get("radius_mm")
    if radius is None:
        radius = (plan.get("request") or {}).get("radius_mm", 100.0)
    radius = float(radius)
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError("方案中的转子半径必须为正数。")

    placements = [p for p in (result.get("placements") or [])
                  if p.get("slot") is not None]
    if not placements:
        raise ValueError(
            f"方案 {plan_id} 没有已放置的试管, 无法进行复称规划。"
        )
    if len(placements) > MAX_TUBES:
        raise ValueError(
            f"方案 {plan_id} 有 {len(placements)} 支已放置试管, 超过复称规划"
            f"上限 {MAX_TUBES} 支(复称组合精确枚举规模随管数组合增长), 明确拒绝。"
        )

    current_in: dict[int, float] = {}
    for item in tube_errors or []:
        tid = int(item["tube_id"])
        if tid in current_in:
            raise ValueError(f"试管 {tid} 的当前误差上界重复填写。")
        e = float(item["mass_error_bound_g"])
        if not math.isfinite(e):
            raise ValueError(f"试管 {tid} 的当前质量误差上界必须为有限数值。")
        if e < 0.0:
            raise ValueError(f"试管 {tid} 的当前质量误差上界不能为负。")
        current_in[tid] = e

    # 逐管校验并整理(顺序与 weighing 模块一致: 槽位、管号升序)
    tubes: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_slots: set[int] = set()
    for p in placements:
        tid = int(p["tube_id"])
        if tid in seen_ids:
            raise ValueError(f"摆放明细中试管 {tid} 重复。")
        seen_ids.add(tid)
        slot = int(p["slot"])
        if slot in seen_slots:
            raise ValueError(f"摆放明细中槽位 {slot} 被重复占用。")
        seen_slots.add(slot)
        mass = float(p["mass"])
        if not math.isfinite(mass) or mass <= 0.0:
            raise ValueError(f"试管 {tid} 的标称质量必须为正数。")
        angle = p.get("angle")
        if angle is None or not math.isfinite(float(angle)):
            raise ValueError(f"试管 {tid} 的摆放结果缺少有效槽位角度。")
        err = current_in.get(tid, 0.0)
        if err > mass + 1e-9:
            raise ValueError(
                f"试管 {tid} 的当前质量误差上界({err:g} g)不能超过其标称质量"
                f"({mass:g} g)。"
            )
        tubes.append({
            "tube_id": tid,
            "slot": slot,
            "angle_deg": float(angle),
            "mass": mass,
            "err": min(err, mass),
        })
    unknown = sorted(set(current_in) - seen_ids)
    if unknown:
        raise ValueError(
            f"试管 {', '.join(str(t) for t in unknown)} 不在方案 {plan_id} 的摆放明细中。"
        )
    tubes.sort(key=lambda t: (t["slot"], t["tube_id"]))
    n = len(tubes)

    def _reduced_errors(reweighed: set[int]) -> dict[int, float]:
        # 复称管取 e*, 其余保持当前上界; 标称质量与落槽不变
        return {
            t["tube_id"]: (post_bound if t["tube_id"] in reweighed else t["err"])
            for t in tubes
        }

    # ---- 复称前(当前误差上界)权威最不利结果 ----
    before = worst_case_u1(
        placements=placements,
        radius_mm=radius,
        errors={t["tube_id"]: t["err"] for t in tubes if t["err"] > 0.0},
        u1_limit_g_mm=limit,
    )
    baseline_mag = before["worst_case"]["u1_mag_g_mm"]

    # ---- 候选管: 只有 e_k > e* 的管复称后误差上界才会真正下降 ----
    # 按管号升序: combinations 按下标升序枚举, 即平局裁决所需的管号序列字典序
    cand_idx = sorted(
        (i for i, t in enumerate(tubes) if t["err"] > post_bound),
        key=lambda i: tubes[i]["tube_id"],
    )
    m = len(cand_idx)
    K = min(M, m)

    # ---- 增量 zonotope 顶点筛选(与 worst_case_u1 同一顶点集合) ----
    # 折叠生成元 g_i = σ_i·e_i·e^{iθ_i}, 方向角 β_i ∈ [0,π), 按 β 排序;
    # 复称把生成元缩短为 (e*/e_i)·g_i, 顶点 q 的新位置:
    #   p'_q = p_q - Σ_{j∈S} c_j·s_{q,j}·g_j,  c_j = (e_j-e*)/e_j
    R = radius
    nom_x = sum(t["mass"] * math.cos(math.radians(t["angle_deg"])) for t in tubes)
    nom_y = sum(t["mass"] * math.sin(math.radians(t["angle_deg"])) for t in tubes)
    gens: list[dict[str, Any]] = []
    for i, t in enumerate(tubes):
        theta = math.radians(t["angle_deg"])
        gx, gy = t["err"] * math.cos(theta), t["err"] * math.sin(theta)
        beta = math.atan2(gy, gx)
        sigma = 1.0
        if beta < 0.0:
            beta += math.pi
            sigma = -1.0
        gens.append({
            "tube_idx": i, "beta": beta,
            "gx": sigma * gx, "gy": sigma * gy,
        })
    gens.sort(key=lambda g: g["beta"])
    gidx_of_tube = {g["tube_idx"]: gi for gi, g in enumerate(gens)}
    ng = len(gens)

    signs = [-1] * ng
    px = -sum(g["gx"] for g in gens)
    py = -sum(g["gy"] for g in gens)
    base_x: list[float] = []
    base_y: list[float] = []
    sign_at: list[list[int]] = []

    def _snapshot() -> None:
        # zonotope 顶点 = 标称向量 nom + 误差偏移(与 worst_case_u1 的 evaluate 一致)
        base_x.append(nom_x + px)
        base_y.append(nom_y + py)
        sign_at.append(signs.copy())

    _snapshot()
    for gi in range(ng):
        px += 2.0 * gens[gi]["gx"]
        py += 2.0 * gens[gi]["gy"]
        signs[gi] = 1
        _snapshot()
    for gi in range(ng):
        px -= 2.0 * gens[gi]["gx"]
        py -= 2.0 * gens[gi]["gy"]
        signs[gi] = -1
        if gi < ng - 1:
            _snapshot()
    V = len(base_x)

    # 每个候选管在各顶点处需减去的修正向量(未乘 R)
    corr_x: list[list[float]] = []
    corr_y: list[list[float]] = []
    cand_ids: list[int] = []
    for ci in cand_idx:
        t = tubes[ci]
        c = (t["err"] - post_bound) / t["err"]
        gi = gidx_of_tube[ci]
        gx, gy = gens[gi]["gx"], gens[gi]["gy"]
        corr_x.append([c * sign_at[q][gi] * gx for q in range(V)])
        corr_y.append([c * sign_at[q][gi] * gy for q in range(V)])
        cand_ids.append(t["tube_id"])

    def _screen(id_combo: tuple[int, ...]) -> float:
        """复称组合(候选下标)对应的复称后最不利 |U₁|(g·mm)。

        顶点位置按单位质量复数和存储, 与限值比较前必须乘转子半径 R。
        """
        best2 = -1.0
        for q in range(V):
            x = base_x[q]
            y = base_y[q]
            for j in id_combo:
                x -= corr_x[j][q]
                y -= corr_y[j][q]
            d2 = x * x + y * y
            if d2 > best2:
                best2 = d2
        return math.sqrt(best2) * R

    # ---- 分层枚举: k=0..K, 先最少管数, 管数内最不利值最低, 平局管号序列升序 ----
    def _tolerance(mag: float) -> float:
        return _NUM_TOL * max(1.0, mag)

    def _authoritative(id_tuple: tuple[int, ...]) -> float:
        """对选定复称组合调用现有最不利一阶不平衡量计算(权威复算)。"""
        return worst_case_u1(
            placements=placements,
            radius_mm=radius,
            errors=_reduced_errors(set(id_tuple)),
        )["worst_case"]["u1_mag_g_mm"]

    winner: Optional[dict[str, Any]] = None
    best_effort: dict[str, Any] = {"k": 0, "mag": baseline_mag, "ids": ()}
    evaluated = 1  # k=0 基线组合
    per_k_best: list[dict[str, Any]] = []
    for k in range(0, K + 1):
        rows: list[tuple[float, tuple[int, ...]]] = []
        if k == 0:
            rows.append((baseline_mag, ()))
        else:
            for id_combo in combinations(range(m), k):
                id_tuple = tuple(cand_ids[j] for j in id_combo)
                rows.append((_screen(id_combo), id_tuple))
                evaluated += 1
        best_mag = min(mag for mag, _ in rows)
        tol = _tolerance(best_mag)
        # combinations 已按管号序列字典序生成, 第一条即平局裁决胜者
        best_ids = next(idt for mag, idt in rows if mag <= best_mag + tol)
        tie_count = sum(1 for mag, _ in rows if mag <= best_mag + tol)
        per_k_best.append({"k": k, "mag": best_mag, "ids": best_ids,
                           "count": tie_count})
        # 全局最优对照(无法达标时使用): 最不利值最低, 平局管数更少、序列更小
        be = best_effort
        if (best_mag < be["mag"] - _tolerance(be["mag"])
                or (abs(best_mag - be["mag"]) <= _tolerance(be["mag"])
                    and (k < be["k"] or (k == be["k"] and best_ids < be["ids"])))):
            best_effort = {"k": k, "mag": best_mag, "ids": best_ids}
        # 第一个存在达标的 k 即最少复称管数; 入选层用现有计算权威复算,
        # 平局条数也按权威值重数, 筛选器只用于加速、不影响精确性
        if best_mag <= limit + _tolerance(limit):
            auth_rows = [(baseline_mag, ())] if k == 0 else [
                (_authoritative(idt), idt) for _, idt in rows]
            auth_best = min(mag for mag, _ in auth_rows)
            auth_tol = _tolerance(auth_best)
            auth_ids = next(idt for mag, idt in auth_rows
                            if mag <= auth_best + auth_tol)
            auth_count = sum(1 for mag, _ in auth_rows
                             if mag <= auth_best + auth_tol)
            if auth_best <= limit + _tolerance(limit):
                winner = {"k": k, "mag": auth_best, "ids": auth_ids,
                          "count": auth_count}
                break

    def _tube_rows(reweighed: set[int]) -> list[dict[str, Any]]:
        rows = []
        for t in tubes:
            after_e = post_bound if t["tube_id"] in reweighed else t["err"]
            rows.append({
                "tube_id": t["tube_id"],
                "slot": t["slot"],
                "angle_deg": t["angle_deg"],
                "nominal_mass_g": t["mass"],
                "error_before_g": t["err"],
                "error_after_g": after_e,
                "error_reduction_g": t["err"] - after_e,
                "reweighed": t["tube_id"] in reweighed,
            })
        return rows

    candidate_rows = [
        {
            "tube_id": tubes[ci]["tube_id"],
            "slot": tubes[ci]["slot"],
            "angle_deg": tubes[ci]["angle_deg"],
            "nominal_mass_g": tubes[ci]["mass"],
            "current_error_bound_g": tubes[ci]["err"],
            "post_error_bound_g": post_bound,
        }
        for ci in cand_idx
    ]

    def _context() -> dict[str, Any]:
        return {
            "method": (
                "按复称管数分层的子集完全枚举(复称只把该管误差上界降至 "
                "min(e_k, e*), 标称质量与落槽不变): 对每个复称组合调用现有"
                "最不利一阶不平衡量计算(二维 zonotope 顶点枚举), "
                f"管数≤{MAX_TUBES}、复称上限 0~{MAX_REWEIGHS}; "
                "先找能达标的最少复称管数, 再在该管数内选最不利值最低的"
                "组合, 平局按管号序列升序裁决; 无法达标时给出次数内最优"
                "组合及超限量, 不误报达标"
            ),
            "plan": {
                "id": plan.get("id"),
                "name": plan.get("name"),
                "created_at": plan.get("created_at"),
            },
            "radius_mm": R,
            "tube_count": n,
            "unified_error_bound_g": post_bound,
            "u1_limit_g_mm": limit,
            "max_reweighs": M,
            "candidate_count": m,
            "reweigh_candidates": candidate_rows,
            "evaluated_combinations": evaluated,
            "layers": [],
            "feasible": False,
            "reweighs_required": None,
            "reweighed_tubes": [],
            "optimal_combination_count": None,
            "tie_broken": None,
            "before": before,
            "after": None,
            "tubes": _tube_rows(set()),
            "within_limit": None,
            "excess_g_mm": None,
            "margin_g_mm": None,
            "reason": None,
            "note": "复称规划为只读结果, 未修改任何已保存方案; 标称质量与落槽均不变。",
        }

    out = _context()

    def _apply_choice(choice: dict[str, Any]) -> dict[str, Any]:
        ids = set(choice["ids"])
        after = worst_case_u1(
            placements=placements,
            radius_mm=radius,
            errors=_reduced_errors(ids),
            u1_limit_g_mm=limit,
        )
        out["after"] = after
        out["tubes"] = _tube_rows(ids)
        out["reweighed_tubes"] = list(choice["ids"])
        return after

    # 各已枚举层(k=0..winner 或 0..K)的层内最优, 用现有计算权威复算
    layer_rows = []
    for layer in per_k_best:
        mag = worst_case_u1(
            placements=placements,
            radius_mm=radius,
            errors=_reduced_errors(set(layer["ids"])),
        )["worst_case"]["u1_mag_g_mm"]
        layer_rows.append({
            "reweigh_count": layer["k"],
            "reweighed_tubes": list(layer["ids"]),
            "worst_u1_g_mm": mag,
            "within_limit": mag <= limit + _tolerance(limit),
            "combination_count_at_best": layer["count"],
        })
    out["layers"] = layer_rows

    if winner is not None:
        after = _apply_choice(winner)
        final_mag = after["worst_case"]["u1_mag_g_mm"]
        out.update({
            "feasible": True,
            "reweighs_required": winner["k"],
            "optimal_combination_count": winner["count"],
            "tie_broken": winner["count"] > 1,
            "within_limit": True,
            "excess_g_mm": 0.0,
            "margin_g_mm": max(0.0, limit - final_mag),
        })
        if winner["k"] == 0:
            out["reason"] = (
                f"当前误差上界下最不利 |U₁|={baseline_mag:.6g} g·mm 已满足限值 "
                f"{limit:g} g·mm, 无需复称。"
            )
        else:
            ids_txt = "、".join(str(i) for i in winner["ids"])
            tie_txt = (
                f" 达到该最优结果的复称组合共 {winner['count']} 种, "
                "已按管号序列升序裁决。"
                if winner["count"] > 1 else " 最优复称组合唯一。"
            )
            out["reason"] = (
                f"最少复称 {winner['k']} 支试管(管号 {ids_txt})即可达标: "
                f"复称后最不利 |U₁| 由 {baseline_mag:.6g} g·mm 降至 "
                f"{final_mag:.6g} g·mm ≤ 限值 {limit:g} g·mm, 余量 "
                f"{out['margin_g_mm']:.6g} g·mm。{tie_txt}"
                + (" 候选管数少于复称上限, 已枚举全部可复称管。" if m < M else "")
            )
        return out

    # ---- 次数内均无法达标: 给出最不利值最低的组合与超限量, 不误报达标 ----
    after = _apply_choice(best_effort)
    final_mag = after["worst_case"]["u1_mag_g_mm"]
    out.update({
        "feasible": False,
        "reweighs_required": None,
        "optimal_combination_count": None,
        "tie_broken": None,
        "within_limit": False,
        "excess_g_mm": max(0.0, final_mag - limit),
        "margin_g_mm": 0.0,
    })
    ids = best_effort["ids"]
    if ids:
        ids_txt = "、".join(str(i) for i in ids)
        head = (
            f"在最多 {M} 次复称内共精确评估 {evaluated} 种复称组合, 均无法使"
            f"最不利 |U₁| ≤ 限值 {limit:g} g·mm; 其中复称后最不利值最低的是"
            f"复称 {len(ids)} 支(管号 {ids_txt}): 最不利 |U₁| 由 "
            f"{baseline_mag:.6g} g·mm 降至 {final_mag:.6g} g·mm, "
        )
    elif m == 0:
        head = (
            f"各管当前误差上界均不高于复称后统一上界 {post_bound:g} g, "
            f"没有复称后误差上界可下降的试管; 最不利 |U₁|={final_mag:.6g} g·mm, "
        )
    elif M == 0:
        head = (
            f"最多复称次数为 0, 不允许复称; 当前误差上界下"
            f"最不利 |U₁|={final_mag:.6g} g·mm, "
        )
    else:
        head = (
            f"在最多 {M} 次复称内共精确评估 {evaluated} 种复称组合, "
            f"即使复称全部 {m} 支候选管(复称次数上限 {M}), "
            f"最不利 |U₁| 仍为 {final_mag:.6g} g·mm, "
        )
    out["reason"] = (
        head + f"仍超出限值 {out['excess_g_mm']:.6g} g·mm, 判定超限、不判为达标。"
        + (" 候选管数少于复称上限, 已枚举全部可复称管。" if m < M else "")
    )
    return out
