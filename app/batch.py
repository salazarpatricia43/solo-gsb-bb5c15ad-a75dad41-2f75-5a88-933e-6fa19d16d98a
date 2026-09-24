"""已保存配平方案的批次换装顺序规划(只读, 不改变求解与保存格式)。

问题模型
--------
从历史方案中选出 k 条(2 ≤ k ≤ 10)已保存方案, 转子从**空载**开始, 逐批把
转子换装成各方案的摆放。每次切换到下一方案时分两步:

1. **先卸**: 卸下所有"不在下一方案原槽位"的试管——当前在槽 s 的试管, 若
   下一方案并未把同一支试管放在槽 s(该管在下一方案中换了槽或不再使用),
   都必须卸下;
2. **后装**: 装入下一方案所需的试管——下一方案要求槽 s 放置的试管若当前
   不在槽 s, 则装入(换槽的试管 = 先卸后装, 共 2 次操作)。

每卸下一支或装入一支试管各计 **1 次操作**; 同一支试管在相邻两方案中同槽
则保留不动, 不计操作。从空转子到首个方案只有装入操作。

精确求序(Held-Karp 子集动态规划)
--------------------------------
一批的换装成本只取决于相邻两条方案的摆放交集, 与更早的批次无关, 因此执行
次序是 k 条方案上的最短哈密顿路径问题。k ≤ 10, 用 Held-Karp 子集动态规划
**精确**求解(不做贪心或近似):

    best(mask, prev) = 当前处于 prev 方案状态(或空转子)时, 再执行 mask 中
                       全部方案所需的最小后续操作数
    best(∅, prev)    = 0
    best(mask, prev) = min_{c ∈ mask} [ trans(prev, c) + best(mask \\ {c}, c) ]

    trans(prev, c) = occ(prev) + occ(c) − 2·keep(prev, c)   (prev 非空转子)
    trans(空, c)   = occ(c)

其中 occ(p) 为方案 p 的已放置试管数, keep(prev, c) 为两方案"同槽同管"的
槽位数。状态数 2^k·(k+1)(k = 10 时约 1.1 万), 精确且瞬时完成; 同一递归
还统计达到最小总操作数的次序条数。

同成本裁决
----------
达到最小总操作数的次序可能不止一条, 此时按**方案 ID 序列升序**(字典序)
裁决: 从首个批次起逐位尝试剩余方案中 ID 最小者, 若以它开头仍存在总成本
为最小值的完整次序则选定之, 再固定位次继续裁决下一位。

兼容性约束(不满足则拒绝, HTTP 400)
----------------------------------
- 各方案转子几何必须一致: 半径 R、逐槽角度与启用状态完全相同;
- 同一编号试管在不同方案中的标称质量必须一致(同一支实体试管不应有两种
  质量), 否则拒绝编排批次。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

# 与 app/compare.py 一致的数值容差
_NUM_TOL = 1e-9
MIN_PLANS = 2
MAX_PLANS = 10


def _same_num(a: float, b: float, tol: float = _NUM_TOL) -> bool:
    return abs(float(a) - float(b)) <= tol


def _slot_signature(plan: dict[str, Any]) -> dict[int, tuple[float, bool]]:
    """方案请求中的槽位几何签名: {槽号: (角度°, 是否启用)}。"""
    req = plan.get("request") or {}
    return {
        int(s["index"]): (float(s.get("angle", 0.0)), bool(s.get("enabled", True)))
        for s in (req.get("slots") or [])
    }


def _placements(plan: dict[str, Any]) -> dict[int, tuple[int, float]]:
    """方案结果中的摆放映射: {槽号: (试管编号, 质量g)}; 仅含已放置试管。"""
    out: dict[int, tuple[int, float]] = {}
    for p in (plan.get("result") or {}).get("placements") or []:
        if p.get("slot") is None:
            continue
        slot = int(p["slot"])
        if slot in out:
            raise ValueError(
                f"方案 {plan.get('id')} 的摆放明细中槽位 {slot} 被重复占用。"
            )
        out[slot] = (int(p["tube_id"]), float(p["mass"]))
    return out


def _validate(plans: list[dict[str, Any]]) -> None:
    if not (MIN_PLANS <= len(plans) <= MAX_PLANS):
        raise ValueError(
            f"批次换装需选择 {MIN_PLANS}~{MAX_PLANS} 条已保存方案"
            f"(当前 {len(plans)} 条)。"
        )
    ids = [int(p.get("id")) for p in plans]
    if len(set(ids)) != len(ids):
        raise ValueError("方案 ID 重复: 每条方案在一个批次中只能出现一次。")

    base = plans[0]
    base_id = int(base.get("id"))
    base_radius = float((base.get("request") or {}).get("radius_mm", 100.0))
    base_slots = _slot_signature(base)
    for p in plans[1:]:
        pid = int(p.get("id"))
        radius = float((p.get("request") or {}).get("radius_mm", 100.0))
        if not _same_num(radius, base_radius):
            raise ValueError(
                f"方案 {pid} 的转子半径({radius:g} mm)与方案 {base_id}"
                f"({base_radius:g} mm)不一致, 不能编入同一换装批次。"
            )
        sig = _slot_signature(p)
        if set(sig) != set(base_slots):
            raise ValueError(
                f"方案 {pid} 的槽位构成与方案 {base_id} 不一致, "
                "不能编入同一换装批次。"
            )
        for idx in sorted(sig):
            ang, en = sig[idx]
            bang, ben = base_slots[idx]
            if not _same_num(ang, bang) or en != ben:
                raise ValueError(
                    f"方案 {pid} 的槽位 {idx}"
                    f"({ang:g}°/{'启用' if en else '禁用'})与方案 {base_id}"
                    f"({bang:g}°/{'启用' if ben else '禁用'})不一致, "
                    "不能编入同一换装批次。"
                )

    mass_by_tube: dict[int, tuple[float, int]] = {}
    for p in plans:
        pid = int(p.get("id"))
        for tid, mass in _placements(p).values():
            known = mass_by_tube.get(tid)
            if known is None:
                mass_by_tube[tid] = (mass, pid)
            elif not _same_num(mass, known[0]):
                raise ValueError(
                    f"试管 {tid} 在方案 {known[1]} 中质量为 {known[0]:g} g, "
                    f"在方案 {pid} 中为 {mass:g} g: 同编号试管质量不一致, "
                    "拒绝编排批次(同一支试管不应有两种质量)。"
                )


def plan_batch_sequence(plans: list[dict[str, Any]]) -> dict[str, Any]:
    """对 2~10 条已保存方案精确规划批次换装次序(只读)。

    参数
    ----
    plans: GET /api/plan/{id} 返回结构的方案列表(含 id/name/request/result),
        传入顺序不影响结果。

    返回
    ----
    含最优执行次序、总操作数、达到最小值的次序条数、逐批卸装清单与单次/
    累计操作数的字典; 不修改入参对应的任何方案数据。
    """
    _validate(plans)
    k = len(plans)
    ids = [int(p["id"]) for p in plans]
    occ_maps = [_placements(p) for p in plans]
    occ = [len(m) for m in occ_maps]

    # keep[i][j]: 方案 i 与 j "同槽同管"的槽位数(换装时保留不动的试管数)
    keep = [[0] * k for _ in range(k)]
    for i in range(k):
        mi = occ_maps[i]
        for j in range(k):
            if i == j:
                continue
            mj = occ_maps[j]
            keep[i][j] = sum(
                1 for s, (t, _) in mi.items() if mj.get(s, (None,))[0] == t
            )

    START = k  # 虚拟起点: 空转子

    def trans(prev: int, nxt: int) -> int:
        if prev == START:
            return occ[nxt]
        return occ[prev] + occ[nxt] - 2 * keep[prev][nxt]

    @lru_cache(maxsize=None)
    def best(mask: int, prev: int) -> tuple[int, int]:
        """当前处于 prev 方案状态(START=空转子), 再执行 mask 中全部方案的
        (最小后续操作数, 达到该最小值的次序条数)。"""
        if mask == 0:
            return (0, 1)
        best_cost = None
        count = 0
        for c in range(k):
            if not (mask >> c) & 1:
                continue
            sub_cost, sub_cnt = best(mask ^ (1 << c), c)
            cand = trans(prev, c) + sub_cost
            if best_cost is None or cand < best_cost:
                best_cost, count = cand, sub_cnt
            elif cand == best_cost:
                count += sub_cnt
        assert best_cost is not None
        return (best_cost, count)

    full = (1 << k) - 1
    total, n_optimal = best(full, START)

    # 同成本裁决: 逐位选取仍能达到最小总操作数的、方案 ID 最小的方案
    order: list[int] = []
    mask, prev = full, START
    while mask:
        for c in sorted((c for c in range(k) if (mask >> c) & 1),
                        key=lambda c: ids[c]):
            sub_cost, _ = best(mask ^ (1 << c), c)
            if trans(prev, c) + sub_cost == best(mask, prev)[0]:
                order.append(c)
                mask ^= 1 << c
                prev = c
                break

    # 逐批卸装清单(角度取自共享槽位几何)
    base_slots = _slot_signature(plans[0])
    angle_of = {idx: ang for idx, (ang, _) in base_slots.items()}

    def _row(slot: int, tube: tuple[int, float]) -> dict[str, Any]:
        return {
            "tube_id": tube[0],
            "mass": tube[1],
            "slot": slot,
            "angle": angle_of.get(slot),
        }

    batches: list[dict[str, Any]] = []
    cumulative = 0
    prev_map: dict[int, tuple[int, float]] = {}
    for seq, idx in enumerate(order, start=1):
        cur = occ_maps[idx]
        unload = [
            _row(s, t) for s, t in sorted(prev_map.items())
            if cur.get(s, (None,))[0] != t[0]
        ]
        load = [
            _row(s, t) for s, t in sorted(cur.items())
            if prev_map.get(s, (None,))[0] != t[0]
        ]
        kept = [
            _row(s, t) for s, t in sorted(cur.items())
            if prev_map.get(s, (None,))[0] == t[0]
        ]
        ops = len(unload) + len(load)
        cumulative += ops
        batches.append({
            "seq": seq,
            "plan_id": ids[idx],
            "plan_name": plans[idx].get("name"),
            "unload": unload,
            "load": load,
            "kept": kept,
            "unload_count": len(unload),
            "load_count": len(load),
            "kept_count": len(kept),
            "operations": ops,
            "cumulative_operations": cumulative,
        })
        prev_map = cur
    assert cumulative == total

    return {
        "method": (
            "Held-Karp 子集动态规划精确求最小总操作数(空转子起步, "
            "每卸下或装入一支试管各计 1 次); 同成本按方案 ID 序列升序裁决"
        ),
        "plan_count": k,
        "total_operations": total,
        "optimal_order_count": n_optimal,
        "tie_broken": n_optimal > 1,
        "order": [ids[i] for i in order],
        "plans": [
            {
                "id": ids[i],
                "name": plans[i].get("name"),
                "created_at": plans[i].get("created_at"),
                "tube_count": occ[i],
            }
            for i in order
        ],
        "batches": batches,
        "rotor": {
            "radius_mm": float(
                (plans[0].get("request") or {}).get("radius_mm", 100.0)
            ),
            "slot_count": len(base_slots),
            "slots": [
                {"index": idx, "angle": ang, "enabled": en}
                for idx, (ang, en) in sorted(base_slots.items())
            ],
        },
        "note": "批次换装顺序为只读规划结果, 未修改任何已保存方案。",
    }
