"""两个已保存配平方案的比较逻辑(只读, 不改变求解与保存格式)。

比较内容:
- 按试管编号合并两方案的摆放明细, 标注 新增 / 移除 / 换槽 / 未变;
- 并列展示两方案的一阶、二阶不平衡量及差值;
- 比较转子几何(槽位/半径)与求解输入(转速/权重/搜索参数/试管清单),
  当条件不一致时输出参数差异, 提示指标差值并非同条件优化对比。
"""
from __future__ import annotations

from typing import Any, Optional

# 并列展示的指标: key -> (中文名, 单位, 小数位)
METRIC_FIELDS: list[tuple[str, str, str, int]] = [
    ("u1_mag_g_mm", "一阶不平衡量 |U₁|", "g·mm", 2),
    ("u1_angle_deg", "U₁ 方向", "°", 1),
    ("u2_mag_g_mm", "二阶不平衡量 |U₂|", "g·mm", 2),
    ("u2_angle_deg", "U₂ 方向", "°", 1),
    ("centrifugal_force_n", "离心合力 F", "N", 3),
    ("resultant_moment_n_m", "合力矩 M", "N·m", 4),
    ("equivalent_eccentricity_um", "等效偏心距", "μm", 2),
    ("total_mass_g", "试管总质量", "g", 1),
]

# 求解输入参数: key -> (中文名, 单位, 小数位)
SOLVER_FIELDS: list[tuple[str, str, str, int]] = [
    ("rpm", "转速", "rpm", 0),
    ("w1", "一阶权重 w₁", "", 6),
    ("w2", "二阶权重 w₂", "", 6),
    ("move_weight", "移动罚权", "", 6),
    ("restarts", "随机起点数", "", 0),
    ("chain_steps", "退火链长度", "步", 0),
]

_ANGLE_KEYS = {"u1_angle_deg", "u2_angle_deg"}
_NUM_TOL = 1e-9


def _signed_angle_diff(a: float, b: float) -> float:
    """角度差 a-b 归一化到 [-180, 180]。"""
    return (a - b + 180.0) % 360.0 - 180.0


def _same_num(a: Any, b: Any, tol: float = _NUM_TOL) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


# ---------------------------------------------------------------------------
# 摆放明细合并
# ---------------------------------------------------------------------------
def merge_placements(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """按试管编号合并两方案摆放, 状态: added / removed / moved / unchanged。"""
    def index_plan(plan: dict[str, Any]):
        return {
            int(p["tube_id"]): {
                "tube_id": int(p["tube_id"]),
                "mass": p.get("mass"),
                "locked": bool(p.get("locked", False)),
                "slot": (p["slot"] if p.get("slot") is not None else None),
                "angle": p.get("angle"),
            }
            for p in plan["result"].get("placements", [])
        }

    pa, pb = index_plan(a), index_plan(b)
    all_ids = sorted(set(pa) | set(pb))
    rows: list[dict[str, Any]] = []
    for tid in all_ids:
        xa, xb = pa.get(tid), pb.get(tid)
        if xa is None and xb is not None:
            status = "added"
        elif xa is not None and xb is None:
            status = "removed"
        elif xa["slot"] != xb["slot"]:
            status = "moved"
        else:
            status = "unchanged"
        rows.append({
            "tube_id": tid,
            "status": status,
            "mass_a": xa["mass"] if xa else None,
            "mass_b": xb["mass"] if xb else None,
            "locked_a": xa["locked"] if xa else False,
            "locked_b": xb["locked"] if xb else False,
            "slot_a": xa["slot"] if xa else None,
            "slot_b": xb["slot"] if xb else None,
            "angle_a": xa["angle"] if xa else None,
            "angle_b": xb["angle"] if xb else None,
        })
    return rows


# ---------------------------------------------------------------------------
# 指标并列与差值
# ---------------------------------------------------------------------------
def compare_metrics(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    ma, mb = a["result"]["metrics"], b["result"]["metrics"]
    out: list[dict[str, Any]] = []
    for key, label, unit, ndigits in METRIC_FIELDS:
        va, vb = ma.get(key), mb.get(key)
        if va is None or vb is None:
            delta = None
        elif key in _ANGLE_KEYS:
            delta = _signed_angle_diff(float(vb), float(va))
        else:
            delta = float(vb) - float(va)
        out.append({
            "key": key,
            "label": label,
            "unit": unit,
            "ndigits": ndigits,
            "is_angle": key in _ANGLE_KEYS,
            "value_a": va,
            "value_b": vb,
            "delta_b_minus_a": delta,
        })
    return out


# ---------------------------------------------------------------------------
# 输入(转子/求解)参数差异
# ---------------------------------------------------------------------------
def _slot_signature(req: dict[str, Any]) -> dict[int, dict[str, Any]]:
    slots = req.get("slots") or []
    return {
        int(s["index"]): {
            "angle": float(s.get("angle", 360.0 * int(s["index"]) / int(req.get("slot_count", 1)))),
            "enabled": bool(s.get("enabled", True)),
        }
        for s in slots
    }


def _tube_signature(req: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        int(t["id"]): {
            "mass": float(t["mass"]),
            "locked": bool(t.get("locked", False)),
            "slot": (int(t["slot"]) if t.get("slot") is not None else None),
        }
        for t in req.get("tubes", [])
    }


def _mutex_signature(req: dict[str, Any]) -> list[tuple[int, int]]:
    """互斥对规范化(无序去重、按小编号优先排序), 旧方案缺省视为空。"""
    pairs: set[tuple[int, int]] = set()
    for item in req.get("mutex_pairs") or []:
        if isinstance(item, dict):
            a = item.get("tube_a", item.get("a"))
            b = item.get("tube_b", item.get("b"))
        else:
            a, b = item[0], item[1]
        a, b = int(a), int(b)
        pairs.add((a, b) if a < b else (b, a))
    return sorted(pairs)


def _diff_entry(label: str, unit: str, ndigits: int,
                va: Any, vb: Any) -> dict[str, Any]:
    return {
        "label": label,
        "unit": unit,
        "ndigits": ndigits,
        "value_a": va,
        "value_b": vb,
    }


def compare_inputs(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """转子几何与求解输入差异清单。增量重排专用字段(previous/parent_id)不参与比较。"""
    qa, qb = a["request"], b["request"]
    rotor: list[dict[str, Any]] = []

    # 槽位数
    na, nb = int(qa.get("slot_count", len(qa.get("slots", [])))), \
             int(qb.get("slot_count", len(qb.get("slots", []))))
    if na != nb:
        rotor.append(_diff_entry("槽位数", "", 0, na, nb))

    # 逐槽角度/启用状态
    sa, sb = _slot_signature(qa), _slot_signature(qb)
    for idx in sorted(set(sa) | set(sb)):
        xa, xb = sa.get(idx), sb.get(idx)
        if xa is None:
            rotor.append(_diff_entry(f"槽 {idx}", "", 0, None,
                                     f"{xb['angle']:g}°({'启用' if xb['enabled'] else '禁用'})"))
            continue
        if xb is None:
            rotor.append(_diff_entry(f"槽 {idx}", "", 0,
                                     f"{xa['angle']:g}°({'启用' if xa['enabled'] else '禁用'})", None))
            continue
        changes = []
        if not _same_num(xa["angle"], xb["angle"]):
            changes.append(f"角度 {xa['angle']:g}°→{xb['angle']:g}°")
        if xa["enabled"] != xb["enabled"]:
            changes.append(f"{'禁用' if xa['enabled'] else '启用'}"
                           f"→{'启用' if xb['enabled'] else '禁用'}")
        if changes:
            rotor.append(_diff_entry(
                f"槽 {idx}", "", 1,
                f"{xa['angle']:g}°/{'启用' if xa['enabled'] else '禁用'}",
                f"{xb['angle']:g}°/{'启用' if xb['enabled'] else '禁用'}"))

    # 转子半径
    ra, rb = float(qa.get("radius_mm", 100.0)), float(qb.get("radius_mm", 100.0))
    if not _same_num(ra, rb):
        rotor.append(_diff_entry("转子半径 R", "mm", 1, ra, rb))

    # 求解输入
    solver: list[dict[str, Any]] = []
    for key, label, unit, ndigits in SOLVER_FIELDS:
        va, vb = qa.get(key), qb.get(key)
        if va is None and vb is None:
            continue
        if va is None or vb is None or not _same_num(va, vb):
            solver.append(_diff_entry(label, unit, ndigits, va, vb))

    # 互斥试管隔离约束(影响可行域与最优性, 必须纳入同条件判定)
    ma, mb = _mutex_signature(qa), _mutex_signature(qb)
    if ma != mb:
        solver.append({
            "label": "互斥试管对",
            "unit": "",
            "ndigits": 0,
            "value_a": ", ".join(f"{a}↔{b}" for a, b in ma) or "无",
            "value_b": ", ".join(f"{a}↔{b}" for a, b in mb) or "无",
        })

    # 试管清单差异(新增/移除/质量或锁定变化; 摆放槽位差异另由明细表体现)
    tubes: list[dict[str, Any]] = []
    ta, tb = _tube_signature(qa), _tube_signature(qb)
    for tid in sorted(set(ta) | set(tb)):
        xa, xb = ta.get(tid), tb.get(tid)
        if xa is None:
            tubes.append(_diff_entry(
                f"试管 {tid}", "", 1, None,
                f"{xb['mass']:g}g{'(锁定)' if xb['locked'] else ''}"))
            continue
        if xb is None:
            tubes.append(_diff_entry(
                f"试管 {tid}", "", 1,
                f"{xa['mass']:g}g{'(锁定)' if xa['locked'] else ''}", None))
            continue
        changes = []
        if not _same_num(xa["mass"], xb["mass"]):
            changes.append(f"质量 {xa['mass']:g}→{xb['mass']:g}g")
        if xa["locked"] != xb["locked"]:
            changes.append(f"{'锁定' if xa['locked'] else '未锁定'}"
                           f"→{'锁定' if xb['locked'] else '未锁定'}")
        elif xa["locked"] and xa["slot"] != xb["slot"]:
            changes.append(f"锁定槽 {xa['slot']}→{xb['slot']}")
        if changes:
            tubes.append(_diff_entry(f"试管 {tid}", "", 1,
                                     xa["mass"], xb["mass"]))
            tubes[-1]["change"] = "; ".join(changes)

    same_conditions = not (rotor or solver or tubes)
    differing_groups = []
    if rotor:
        differing_groups.append("rotor")
    if solver:
        differing_groups.append("solver")
    if tubes:
        differing_groups.append("tubes")
    return {
        "rotor": rotor,
        "solver": solver,
        "tubes": tubes,
        "same_conditions": same_conditions,
        "differing_groups": differing_groups,
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def compare_plans(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """比较两个已保存方案(GET /api/plan/{id} 返回结构)。

    a 为基准方案, b 为对比方案, 差值统一为 b - a。
    """
    placements = merge_placements(a, b)
    counts = {
        "added": sum(1 for r in placements if r["status"] == "added"),
        "removed": sum(1 for r in placements if r["status"] == "removed"),
        "moved": sum(1 for r in placements if r["status"] == "moved"),
        "unchanged": sum(1 for r in placements if r["status"] == "unchanged"),
    }
    inputs = compare_inputs(a, b)
    return {
        "plan_a": {
            "id": a.get("id"),
            "name": a.get("name"),
            "created_at": a.get("created_at"),
            "grade": a["result"]["metrics"].get("grade"),
        },
        "plan_b": {
            "id": b.get("id"),
            "name": b.get("name"),
            "created_at": b.get("created_at"),
            "grade": b["result"]["metrics"].get("grade"),
        },
        "same_conditions": inputs["same_conditions"],
        "differing_groups": inputs["differing_groups"],
        "input_diff": inputs,
        "metrics": compare_metrics(a, b),
        "placements": placements,
        "placement_counts": counts,
    }
