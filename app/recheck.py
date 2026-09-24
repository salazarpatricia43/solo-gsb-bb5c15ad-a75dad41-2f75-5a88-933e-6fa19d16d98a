"""已保存配平方案的现场装管复核(只读, 不改变求解与保存格式)。

问题模型
--------
方案确定后: n 支试管各有标称质量 m_k 与方案槽位 s_k。现场装管后, 操作者
按**启用槽**录入实际有管槽位及天平实测质量 r_j(精度 0.01 g), 并给出一个
**统一允许偏差** e(g)。读数 j 可能对应试管 k 当且仅当

    |m_k - r_j| <= e

复核问题即: 只在**质量差不超过偏差**的边上, 求方案试管与实测读数之间的
**一对一匹配**(二分图完美匹配), 并以**总绝对质量差最小**为准。

精确求解(子集动态规划)
--------------------
读数最多 12 份, 故完整匹配时方案管数 n <= 12, 用子集 DP **精确**求最小
总绝对质量差(不做贪心、不取近似):

    f(i, S) = min_{j in S} f(i-1, S\\{j}) + |m_i - r_j|   前 i 支试管匹配读数集 S
    g(i, S) = min_{j in S} |m_i - r_j| + g(i+1, S\\{j})   第 i 支起匹配读数集 S

最小总绝对质量差 C* = f(n, 全集)。边 (k, j) 出现在**某个最优匹配**中,
当且仅当存在不含 j 的读数集 S 使

    f(k, S) + |m_k - r_j| + g(k+1, 全集 \\ S \\ {j}) = C*     (1e-9 相对容差)

其中 S 是前 k 支试管占用的读数集(不含 j)。据此对每个实测槽汇总**所有
最优匹配**可能对应的试管, 而不枚举全部最优匹配(退化情形下可达 n! 条;
条数用同一 DP 以任意精度整数计数):

- **候选唯一**: 该槽读数在全部最优匹配中都对应同一支试管 —— 按原方案
  槽位判定"正确"(读数槽即该管方案槽)或"错槽"(读数槽不是该管方案槽);
- **候选多解**: 标为"身份不确定" —— 不能任取一个最优匹配断言错放。

读数数量与方案管数不等、或不存在完整匹配时, 明确说明原因(并给出最多
可匹配对数, 以及在允许偏差内无任何兼容读数的试管/无任何兼容试管的
读数清单)。对称地, 也汇总每支方案试管在全部最优匹配中可能落入的实测
槽位, 供操作者交叉核对。

拒绝条件(ValueError → HTTP 400)
-------------------------------
- 实测读数少于 1 份或超过 12 份; 槽位重复、在方案中不存在或为禁用槽;
- 实测质量非正、非有限, 或不是 0.01 g 的整数倍(读数精度 0.01 g);
- 统一允许偏差为负或非有限;
- 方案没有已放置试管、缺少槽位状态; 摆放明细编号/槽位重复、质量非正。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# 读数份数上限: 子集 DP 状态数为 2^n, n <= 12 时 4096, 瞬时完成
MAX_READINGS = 12
# 天平读数精度(g): 实测质量必须是该步长的整数倍
MASS_STEP_G = 0.01
# 与 app/compare.py / app/loading.py 一致的数值容差
_NUM_TOL = 1e-9

_STATUS_LABELS = {
    "correct": "正确",
    "wrong_slot": "错槽",
    "uncertain": "身份不确定",
}


def recheck_tube_loading(plan: dict[str, Any],
                         readings: list[dict[str, Any]],
                         tolerance_g: float) -> dict[str, Any]:
    """对已保存方案做现场装管复核(只读)。

    参数
    ----
    plan: GET /api/plan/{id} 返回结构的方案(含 id/name/request/result),
        仅读取, 不修改。
    readings: 实测读数列表, 每项含 slot(实际有管的启用槽)与 mass(实测
        质量 g, 0.01 精度); 1~12 份, 槽位不得重复。
    tolerance_g: 统一允许偏差(g), 非负有限数; 只有 |标称-实测| <= 偏差
        的试管-读数对才允许匹配。

    返回
    ----
    含匹配可行性、最小总绝对质量差、最优匹配条数、逐实测槽候选试管与
    判定(正确/错槽/身份不确定)、逐方案试管可能槽位及原因说明的字典。
    """
    plan_id = plan.get("id")

    # ---- 统一允许偏差 ----
    tol = float(tolerance_g)
    if not math.isfinite(tol):
        raise ValueError("统一允许偏差必须为有限数值。")
    if tol < 0.0:
        raise ValueError("统一允许偏差不能为负。")

    # ---- 方案试管(标称质量与方案槽位) ----
    result = plan.get("result") or {}
    used = [p for p in (result.get("placements") or [])
            if p.get("slot") is not None]
    if not used:
        raise ValueError(
            f"方案 {plan_id} 没有已放置的试管, 无法进行装管复核。"
        )
    tubes: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_slots: set[int] = set()
    for p in used:
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
        tubes.append({"tube_id": tid, "plan_slot": slot, "mass": mass})
    # 管号升序: DP 位序与结果展示都以此为依据
    tubes.sort(key=lambda t: t["tube_id"])
    n = len(tubes)

    # ---- 槽位状态(读数槽必须是方案中的启用槽) ----
    slot_state = result.get("slot_state") or []
    if not slot_state:
        raise ValueError(
            f"方案 {plan_id} 缺少槽位状态信息, 无法进行装管复核。"
        )
    slot_info: dict[int, dict[str, Any]] = {}
    for item in slot_state:
        idx = int(item["slot"])
        slot_info[idx] = {
            "angle": float(item["angle"]),
            "enabled": bool(item.get("enabled", True)),
        }

    # ---- 实测读数 ----
    if not isinstance(readings, (list, tuple)) or not readings:
        raise ValueError("请至少录入 1 份实测读数。")
    if len(readings) > MAX_READINGS:
        raise ValueError(
            f"实测读数最多 {MAX_READINGS} 份(当前 {len(readings)} 份), 明确拒绝。"
        )
    meas: list[dict[str, Any]] = []
    seen_rslots: set[int] = set()
    for raw in readings:
        slot = int(raw["slot"])
        if slot in seen_rslots:
            raise ValueError(f"实测槽位 {slot} 重复录入。")
        seen_rslots.add(slot)
        info = slot_info.get(slot)
        if info is None:
            raise ValueError(f"实测槽位 {slot} 在方案中不存在。")
        if not info["enabled"]:
            raise ValueError(
                f"实测槽位 {slot} 在方案中为禁用槽, 不属于可装管的启用槽。"
            )
        mass = float(raw["mass"])
        if not math.isfinite(mass) or mass <= 0.0:
            raise ValueError(f"槽位 {slot} 的实测质量必须为正的有限数。")
        scaled = mass / MASS_STEP_G
        if abs(scaled - round(scaled)) > 1e-6:
            raise ValueError(
                f"槽位 {slot} 的实测质量 {mass:g} g 不符合 0.01 g 读数精度"
                "(实测质量须为 0.01 g 的整数倍)。"
            )
        meas.append({
            "slot": slot,
            "mass": round(scaled) * MASS_STEP_G,
            "angle": info["angle"],
        })
    # 槽号升序: 读数位序与展示顺序一致
    meas.sort(key=lambda r: r["slot"])
    m = len(meas)

    def _context() -> dict[str, Any]:
        return {
            "method": (
                "二分图一对一匹配(子集动态规划精确求最小总绝对质量差, "
                f"读数≤{MAX_READINGS} 份): 只在 |标称-实测| ≤ 统一允许偏差的"
                "边上匹配; 对每个实测槽汇总所有最优匹配可能对应的试管, "
                "唯一时按原方案槽位判定正确/错槽, 多解时标为身份不确定"
            ),
            "plan": {
                "id": plan.get("id"),
                "name": plan.get("name"),
                "created_at": plan.get("created_at"),
            },
            "tolerance_g": tol,
            "plan_tube_count": n,
            "reading_count": m,
            "complete": False,
            "reason": None,
            "total_abs_deviation_g": None,
            "optimal_matching_count": None,
            "max_matched_pairs": None,
            "tubes_without_candidate": [],
            "readings_without_candidate": [],
            "readings": [],
            "tubes": [],
            "summary": None,
            "note": "现场装管复核为只读结果, 未修改任何已保存方案。",
        }

    # ---- 读数数量与方案管数不等: 说明原因, 不做匹配 ----
    if m != n:
        out = _context()
        out["reason"] = (
            f"实测读数 {m} 份与方案试管数 {n} 支不等: 数量不一致时无法建立"
            "一一对应的完整匹配。请核对是否漏录、多录或装管数量本身有误, "
            "修正后重新复核。"
        )
        return out

    # ---- 匹配边: 仅质量差不超过统一允许偏差的试管-读数对 ----
    INF = math.inf
    edge_tol = _NUM_TOL * max(1.0, tol)
    cost = [[INF] * n for _ in range(n)]
    for k, t in enumerate(tubes):
        for j, r in enumerate(meas):
            d = abs(t["mass"] - r["mass"])
            if d <= tol + edge_tol:
                cost[k][j] = d

    size = 1 << n
    full = size - 1

    # f[i][mask]: 前 i 支试管(按管号升序)匹配读数集 mask 的最小总绝对质量差
    f = [[INF] * size for _ in range(n + 1)]
    f[0][0] = 0.0
    for i in range(1, n + 1):
        fi, fp, ci = f[i], f[i - 1], cost[i - 1]
        for mask in range(size):
            best = INF
            sub = mask
            while sub:
                lsb = sub & (-sub)
                prev = fp[mask ^ lsb]
                c = ci[lsb.bit_length() - 1]
                if prev < INF and c < INF:
                    v = prev + c
                    if v < best:
                        best = v
                sub ^= lsb
            fi[mask] = best

    # g[i][mask]: 第 i 支起的试管匹配读数集 mask 的最小总绝对质量差
    g = [[INF] * size for _ in range(n + 1)]
    g[n][0] = 0.0
    for i in range(n - 1, -1, -1):
        gi, gn, ci = g[i], g[i + 1], cost[i]
        for mask in range(size):
            best = INF
            sub = mask
            while sub:
                lsb = sub & (-sub)
                nxt = gn[mask ^ lsb]
                c = ci[lsb.bit_length() - 1]
                if nxt < INF and c < INF:
                    v = nxt + c
                    if v < best:
                        best = v
                sub ^= lsb
            gi[mask] = best

    # h[i][mask]: 前 i 支试管与读数集 mask 的子集最多可匹配对数(用于不可行说明)
    h = [[0] * size for _ in range(n + 1)]
    for i in range(1, n + 1):
        hi, hp, ci = h[i], h[i - 1], cost[i - 1]
        for mask in range(size):
            best = hp[mask]  # 试管 i-1 不参与匹配
            sub = mask
            while sub:
                lsb = sub & (-sub)
                if ci[lsb.bit_length() - 1] < INF:
                    v = hp[mask ^ lsb] + 1
                    if v > best:
                        best = v
                sub ^= lsb
            hi[mask] = best
    max_pairs = h[n][full]

    # ---- 不存在完整匹配: 说明原因 ----
    total = f[n][full]
    if total == INF:
        out = _context()
        out["max_matched_pairs"] = max_pairs
        no_cand_tubes = [
            t for k, t in enumerate(tubes)
            if all(cost[k][j] == INF for j in range(n))
        ]
        no_cand_readings = [
            r for j, r in enumerate(meas)
            if all(cost[k][j] == INF for k in range(n))
        ]
        out["tubes_without_candidate"] = [
            {"tube_id": t["tube_id"], "nominal_mass_g": t["mass"],
             "plan_slot": t["plan_slot"]}
            for t in no_cand_tubes
        ]
        out["readings_without_candidate"] = [
            {"slot": r["slot"], "measured_mass_g": r["mass"]}
            for r in no_cand_readings
        ]
        parts = [
            f"在统一允许偏差 ±{tol:g} g 内不存在完整的一对一匹配: "
            f"最多只能匹配 {max_pairs}/{n} 对。"
        ]
        if no_cand_tubes:
            txt = "、".join(
                f"试管 {t['tube_id']}(标称 {t['mass']:g} g)" for t in no_cand_tubes
            )
            parts.append(f"{txt} 在偏差内没有任何可对应的实测读数。")
        if no_cand_readings:
            txt = "、".join(
                f"槽 #{r['slot']}({r['mass']:g} g)" for r in no_cand_readings
            )
            parts.append(f"实测读数 {txt} 在偏差内没有任何可对应的方案试管。")
        parts.append(
            "可能原因: 实际装管数量或位置与方案不符、实测质量超出允许偏差, "
            "或统一允许偏差设置过小; 请核对现场装管与读数后重新复核。"
        )
        out["reason"] = "".join(parts)
        return out

    # ---- 最优匹配条数(任意精度整数, 不逐一枚举) ----
    cnt = [[0] * size for _ in range(n + 1)]
    cnt[0][0] = 1
    for i in range(1, n + 1):
        fi, fp, ci = f[i], f[i - 1], cost[i - 1]
        cnti, cntp = cnt[i], cnt[i - 1]
        for mask in range(size):
            target = fi[mask]
            if target == INF:
                continue
            tol_i = _NUM_TOL * max(1.0, target)
            total_i = 0
            sub = mask
            while sub:
                lsb = sub & (-sub)
                prev_mask = mask ^ lsb
                c = ci[lsb.bit_length() - 1]
                prev = fp[prev_mask]
                if prev < INF and c < INF and cntp[prev_mask]:
                    if abs(prev + c - target) <= tol_i:
                        total_i += cntp[prev_mask]
                sub ^= lsb
            cnti[mask] = total_i
    optimal_count = cnt[n][full]

    # ---- 逐边判定: (k, j) 是否出现在某个最优匹配中 ----
    tol_c = _NUM_TOL * max(1.0, total)
    cand_of_reading: list[list[int]] = [[] for _ in range(n)]  # j -> [k]
    cand_of_tube: list[list[int]] = [[] for _ in range(n)]     # k -> [j]
    for k in range(n):
        fk, gk1, ck = f[k], g[k + 1], cost[k]
        for j in range(n):
            c = ck[j]
            if c == INF:
                continue
            bit = 1 << j
            hit = False
            # 前 k 支试管占用读数集 S(不含 j), 试管 k 取 j, 其余试管取剩余
            for mask in range(size):
                if mask & bit:
                    continue
                a = fk[mask]
                if a == INF:
                    continue
                b = gk1[full ^ mask ^ bit]
                if b == INF:
                    continue
                if abs(a + c + b - total) <= tol_c:
                    hit = True
                    break
            if hit:
                cand_of_reading[j].append(k)
                cand_of_tube[k].append(j)

    # ---- 逐实测槽汇总与判定 ----
    reading_rows: list[dict[str, Any]] = []
    for j, r in enumerate(meas):
        cands = [{
            "tube_id": tubes[k]["tube_id"],
            "nominal_mass_g": tubes[k]["mass"],
            "plan_slot": tubes[k]["plan_slot"],
            "abs_deviation_g": cost[k][j],
        } for k in cand_of_reading[j]]
        if len(cands) == 1:
            k = cand_of_reading[j][0]
            t = tubes[k]
            matched: Optional[int] = t["tube_id"]
            if t["plan_slot"] == r["slot"]:
                status = "correct"
                detail = (
                    f"全部最优匹配中该槽读数唯一对应试管 {t['tube_id']}"
                    f"(标称 {t['mass']:g} g), 与其方案槽位一致。"
                )
            else:
                status = "wrong_slot"
                detail = (
                    f"全部最优匹配中该槽读数唯一对应试管 {t['tube_id']}"
                    f"(标称 {t['mass']:g} g), 但试管 {t['tube_id']} 的方案槽位"
                    f"为 #{t['plan_slot']}: 该管未按方案装管(错槽)。"
                )
        else:
            status = "uncertain"
            matched = None
            ids = "、".join(str(c["tube_id"]) for c in cands)
            detail = (
                f"最优匹配共 {optimal_count} 种, 该槽读数可能对应试管 {ids}: "
                "身份不确定, 不能任取一解断言错放。"
            )
        reading_rows.append({
            "slot": r["slot"],
            "angle_deg": r["angle"],
            "measured_mass_g": r["mass"],
            "candidates": cands,
            "candidate_count": len(cands),
            "status": status,
            "status_label": _STATUS_LABELS[status],
            "matched_tube_id": matched,
            "detail": detail,
        })

    # ---- 逐方案试管汇总(对称信息, 供交叉核对) ----
    tube_rows: list[dict[str, Any]] = []
    for k, t in enumerate(tubes):
        slots_j = sorted(meas[j]["slot"] for j in cand_of_tube[k])
        if slots_j == [t["plan_slot"]]:
            status = "correct"
            detail = "全部最优匹配中该管都位于其方案槽位。"
        elif t["plan_slot"] not in slots_j:
            status = "wrong_slot"
            txt = "、#".join(str(s) for s in slots_j)
            detail = (
                f"全部最优匹配中该管均不在方案槽位 #{t['plan_slot']}"
                f"(可能位于实测槽 #{txt}): 未按方案装管。"
            )
        else:
            status = "uncertain"
            txt = "、#".join(str(s) for s in slots_j)
            detail = (
                f"最优匹配不唯一, 该管可能位于实测槽 #{txt}"
                "(含其方案槽位): 不能断言错放。"
            )
        tube_rows.append({
            "tube_id": t["tube_id"],
            "nominal_mass_g": t["mass"],
            "plan_slot": t["plan_slot"],
            "candidate_slots": slots_j,
            "status": status,
            "status_label": _STATUS_LABELS[status],
            "detail": detail,
        })

    n_correct = sum(1 for r in reading_rows if r["status"] == "correct")
    n_wrong = sum(1 for r in reading_rows if r["status"] == "wrong_slot")
    n_uncertain = n - n_correct - n_wrong

    out = _context()
    out.update({
        "complete": True,
        "total_abs_deviation_g": total,
        "optimal_matching_count": optimal_count,
        "max_matched_pairs": n,
        "readings": reading_rows,
        "tubes": tube_rows,
        "summary": {
            "correct": n_correct,
            "wrong_slot": n_wrong,
            "uncertain": n_uncertain,
        },
    })
    match_txt = (
        "最优匹配唯一" if optimal_count == 1
        else f"最优匹配共 {optimal_count} 种"
    )
    if n_wrong == 0 and n_uncertain == 0:
        out["reason"] = (
            f"{n} 个实测槽全部唯一对应且与方案槽位一致: 装管全部正确"
            f"(最小总绝对质量差 {total:g} g, {match_txt})。"
        )
    elif n_wrong > 0:
        slots = "、#".join(
            str(r["slot"]) for r in reading_rows if r["status"] == "wrong_slot"
        )
        unc_txt = (
            f"; 另有 {n_uncertain} 个实测槽身份不确定, 不能断言错放"
            if n_uncertain else ""
        )
        out["reason"] = (
            f"发现 {n_wrong} 处错槽: 实测槽 #{slots} 唯一对应的试管与其方案"
            f"槽位不符{unc_txt}(最小总绝对质量差 {total:g} g, {match_txt})。"
        )
    else:
        slots = "、#".join(
            str(r["slot"]) for r in reading_rows if r["status"] == "uncertain"
        )
        out["reason"] = (
            f"{n_uncertain} 个实测槽(#{slots})身份不确定: 最优匹配不唯一, "
            "这些槽的读数在偏差内可对应多支试管, 不能任取一解断言错放"
            f"(最小总绝对质量差 {total:g} g, {match_txt})。"
        )
    return out
