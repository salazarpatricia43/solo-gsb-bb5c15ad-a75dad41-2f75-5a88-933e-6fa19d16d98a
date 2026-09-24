"""已保存配平方案的失效槽位应急迁移(只读, 不改变求解与保存格式)。

问题模型
--------
方案已确定: n 支试管各自的原槽位 θ_k 与标称质量 m_k 固定。运行现场出现
若干**新失效槽位**(原本启用、现在不得再占用), 需要在**不改动试管质量**的
前提下, 把少量非锁定试管迁移到其余有效槽位:

- **锁定试管不得移动**; 其原槽若成为失效槽, 则不存在可行摆放;
- **失效槽位不得占用**: 最终摆放中任何试管都不能落在失效槽(原本禁用的
  槽位同样不可用);
- 最多移动 ``max_moves`` 支非锁定试管(0~3); 一支试管换一个槽计 1 次移动,
  被移动试管允许落到另一支被移动试管空出的原槽(即允许成对交换/链式迁移);
- 迁移后的一阶不平衡量

      U₁ = R · Σ_k m_k · e^{iθ_k(final)}             (g·mm)

  必须不超过给定限值 L(必填, 非负)。

精确搜索(按移动数分层的完全枚举)
--------------------------------
为控制搜索规模, 仅接受 n ≤ 10 支试管、启用槽位数 ≤ 16、0 ≤ max_moves ≤ 3
的方案。设失效槽上的非锁定试管有 f 支(它们**必须**迁走), 有效空槽(启用、
非失效、且不被未移动试管占用)有 q 个。若选定 k 支试管移动(f ≤ k ≤ M),
则可注入的目标槽池共 q + k 个(q 个空槽 + k 个被移动试管腾出的原槽):

  子集选择  ≤ C(10, k) ≤ 120 种;
  注入方式  ≤ P(q+k, k) = (q+k)!/(q!) 种, k ≤ 3 时至多 9·8·7 = 504 种;
  叶子总数  ≤ ~4.4 万(k=0..3 合计), 瞬时完成。

服务端对每个 k(从 f 起, 到 M 止)枚举全部子集 × 全部合法目标槽注入
(回溯剪枝: 目标槽唯一、不得等于该管原槽), **精确**寻找使 |U₁| ≤ L 的
摆放, 找到的第一个可行 k 即最少移动管数。

目标排序(字典序, 确定性)
------------------------
在达到最少移动数的摆放中:

1. 最终 |U₁| 最小;
2. 仍并列时, 取**按管号升序的目标槽位序列**
   (final_slot(tube_id 最小), final_slot(tube_id 次小), …)字典序最小者;
   未移动试管的目标槽即原槽, 一并参与该序列比较。

同时统计达到该最优结果的摆放条数。任何 k 都找不到满足限值的摆放时
明确说明无可行摆放, 并给出移动数不超过 M 的范围内 |U₁| 最小的对照摆放
(仅供参考, 不落库、不覆盖原方案)。

拒绝条件(ValueError → HTTP 400)
-------------------------------
- 限值不是有限非负数, 或 max_moves 不在 0~3;
- 失效槽位列表为空、含不存在槽位、含原本禁用槽位或重复槽位;
- 方案没有已放置试管、已放置试管超过 10 支或启用槽位超过 16 个;
- 摆放明细中试管编号/槽位重复、质量非正、缺少槽位角度; 半径非法。
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Optional

# 搜索规模上限: 试管 10 支、启用槽 16 个、移动上限 3
MAX_TUBES = 10
MAX_ENABLED_SLOTS = 16
MAX_MOVES = 3
# 与 app/compare.py / app/loading.py 一致的数值容差
_NUM_TOL = 1e-9


def _vec_block(x: float, y: float) -> dict[str, Any]:
    return {
        "u1_x_g_mm": x,
        "u1_y_g_mm": y,
        "u1_mag_g_mm": math.hypot(x, y),
        "u1_angle_deg": math.degrees(math.atan2(y, x)) % 360.0,
    }


def plan_slot_evacuation(plan: dict[str, Any],
                         failed_slots: list[int],
                         u1_limit_g_mm: float,
                         max_moves: int) -> dict[str, Any]:
    """对已保存方案精确规划失效槽位应急迁移(只读)。

    参数
    ----
    plan: GET /api/plan/{id} 返回结构的方案(含 id/name/request/result),
        仅读取, 不修改。
    failed_slots: 新失效槽位索引列表(原本必须启用), 至少 1 个、不重复。
    u1_limit_g_mm: 一阶不平衡量限值(g·mm), 必填的有限非负数。
    max_moves: 最多可移动管数, 0~3。

    返回
    ----
    含逐管原槽/目标槽、移动数、原/最终 |U₁|、最优摆放条数与限值判定的
    字典; ``feasible=False`` 时给出明确原因(并尽量附最优对照摆放)。
    """
    plan_id = plan.get("id")

    # ---- 限值 / 移动上限 ----
    limit = float(u1_limit_g_mm)
    if not math.isfinite(limit):
        raise ValueError("一阶不平衡量限值必须为有限数值。")
    if limit < 0.0:
        raise ValueError("一阶不平衡量限值不能为负。")
    M = int(max_moves)
    if not (0 <= M <= MAX_MOVES):
        raise ValueError(f"最多可移动管数必须在 0~{MAX_MOVES} 之间(当前 {M})。")

    # ---- 半径 ----
    result = plan.get("result") or {}
    cfg = result.get("config") or {}
    radius = cfg.get("radius_mm")
    if radius is None:
        radius = (plan.get("request") or {}).get("radius_mm", 100.0)
    radius = float(radius)
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError("方案中的转子半径必须为正数。")

    # ---- 失效槽位入参 ----
    if not isinstance(failed_slots, (list, tuple)) or not failed_slots:
        raise ValueError("请至少填写一个新失效槽位。")
    failed: list[int] = []
    for raw in failed_slots:
        s = int(raw)
        if s in failed:
            raise ValueError(f"失效槽位 {s} 重复填写。")
        failed.append(s)
    failed_set = set(failed)

    # ---- 槽位几何(以方案结果 slot_state 为准) ----
    slot_state = result.get("slot_state") or []
    if not slot_state:
        raise ValueError(
            f"方案 {plan_id} 缺少槽位状态信息, 无法进行失效槽位应急迁移。"
        )
    slot_info: dict[int, dict[str, Any]] = {}
    for item in slot_state:
        idx = int(item["slot"])
        ang = float(item["angle"])
        if not math.isfinite(ang):
            raise ValueError(f"槽位 {idx} 的角度必须为有限数值。")
        slot_info[idx] = {"angle": ang, "enabled": bool(item.get("enabled", True))}
    for s in failed:
        info = slot_info.get(s)
        if info is None:
            raise ValueError(f"失效槽位 {s} 在方案中不存在。")
        if not info["enabled"]:
            raise ValueError(
                f"槽位 {s} 在方案中本就处于禁用状态, 不属于新失效槽位"
                "(应急迁移只接受原本启用的槽位)。"
            )

    # ---- 摆放明细 ----
    used = [p for p in (result.get("placements") or [])
            if p.get("slot") is not None]
    if not used:
        raise ValueError(
            f"方案 {plan_id} 没有已放置的试管, 无法进行失效槽位应急迁移。"
        )
    if len(used) > MAX_TUBES:
        raise ValueError(
            f"方案 {plan_id} 有 {len(used)} 支已放置试管, 超过失效槽位应急迁移"
            f"上限 {MAX_TUBES} 支(精确枚举搜索规模随管数组合增长), 明确拒绝。"
        )
    enabled_count = sum(1 for info in slot_info.values() if info["enabled"])
    if enabled_count > MAX_ENABLED_SLOTS:
        raise ValueError(
            f"方案有 {enabled_count} 个启用槽位, 超过失效槽位应急迁移上限 "
            f"{MAX_ENABLED_SLOTS} 个, 明确拒绝。"
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
        angle = p.get("angle")
        if angle is None or not math.isfinite(float(angle)):
            raise ValueError(f"试管 {tid} 的摆放结果缺少有效槽位角度。")
        tubes.append({
            "tube_id": tid,
            "origin": slot,
            "angle_deg": float(angle),
            "mass": mass,
            "locked": bool(p.get("locked", False)),
        })
    # 管号升序: 并列裁决的目标槽位序列即按此顺序
    tubes.sort(key=lambda t: t["tube_id"])
    n = len(tubes)

    def _vector(j: int, slot: int) -> complex:
        """试管 j 放到 slot 时的质径积向量 R·m·e^{iθ}(g·mm)。"""
        a = math.radians(slot_info[slot]["angle"])
        m = tubes[j]["mass"] * radius
        return complex(m * math.cos(a), m * math.sin(a))

    # 各管在原槽的向量; 总向量(迁移前)
    origin_vec = [_vector(j, tubes[j]["origin"]) for j in range(n)]
    total_vec = sum(origin_vec, 0j)
    original_block = _vec_block(total_vec.real, total_vec.imag)

    # 失效槽占用情况
    locked_on_failed = [
        {"tube_id": tubes[j]["tube_id"], "slot": tubes[j]["origin"],
         "angle_deg": tubes[j]["angle_deg"], "mass_g": tubes[j]["mass"]}
        for j in range(n)
        if tubes[j]["origin"] in failed_set and tubes[j]["locked"]
    ]
    movable = [j for j in range(n) if not tubes[j]["locked"]]
    forced = [j for j in movable if tubes[j]["origin"] in failed_set]
    forced_set = set(forced)

    failed_rows = []
    for s in failed:
        occ = next((j for j in range(n) if tubes[j]["origin"] == s), None)
        failed_rows.append({
            "slot": s,
            "angle_deg": slot_info[s]["angle"],
            "occupied_by_tube": (tubes[occ]["tube_id"] if occ is not None else None),
            "locked": (tubes[occ]["locked"] if occ is not None else False),
        })

    def _tube_rows(targets: list[Optional[int]]) -> list[dict[str, Any]]:
        rows = []
        for j, t in enumerate(tubes):
            target = targets[j]
            moved = target is not None and target != t["origin"]
            rows.append({
                "tube_id": t["tube_id"],
                "mass_g": t["mass"],
                "locked": t["locked"],
                "original_slot": t["origin"],
                "original_angle_deg": t["angle_deg"],
                "final_slot": target,
                "final_angle_deg": (
                    slot_info[target]["angle"] if target is not None else None
                ),
                "moved": moved,
            })
        return rows

    def _context() -> dict[str, Any]:
        return {
            "method": (
                "按移动管数分层的完全枚举(子集选择 × 目标槽注入, "
                f"管数≤{MAX_TUBES}、启用槽≤{MAX_ENABLED_SLOTS}、"
                f"移动上限 0~{MAX_MOVES}): 先使移动管数最少, "
                "再使最终 |U₁| 最小; 并列时按管号升序的目标槽位序列裁决"
            ),
            "feasible": False,
            "plan": {
                "id": plan.get("id"),
                "name": plan.get("name"),
                "created_at": plan.get("created_at"),
            },
            "radius_mm": radius,
            "tube_count": n,
            "enabled_slot_count": enabled_count,
            "failed_slots": failed_rows,
            "u1_limit_g_mm": limit,
            "max_moves": M,
            "forced_move_count": len(forced),
            "locked_on_failed_slots": locked_on_failed,
            "original_u1": original_block,
            "moves_required": None,
            "optimal_arrangement_count": None,
            "tie_broken": None,
            "evaluated_arrangements": 0,
            "usable_target_slots": [],
            "within_limit": None,
            "excess_g_mm": None,
            "margin_g_mm": None,
            "final_u1": None,
            "tubes": [],
            "moves": [],
            "best_effort": None,
            "note": "失效槽位应急迁移为只读规划结果, 未修改任何已保存方案。",
        }

    # ---- 结构性不可行: 锁定试管压在失效槽上 ----
    if locked_on_failed:
        out = _context()
        ids = ", ".join(str(r["tube_id"]) for r in locked_on_failed)
        slots = ", ".join(str(r["slot"]) for r in locked_on_failed)
        out["reason"] = (
            f"失效槽位 {slots} 被锁定试管 {ids} 占用: 锁定试管不得移动, "
            "而失效槽位不得占用, 因此不存在可行摆放。"
        )
        out["tubes"] = _tube_rows([None] * n)
        return out

    # ---- 必须迁移的试管已超过移动上限 ----
    if len(forced) > M:
        out = _context()
        out["reason"] = (
            f"失效槽位上有 {len(forced)} 支非锁定试管必须迁移"
            f"({', '.join(str(tubes[j]['tube_id']) for j in forced)}), "
            f"超过最多可移动管数 {M}, 不存在满足移动上限的摆放。"
        )
        out["tubes"] = _tube_rows([None] * n)
        return out

    # ---- 有效空槽: 启用、非失效、且未被任何试管原槽占用 ----
    occupied_origins = {tubes[j]["origin"] for j in range(n)}
    empty_slots = sorted(
        s for s, info in slot_info.items()
        if info["enabled"] and s not in failed_set and s not in occupied_origins
    )
    # 非锁定试管原槽中不在失效槽上的: 可在被腾出后作为迁移目标
    movable_origin_slots = sorted(
        tubes[j]["origin"] for j in movable if tubes[j]["origin"] not in failed_set
    )
    usable_target_slots = sorted(set(empty_slots) | set(movable_origin_slots))

    def _enumerate_k(k: int) -> list[tuple[float, tuple[int, ...]]]:
        """枚举恰好移动 k 支试管的全部合法摆放, 返回 (|U₁|², 目标槽序列)。"""
        optional = [j for j in movable if j not in forced]
        leaves: list[tuple[float, tuple[int, ...]]] = []
        if k < len(forced) or k - len(forced) > len(optional):
            return leaves
        for extra in combinations(optional, k - len(forced)):
            movers = sorted(forced) + list(extra)
            movers_set = set(movers)
            # 固定不动试管(含全部锁定管)的向量和
            base = sum(
                (origin_vec[j] for j in range(n) if j not in movers_set), 0j
            )
            pool = sorted(
                set(empty_slots)
                | {tubes[j]["origin"] for j in movers if j not in forced_set}
            )
            used_pool = [False] * len(pool)
            assign: dict[int, int] = {}

            def key_of() -> tuple[int, ...]:
                return tuple(
                    assign[j] if j in movers_set else tubes[j]["origin"]
                    for j in range(n)
                )

            def rec(pos: int, u: complex) -> None:
                if pos == k:
                    leaves.append((u.real * u.real + u.imag * u.imag, key_of()))
                    return
                j = movers[pos]
                origin = tubes[j]["origin"]
                for pi, slot in enumerate(pool):
                    if used_pool[pi] or slot == origin:
                        continue
                    used_pool[pi] = True
                    assign[j] = slot
                    rec(pos + 1, u + _vector(j, slot))
                    used_pool[pi] = False
                assign.pop(j, None)

            rec(0, base)
        return leaves

    # ---- 按移动数从少到多精确搜索 ----
    def _best_of(mag_keys: list[tuple[float, tuple[int, ...]]],
                 ) -> tuple[float, tuple[int, ...]]:
        """(mag, key) 列表中的最优者: 模长最小; 容差内并列按目标槽序列升序。"""
        best_mag = min(m for m, _ in mag_keys)
        tol = _NUM_TOL * max(1.0, best_mag)
        tied = [(m, k) for m, k in mag_keys if m <= best_mag + tol]
        key = min(k for _, k in tied)
        mag = next(m for m, k in tied if k == key)
        return mag, key

    limit_tol = _NUM_TOL * max(1.0, limit)
    winner: Optional[dict[str, Any]] = None       # 首个可行 k 上的最优摆放
    best_effort: Optional[dict[str, Any]] = None  # 0..M 内 |U₁| 最小的摆放
    evaluated = 0
    for k in range(len(forced), M + 1):
        leaves = _enumerate_k(k)
        evaluated += len(leaves)
        if not leaves:
            continue
        # 该移动数下的全局最优(用于无可行摆放时的对照)
        mags = [(math.sqrt(m2), key) for m2, key in leaves]
        k_best_mag, k_best_key = _best_of(mags)
        if best_effort is None or k_best_mag < best_effort["mag"] - _NUM_TOL:
            best_effort = {"k": k, "mag": k_best_mag, "key": k_best_key}
        # 满足限值的摆放(第一个可行 k 即最少移动数)
        feasible_leaves = [(m, key) for m, key in mags if m <= limit + limit_tol]
        if feasible_leaves:
            best_mag = min(m for m, _ in feasible_leaves)
            mag_tol = _NUM_TOL * max(1.0, best_mag)
            optimal = [
                (m, key) for m, key in feasible_leaves
                if m <= best_mag + mag_tol
            ]
            opt_key = min(key for _, key in optimal)
            opt_mag = next(m for m, key in optimal if key == opt_key)
            winner = {
                "k": k,
                "mag": opt_mag,
                "key": opt_key,
                "count": len(optimal),
            }
            break

    out = _context()
    out["evaluated_arrangements"] = evaluated
    out["usable_target_slots"] = usable_target_slots

    def _arrangement_payload(k: int, key: tuple[int, ...],
                             count: Optional[int]) -> dict[str, Any]:
        rows = _tube_rows(list(key))
        moves = [
            {
                "tube_id": r["tube_id"],
                "mass_g": r["mass_g"],
                "from_slot": r["original_slot"],
                "from_angle_deg": r["original_angle_deg"],
                "to_slot": r["final_slot"],
                "to_angle_deg": r["final_angle_deg"],
            }
            for r in rows if r["moved"]
        ]
        moves.sort(key=lambda r: r["tube_id"])
        # 由目标槽序列独立复算最终 U₁(避免依赖搜索时的浮点累加)
        ux = uy = 0.0
        for j, slot in enumerate(key):
            v = _vector(j, slot)
            ux += v.real
            uy += v.imag
        return {
            "moves_required": k,
            "optimal_arrangement_count": count,
            "tie_broken": (count > 1) if count is not None else None,
            "final_u1": _vec_block(ux, uy),
            "tubes": rows,
            "moves": moves,
        }

    if winner is not None:
        payload = _arrangement_payload(winner["k"], winner["key"], winner["count"])
        out.update(payload)
        out["feasible"] = True
        out["within_limit"] = True
        final_mag = payload["final_u1"]["u1_mag_g_mm"]
        out["excess_g_mm"] = 0.0
        out["margin_g_mm"] = max(0.0, limit - final_mag)
        out["reason"] = (
            f"移动 {winner['k']} 支试管(已达最少移动数)即可满足限值: "
            f"最终 |U₁|={final_mag:.6g} g·mm ≤ {limit:g} g·mm; "
            f"迁移前 |U₁|={original_block['u1_mag_g_mm']:.6g} g·mm。"
        )
        return out

    # ---- 无可行摆放 ----
    if best_effort is None:
        # 移动上限内连一种合法摆放都不存在(例如必须迁移的试管没有可注入的
        # 有效目标槽), 与结构性不可行一样只给出原摆放明细
        out["tubes"] = _tube_rows([None] * n)
        out["reason"] = (
            f"在移动上限 {M} 支以内不存在任何合法摆放"
            "(必须迁移的试管没有可注入的有效目标槽: 有效空槽不足), "
            "无可行摆放。"
        )
        return out

    payload = _arrangement_payload(best_effort["k"], best_effort["key"], None)
    be_mag = payload["final_u1"]["u1_mag_g_mm"]
    out["best_effort"] = {
        "description": (
            f"在最多移动 {M} 支试管的范围内使最终 |U₁| 最小的摆放"
            f"(移动 {best_effort['k']} 支), 仍不满足限值, 仅供参考"
        ),
        "moves_used": payload["moves_required"],
        "final_u1": payload["final_u1"],
        "tubes": payload["tubes"],
        "moves": payload["moves"],
    }
    # 顶层同样给出该对照摆放, 便于界面直接渲染
    out["final_u1"] = payload["final_u1"]
    out["tubes"] = payload["tubes"]
    out["moves"] = payload["moves"]
    out["within_limit"] = False
    out["excess_g_mm"] = max(0.0, be_mag - limit)
    out["margin_g_mm"] = 0.0
    out["reason"] = (
        f"在移动上限 {M} 支以内, 共精确评估 {evaluated} 种合法摆放, "
        f"没有使最终 |U₁| ≤ 限值 {limit:g} g·mm 的摆放; "
        f"范围内最优摆放需移动 {best_effort['k']} 支, "
        f"最终 |U₁|={be_mag:.6g} g·mm(超出 {out['excess_g_mm']:.6g} g·mm)。"
    )
    return out
