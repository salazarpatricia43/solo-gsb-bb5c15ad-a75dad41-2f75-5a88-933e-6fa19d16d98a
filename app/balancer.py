"""离心机转子配平引擎。

模型假设:
- 单面(single-plane)转子, 所有试管位于同一半径 R 的圆周上;
- 槽位角度由用户给定(度), 0° 在正上方, 顺时针为正(前端按同一约定绘制);
- 每个可用槽位最多放置一支试管, 禁用槽位不能放置;
- 质量单位克(g), 半径单位毫米(mm)。

物理量:
- 一阶不平衡量 U1 = Σ m_k · R · e^{iθ_k}        (质径积, g·mm)  → 离心合力方向
- 二阶不平衡量 U2 = Σ m_k · R · e^{i·2θ_k}      (g·mm)          → 2 倍频残余
- 离心合力 F = ω² · |U1|/1000  (N, 质量换算为 kg)
- 合力矩(关于轴心) τ = F · R/1000  (N·m)
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Optional

TWO_PI = 2.0 * math.pi


# ---------------------------------------------------------------------------
# 几何工具
# ---------------------------------------------------------------------------
def _d(angle_deg: float) -> float:
    """角度(度) → 弧度。"""
    return math.radians(angle_deg)


def signed_angle_diff(a: float, b: float) -> float:
    """a-b 归一化到 [-180, 180]。"""
    return (a - b + 180.0) % 360.0 - 180.0


def angle_between(a: float, b: float) -> float:
    """两个角度之间的最小夹角 [0, 180]。"""
    return abs(signed_angle_diff(a, b))


def opposite_deviation(a: float, b: float) -> float:
    """槽位 a 与 b 偏离严格对径(180°)的程度 [0,180]。"""
    return abs(abs(signed_angle_diff(a, b)) - 180.0)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    index: int
    angle: float          # 度
    enabled: bool = True


@dataclass
class Tube:
    id: int
    mass: float
    locked: bool = False
    slot: Optional[int] = None   # 锁定试管的固定槽位; 非锁定为 None


@dataclass
class Solver:
    slots: list[Slot]
    tubes: list[Tube]
    radius_mm: float = 100.0
    rpm: float = 4000.0
    w1: float = 1.0             # 一阶不平衡量目标权重(主导)
    w2: float = 0.000001        # 二阶不平衡量目标权重(仅在一阶几乎持平时作裁决)
    move_weight: float = 0.0    # 增量重排: 每支被移动试管的罚分(g² 当量); 默认不限制移动
    restarts: int = 10
    chain_steps: int = 2500
    seed: int = 20260923
    # 互斥试管对(试管编号二元组): 每对试管不得落在按角度环形排序后相邻的物理槽位;
    # 非空时改用精确完全枚举(含互斥对的请求限至多 7 管、10 个启用槽)
    mutex_pairs: list[tuple[int, int]] = field(default_factory=list)

    # —— 运行期状态 ——
    _enc: dict[int, int] = field(default_factory=dict)   # slot 索引 → tube id
    _loc: dict[int, int] = field(default_factory=dict)   # tube id → slot 索引
    _origin: dict[int, Optional[int]] = field(default_factory=dict)
    _u1: complex = 0j
    _u2: complex = 0j
    _rng: Optional[random.Random] = None
    _steps: list[dict[str, Any]] = field(default_factory=list)
    _track: bool = False
    _score_cache: float = 0.0
    _track_best: float = math.inf
    _anneal_recorded: int = 0
    _anneal_record_cap: int = 24
    # —— 互斥精确搜索运行期状态 ——
    _adj: dict[int, set[int]] = field(default_factory=dict)       # 物理槽环形相邻关系
    _mutex_partners: dict[int, set[int]] = field(default_factory=dict)
    _exact_info: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        self._chain_steps = self.chain_steps

    # ------------------------------------------------------------------
    # 校验与预处理
    # ------------------------------------------------------------------
    def validate(self) -> None:
        if not self.slots:
            raise ValueError("至少需要定义一个槽位。")
        n = len(self.slots)
        for s in self.slots:
            if not (0 <= s.index < n):
                raise ValueError(f"槽位索引 {s.index} 超出范围。")
        seen = set()
        for s in self.slots:
            if not s.enabled:
                continue
            key = round(s.angle % 360.0, 6)
            if key in seen:
                raise ValueError(f"槽位 {s.index} 与其他启用槽位角度重复({s.angle}°)。")
            seen.add(key)
        enabled = {s.index for s in self.slots if s.enabled}
        if not enabled:
            raise ValueError("至少需要启用一个槽位。")
        ids = set()
        for t in self.tubes:
            if t.id in ids:
                raise ValueError(f"试管编号 {t.id} 重复。")
            ids.add(t.id)
            if t.mass <= 0:
                raise ValueError(f"试管 {t.id} 质量必须为正数。")
        for t in self.tubes:
            if t.locked:
                if t.slot is None:
                    raise ValueError(f"锁定试管 {t.id} 必须指定槽位。")
                if t.slot not in range(n):
                    raise ValueError(f"锁定试管 {t.id} 的槽位 {t.slot} 不存在。")
                if t.slot not in enabled:
                    raise ValueError(f"锁定试管 {t.id} 不能放在禁用槽位 {t.slot}。")
        locked_slots = [t.slot for t in self.tubes if t.locked]
        if len(set(locked_slots)) != len(locked_slots):
            raise ValueError("同一槽位不能放置多支锁定试管。")
        if len([t for t in self.tubes]) > len(enabled):
            raise ValueError(
                f"试管数量({len(self.tubes)})超过可用槽位数量({len(enabled)})。"
            )
        if self.radius_mm <= 0 or self.rpm < 0:
            raise ValueError("半径必须为正, 转速不能为负。")
        self._validate_mutex_pairs(enabled)

    def _validate_mutex_pairs(self, enabled: set[int]) -> None:
        """校验互斥对输入, 并检测锁定试管互斥冲突(结构性不可行)。

        - 至多 12 对、成对编号不同、编号必须存在、不可重复(无序去重);
        - 含互斥对的请求限至多 7 支试管、10 个启用槽;
        - 两支互斥试管若均锁定, 其固定槽在物理环形相邻关系(禁用槽仍占位)
          上相邻, 则结构上无合法摆放。
        """
        if not self.mutex_pairs:
            return
        if len(self.mutex_pairs) > 12:
            raise ValueError(f"互斥试管对最多 12 对, 当前 {len(self.mutex_pairs)} 对。")
        ids = {t.id for t in self.tubes}
        seen: set[tuple[int, int]] = set()
        partners: dict[int, set[int]] = {}
        for a, b in self.mutex_pairs:
            if a == b:
                raise ValueError(f"互斥对 ({a}, {b}) 无效: 试管不能与自身互斥。")
            if a not in ids or b not in ids:
                raise ValueError(f"互斥对 ({a}, {b}) 引用了不存在的试管编号。")
            key = (a, b) if a < b else (b, a)
            if key in seen:
                raise ValueError(f"互斥试管对 ({key[0]}, {key[1]}) 重复填写。")
            seen.add(key)
            partners.setdefault(key[0], set()).add(key[1])
            partners.setdefault(key[1], set()).add(key[0])
        # 规范化为按小编号优先的有序对
        self.mutex_pairs = sorted(seen)
        if len(self.tubes) > 7:
            raise ValueError(
                f"含互斥对的请求至多允许 7 支试管, 当前 {len(self.tubes)} 支。"
            )
        if len(enabled) > 10:
            raise ValueError(
                f"含互斥对的请求至多允许 10 个启用槽, 当前 {len(enabled)} 个。"
            )
        # 锁定-锁定互斥冲突: 固定槽物理相邻即结构上不可行
        adj = self._ring_adjacency()
        locked_slot = {t.id: t.slot for t in self.tubes if t.locked}
        for a, b in self.mutex_pairs:
            if a in locked_slot and b in locked_slot:
                sa, sb = locked_slot[a], locked_slot[b]
                if sb in adj[sa]:
                    raise ValueError(
                        f"不可行: 互斥试管 {a} 与 {b} 均已锁定, 而锁定槽位 {sa} 与 {sb} "
                        f"在按角度环形排序后相邻(禁用槽仍占据相邻关系)。"
                    )
        self._mutex_partners = partners

    def _ring_adjacency(self) -> dict[int, set[int]]:
        """全部物理槽(含禁用槽)按角度环形排序后的相邻关系。

        同角度时按槽位索引排序; 相邻关系建立在物理槽位之间, 因此禁用槽
        仍然"占位"——禁用槽两侧的启用槽并不相邻。N=2 时两槽互为前后邻,
        去重后只连一条边。
        """
        order = sorted(self.slots, key=lambda s: (s.angle % 360.0, s.index))
        adj: dict[int, set[int]] = {s.index: set() for s in self.slots}
        m = len(order)
        if m < 2:
            return adj
        for i in range(m):
            x, y = order[i].index, order[(i + 1) % m].index
            if x != y:
                adj[x].add(y)
                adj[y].add(x)
        return adj

    # ------------------------------------------------------------------
    # 复向量
    # ------------------------------------------------------------------
    def _c1(self, slot_idx: int, m: float) -> complex:
        a = _d(self.slots[slot_idx].angle)
        return complex(m * math.cos(a), m * math.sin(a))

    def _c2(self, slot_idx: int, m: float) -> complex:
        a = _d(self.slots[slot_idx].angle)
        return complex(m * math.cos(2 * a), m * math.sin(2 * a))

    def _penalty(self, loc: dict[int, int]) -> float:
        moved = sum(
            1 for tid, slot in loc.items()
            if not self._by_id[tid].locked and self._origin.get(tid) is not None
            and self._origin[tid] != slot
        )
        return self.move_weight * moved

    # ------------------------------------------------------------------
    # 状态初始化
    # ------------------------------------------------------------------
    def _reset(self) -> None:
        self._enc = {}
        self._loc = {}
        self._u1 = 0j
        self._u2 = 0j
        self._kept = []
        self._steps = []
        self._exact_info = None
        self._by_id = {t.id: t for t in self.tubes}

    def _place(self, tube_id: int, slot_idx: int) -> None:
        t = self._by_id[tube_id]
        self._enc[slot_idx] = tube_id
        self._loc[tube_id] = slot_idx
        self._u1 += self._c1(slot_idx, t.mass)
        self._u2 += self._c2(slot_idx, t.mass)

    def _remove(self, tube_id: int) -> None:
        slot_idx = self._loc.pop(tube_id)
        t = self._by_id[tube_id]
        del self._enc[slot_idx]
        self._u1 -= self._c1(slot_idx, t.mass)
        self._u2 -= self._c2(slot_idx, t.mass)

    def _score(self, u1: complex, u2: complex, loc: dict[int, int]) -> float:
        return (
            self.w1 * abs(u1) ** 2
            + self.w2 * abs(u2) ** 2
            + self._penalty(loc)
        )

    # ------------------------------------------------------------------
    # 锁定试管放置
    # ------------------------------------------------------------------
    def _place_locked(self, record: bool = True) -> None:
        for t in sorted((x for x in self.tubes if x.locked), key=lambda x: x.id):
            assert t.slot is not None
            self._place(t.id, t.slot)
        if record and self._track:
            n_lock = len([t for t in self.tubes if t.locked])
            if n_lock:
                self._snapshot("init", None, f"载入 {n_lock} 支锁定试管作为初始约束。")
            else:
                self._snapshot("init", None, "开始贪心构造初始对称布局。")

    # ------------------------------------------------------------------
    # 初始布局构造
    # variants:
    #   0                              —— 质量贪心配对, 槽位确定性打分
    #   1 .. GREEDY_VARIANTS           —— 随机化配对 + 槽位打分抖动
    #   > GREEDY_VARIANTS              —— 完全随机摆放(增强搜索多样性)
    #   "incremental"                  —— 优先沿用旧布局
    # ------------------------------------------------------------------
    GREEDY_VARIANTS = 4

    def _free_pairs(self, variant: int) -> list[list[int]]:
        free = [t for t in self.tubes if not t.locked]
        if variant != 0:
            # 随机变体: 洗牌改变配对构成与放置顺序
            self._rng.shuffle(free)
        else:
            # 质量贪心: 重试管先放, 相邻质量优先配对
            free = sorted(free, key=lambda t: (-t.mass, t.id))
        pairs: list[list[int]] = []
        for i in range(0, len(free) - 1, 2):
            pairs.append([free[i].id, free[i + 1].id])
        if len(free) % 2 == 1:
            pairs.append([free[-1].id])
        return pairs

    def _construct_random(self) -> None:
        free_slots = [s.index for s in self.slots if s.enabled]
        free_tubes = [t.id for t in self.tubes if not t.locked]
        self._rng.shuffle(free_slots)
        self._rng.shuffle(free_tubes)
        for tid, slot in zip(free_tubes, free_slots):
            self._place(tid, slot)
        if self._track:
            self._snapshot(
                "init", free_tubes,
                f"随机起点: 将 {len(free_tubes)} 支试管随机分布到可用槽位, 交由模拟退火搜索。",
            )

    def _place_group_greedy(self, group: list[int], variant: int) -> tuple[int, Optional[int]]:
        """把一个配对组(1~2 支试管)放到使目标增量最小的槽位。"""
        avail = [s.index for s in self.slots if s.enabled and s.index not in self._enc]
        jitter = 1e-9 if variant != 0 else 0.0

        if len(group) == 2:
            id0, id1 = group[0], group[1]
            t0, t1 = self._by_id[id0], self._by_id[id1]
            best: Optional[tuple[float, float, int, Optional[int]]] = None
            for x in range(len(avail)):
                for y in range(x + 1, len(avail)):
                    a, b = avail[x], avail[y]
                    dev = opposite_deviation(self.slots[a].angle, self.slots[b].angle)
                    # 两种朝向: t0→a/t1→b 与 t0→b/t1→a
                    for ia, ib, ta, tb in ((a, b, t0, t1), (b, a, t0, t1)):
                        nu1 = self._u1 + self._c1(ia, ta.mass) + self._c1(ib, tb.mass)
                        nu2 = self._u2 + self._c2(ia, ta.mass) + self._c2(ib, tb.mass)
                        trial_loc = dict(self._loc)
                        trial_loc[ta.id] = ia
                        trial_loc[tb.id] = ib
                        sc = self._score(nu1, nu2, trial_loc)
                        if jitter:
                            sc += self._rng.random() * jitter
                        # 同分时偏好接近对径的槽位对
                        cand = (sc, dev, a, b)
                        if best is None or cand < best:
                            best = cand
            assert best is not None
            _, _, a, b = best
            # 在 (a, b) 上比较两种朝向, 确定谁进 a
            def orient_score(sa, sb, ta, tb):
                nu1 = self._u1 + self._c1(sa, ta.mass) + self._c1(sb, tb.mass)
                nu2 = self._u2 + self._c2(sa, ta.mass) + self._c2(sb, tb.mass)
                trial_loc = dict(self._loc)
                trial_loc[ta.id] = sa
                trial_loc[tb.id] = sb
                return self._score(nu1, nu2, trial_loc)

            if orient_score(a, b, t0, t1) <= orient_score(a, b, t1, t0):
                self._place(t0.id, a)
                self._place(t1.id, b)
            else:
                self._place(t1.id, a)
                self._place(t0.id, b)
            return a, b

        # 单支
        t = self._by_id[group[0]]
        best = None
        for a in avail:
            nu1 = self._u1 + self._c1(a, t.mass)
            nu2 = self._u2 + self._c2(a, t.mass)
            trial_loc = dict(self._loc)
            trial_loc[t.id] = a
            sc = self._score(nu1, nu2, trial_loc)
            if jitter:
                sc += self._rng.random() * jitter
            if best is None or sc < best[0]:
                best = (sc, a)
        a = best[1]
        self._place(t.id, a)
        return a, None

    def _seed_incremental(self, previous: list[dict[str, Any]]) -> list[int]:
        """增量重排: 沿用上一方案中仍然有效的摆放, 返回仍需放置的试管 id。"""
        prev_by_tube = {}
        for item in previous:
            tid = int(item["tube_id"])
            prev_by_tube[tid] = int(item["slot"])
        cur_ids = {t.id for t in self.tubes}
        kept: list[int] = []
        blocked: list[int] = []
        occupied = set(self._loc.values())  # 锁定试管占用的槽
        # 按旧槽顺序依次保留, 冲突者进入 blocked
        ordered = sorted(
            (tid for tid in prev_by_tube if tid in cur_ids),
            key=lambda tid: (prev_by_tube[tid], tid),
        )
        for tid in ordered:
            t = self._by_id[tid]
            if t.locked:
                continue
            old_slot = prev_by_tube[tid]
            slot_ok = (
                0 <= old_slot < len(self.slots)
                and self.slots[old_slot].enabled
                and old_slot not in occupied
            )
            if slot_ok:
                self._place(tid, old_slot)
                occupied.add(old_slot)
                kept.append(tid)
            else:
                blocked.append(tid)
        new_tubes = [
            t.id for t in self.tubes if not t.locked and t.id not in prev_by_tube
        ]
        # blocked 中的试管旧槽失效/冲突, 稍后必须重新放置
        self._kept = kept
        if self._track:
            if kept:
                self._snapshot(
                    "init", kept,
                    f"增量重排: {len(kept)} 支试管沿用上一方案槽位"
                    f"({', '.join(map(str, kept))}); "
                    f"{len(blocked)} 支旧槽失效/冲突需重放, "
                    f"{len(new_tubes)} 支为新增试管。",
                )
            else:
                self._snapshot(
                    "init", None,
                    f"增量重排: 没有可沿用的旧摆放, "
                    f"{len(new_tubes) + len(blocked)} 支试管待放置。",
                )
        # 剩余待放试管: 被阻塞的 + 新增的(已保留的不再返回)
        return blocked + new_tubes

    def _construct(self, variant, previous=None):
        self._reset()
        self._rng = random.Random(self.seed + (0 if variant == "incremental" else variant))
        incremental = variant == "incremental"
        self._place_locked(record=not incremental)
        if isinstance(variant, int) and variant > self.GREEDY_VARIANTS:
            # 完全随机起点
            self._construct_random()
            return self._score(self._u1, self._u2, self._loc)
        if incremental:
            groups = [[tid] for tid in self._seed_incremental(previous)]
        else:
            groups = self._free_pairs(variant)
        for g in groups:
            avail = [s for s in self.slots if s.enabled and s.index not in self._enc]
            if not avail:
                break
            if len(g) == 2 and len(avail) < 2:
                g = g[:1]
            sa, sb = self._place_group_greedy(g, variant if variant != "incremental" else 0)
            if self._track:
                self._record_build(g, sa, sb)
        return self._score(self._u1, self._u2, self._loc)

    def _record_build(self, group, sa, sb) -> None:
        if len(group) == 2 and sb is not None:
            t1, t2 = self._by_id[group[0]], self._by_id[group[1]]
            dev = opposite_deviation(self.slots[sa].angle, self.slots[sb].angle)
            if dev <= 0.5:
                geo = f"槽位 {sa} 与 {sb} 互为对径({self.slots[sa].angle:g}°/{self.slots[sb].angle:g}°)"
            else:
                geo = (f"槽位 {sa} 与 {sb} 夹角 {angle_between(self.slots[sa].angle, self.slots[sb].angle):.1f}°"
                       f"(偏离对径 {dev:.1f}°)")
            dm = abs(t1.mass - t2.mass)
            if dm < 1e-9:
                mass_txt = "质量完全相等"
            else:
                mass_txt = f"质量差 {dm:g} g, 为剩余试管中最接近的一对"
            reason = f"试管 {t1.id}({t1.mass:g}g) 与 {t2.id}({t2.mass:g}g) 配对: {mass_txt}; {geo}, 一阶力近似对消。"
            kind = "pair"
            ref = sorted(group)
        else:
            t = self._by_id[group[0]]
            reason = (f"试管 {t.id}({t.mass:g}g) 放入槽位 {sa}({self.slots[sa].angle:g}°), "
                      f"作为当前残余向量的补偿方向。")
            kind = "single"
            ref = [t.id]
        self._snapshot(kind, ref, reason)

    # ------------------------------------------------------------------
    # 局部搜索: 交换(两支试管) / 迁移(移到空槽)
    # ------------------------------------------------------------------
    def _move_delta(self, tid: int, frm: int, to: int) -> float:
        """试管 tid 从 frm 移到 to 后的目标变化(不含另一支)。"""
        t = self._by_id[tid]
        nu1 = self._u1 - self._c1(frm, t.mass) + self._c1(to, t.mass)
        nu2 = self._u2 - self._c2(frm, t.mass) + self._c2(to, t.mass)
        trial = dict(self._loc)
        trial[tid] = to
        return self._score(nu1, nu2, trial) - self._score_cache, nu1, nu2

    def _swap_delta(self, tid_a: int, a: int, tid_b: int, b: int) -> float:
        ta, tb = self._by_id[tid_a], self._by_id[tid_b]
        nu1 = (
            self._u1
            - self._c1(a, ta.mass) - self._c1(b, tb.mass)
            + self._c1(b, ta.mass) + self._c1(a, tb.mass)
        )
        nu2 = (
            self._u2
            - self._c2(a, ta.mass) - self._c2(b, tb.mass)
            + self._c2(b, ta.mass) + self._c2(a, tb.mass)
        )
        trial = dict(self._loc)
        trial[tid_a] = b
        trial[tid_b] = a
        return self._score(nu1, nu2, trial) - self._score_cache, nu1, nu2

    # ------------------------------------------------------------------
    # 局部搜索: 模拟退火(逃出局部最优) + 爬山(收敛)
    # ------------------------------------------------------------------
    def _state(self):
        return dict(self._loc), complex(self._u1), complex(self._u2)

    def _restore(self, state) -> None:
        loc, u1, u2 = state
        self._loc = dict(loc)
        self._enc = {v: k for k, v in self._loc.items()}
        self._u1, self._u2 = u1, u2

    def _apply_move(self, move) -> tuple[float, str]:
        """执行一个邻域动作, 返回 (目标变化, 描述)。"""
        if move[0] == "move":
            _, tid, a, b, d, nu1, nu2 = move
            self._remove(tid)
            self._place(tid, b)
            self._u1, self._u2 = nu1, nu2
            return d, (f"微调: 试管 {tid} 由槽位 {a} 迁到空槽 {b}")
        _, ti, a, tj, b, d, nu1, nu2 = move
        self._remove(ti)
        self._remove(tj)
        self._place(ti, b)
        self._place(tj, a)
        self._u1, self._u2 = nu1, nu2
        return d, (f"微调: 交换试管 {ti}(槽 {a}) 与试管 {tj}(槽 {b})")

    def _all_moves(self) -> list[tuple]:
        free_ids = [t.id for t in self.tubes if not t.locked]
        enabled_slots = [s.index for s in self.slots if s.enabled]
        empty = [s for s in enabled_slots if s not in self._enc]
        moves: list[tuple] = []
        self._score_cache = self._score(self._u1, self._u2, self._loc)
        for tid in free_ids:
            a = self._loc[tid]
            for b in empty:
                if a == b:
                    continue
                d, nu1, nu2 = self._move_delta(tid, a, b)
                moves.append(("move", tid, a, b, d, nu1, nu2))
        for i in range(len(free_ids)):
            for j in range(i + 1, len(free_ids)):
                ti, tj = free_ids[i], free_ids[j]
                a, b = self._loc[ti], self._loc[tj]
                d, nu1, nu2 = self._swap_delta(ti, a, tj, b)
                moves.append(("swap", ti, a, tj, b, d, nu1, nu2))
        return moves

    def _random_move(self):
        free_ids = [t.id for t in self.tubes if not t.locked]
        enabled_slots = [s.index for s in self.slots if s.enabled]
        empty = [s for s in enabled_slots if s not in self._enc]
        candidates = (1 if empty else 0) + 1  # move / swap
        kind = "move" if (empty and self._rng.randrange(candidates) == 0) else "swap"
        self._score_cache = self._score(self._u1, self._u2, self._loc)
        if kind == "move":
            tid = self._rng.choice(free_ids)
            a = self._loc[tid]
            b = self._rng.choice(empty)
            while b == a and len(empty) > 1:
                b = self._rng.choice(empty)
            if a == b:
                return None
            d, nu1, nu2 = self._move_delta(tid, a, b)
            return ("move", tid, a, b, d, nu1, nu2)
        if len(free_ids) < 2:
            return None
        ti, tj = self._rng.sample(free_ids, 2)
        a, b = self._loc[ti], self._loc[tj]
        d, nu1, nu2 = self._swap_delta(ti, a, tj, b)
        return ("swap", ti, a, tj, b, d, nu1, nu2)

    def _anneal_chain(self, steps: int, t0_frac: float = 0.05) -> None:
        """一次退火短链: 从当前布局出发, 指数降温。"""
        if len([t for t in self.tubes if not t.locked]) < 2:
            return
        cur_score = self._score(self._u1, self._u2, self._loc)
        t0 = max(cur_score * t0_frac, 1e-6)
        t1 = 1e-7
        cooling = (t1 / t0) ** (1.0 / max(steps, 1))
        temp = t0
        for _ in range(steps):
            move = self._random_move()
            if move is None:
                break
            d = move[4]
            if d < 0 or self._rng.random() < math.exp(-d / max(temp, 1e-18)):
                self._apply_move(move)
                cur_score += d
                # 仅记录追踪运行中"刷新全局最优"的改进, 保持回放精简
                if self._track and d < -1e-9 and cur_score < self._track_best - 1e-9 \
                        and self._anneal_recorded < self._anneal_record_cap:
                    self._track_best = cur_score
                    self._anneal_recorded += 1
                    self._record_anneal_step(move, d)
            temp *= cooling

    def _record_anneal_step(self, move, d: float) -> None:
        if move[0] == "move":
            _, tid, a, b, _, _, _ = move
            desc = f"退火搜索: 试管 {tid} 由槽位 {a} 迁到空槽 {b}"
            refs = [tid]
        else:
            _, ti, a, tj, b, _, _, _ = move
            desc = f"退火搜索: 交换试管 {ti}(槽 {a}) 与试管 {tj}(槽 {b})"
            refs = [ti, tj]
        self._snapshot(
            "refine", refs,
            f"{desc}, 目标函数下降 {abs(d):.4g}, 残余不平衡量减小。",
        )

    def _hill_climb(self, record_refines: bool = False) -> int:
        improvements = 0
        max_passes = 400
        for _ in range(max_passes):
            moves = self._all_moves()
            best = min(moves, key=lambda x: x[4], default=None)
            if best is None or best[4] >= -1e-9:
                break
            d, desc = self._apply_move(best)
            improvements += 1
            if record_refines:
                self._snapshot(
                    "refine",
                    list(best[1:5:3]) if best[0] == "swap" else [best[1]],
                    f"{desc}, 目标函数下降 {abs(d):.4g}, 残余不平衡量进一步减小。",
                )
        return improvements

    def _local_search(self, record: bool = False) -> None:
        """单个起点的局部搜索: 一次退火短链 + 爬山。"""
        free = [t for t in self.tubes if not t.locked]
        if len(free) < 2:
            self._hill_climb(record_refines=record)
            return
        self._anneal_chain(self._chain_steps)
        self._hill_climb(record_refines=record)

    # ------------------------------------------------------------------
    # 互斥试管隔离: 完全枚举精确搜索
    # 合法摆放要求每对互斥试管不同处物理环形相邻槽位(禁用槽仍占位)。
    # 搜索规模: ≤ 7 管、≤ 10 启用槽, 叶子数至多 P(10,7)=604800。
    # 评分沿用 w1·|U1|² + w2·|U2|² + move_weight·移动数(增量重排输入);
    # 平分时按管号升序的槽位序列(DFS 枚举顺序)裁决。
    # ------------------------------------------------------------------
    def _seed_exact(self, previous: list[dict[str, Any]]) -> list[int]:
        """增量重排: 返回参与精确枚举的全部自由管。

        为保证平分裁决与"全量枚举、管号升序槽位序列最小"一致, 不预先固定
        任何旧摆放; 移动罚权(move_weight)在精确枚举评分中自然偏好沿用,
        最优摆放确定后再按"与旧槽相同"统计实际保留的试管(见 _solve_exact)。
        """
        return sorted(t.id for t in self.tubes if not t.locked)

    # 评分平分判定容差(相对): 容差内视为同分, 按管号升序槽位序列裁决
    _EXACT_TIE_REL = 1e-9

    def _solve_exact(
        self, previous: Optional[list[dict[str, Any]]] = None
    ) -> dict[str, Any]:
        """互斥隔离精确求解主流程, 并录制可回放步骤。"""
        incremental = previous is not None
        self._adj = self._ring_adjacency()
        self._reset()
        self._place_locked(record=not incremental)
        if incremental:
            pending = self._seed_exact(previous)
        else:
            pending = [t.id for t in self.tubes if not t.locked]

        free_ids = sorted(pending)
        enabled_slots = sorted(s.index for s in self.slots if s.enabled)
        self._enabled_set = set(enabled_slots)

        # 初始禁用: 锁定搭档所在槽的物理邻槽
        forbidden: dict[int, set[int]] = {}
        for tid in free_ids:
            blocked = set()
            for p in self._mutex_partners.get(tid, ()):
                ps = self._loc.get(p)
                if ps is not None:
                    blocked |= self._adj[ps]
            if blocked:
                forbidden[tid] = blocked

        # 每支自由管的初始候选槽(去掉锁定占用槽与其锁定互斥搭档的
        # 启用物理邻槽; 邻槽中的禁用槽本就不在候选内)
        locked_occ = set(self._loc.values())
        self._cand_slots = {
            tid: [s for s in enabled_slots
                  if s not in locked_occ
                  and s not in (forbidden.get(tid, set()) & self._enabled_set)]
            for tid in free_ids
        }
        n_slots = len(self.slots)
        c1x = [[0.0] * n_slots for _ in free_ids]
        c1y = [[0.0] * n_slots for _ in free_ids]
        c2x = [[0.0] * n_slots for _ in free_ids]
        c2y = [[0.0] * n_slots for _ in free_ids]
        cands = []
        for k, tid in enumerate(free_ids):
            m = self._by_id[tid].mass
            for s in self._cand_slots[tid]:
                a = _d(self.slots[s].angle)
                c1x[k][s] = m * math.cos(a)
                c1y[k][s] = m * math.sin(a)
                c2x[k][s] = m * math.cos(2 * a)
                c2y[k][s] = m * math.sin(2 * a)
            cands.append(self._cand_slots[tid])
        # 互斥伙伴在 free_ids 中的下标(自由-自由对); 锁定搭档只体现在初始 banned
        partn_idx: list[list[int]] = [[] for _ in free_ids]
        idx_of = {tid: k for k, tid in enumerate(free_ids)}
        for k, tid in enumerate(free_ids):
            for p in self._mutex_partners.get(tid, ()):
                if p in idx_of:
                    partn_idx[k].append(idx_of[p])
        adj = self._adj
        enabled_set = self._enabled_set
        w1, w2, mw = self.w1, self.w2, self.move_weight
        orig = [self._origin.get(tid) for tid in free_ids]
        n_free = len(free_ids)
        # 每个自由管的初始禁止槽(锁定互斥搭档邻槽)
        banned0 = [sorted(forbidden.get(tid, set())) for tid in free_ids]
        occ_slot = [-1] * n_slots
        for tid, s in self._loc.items():   # _loc: 管号 → 槽位
            occ_slot[s] = tid
        # 运行期禁止槽集合(自由搭档放置后传播); 用 list+时间戳风格的集合
        banned = [set(b) for b in banned0]
        u1x0, u1y0 = self._u1.real, self._u1.imag
        u2x0, u2y0 = self._u2.real, self._u2.imag

        best = {"score": math.inf, "key": None, "assign": None, "count": 0, "leaves": 0}
        assign = [-1] * n_free

        def dfs(k, ux, uy, vx, vy, moved):
            if k == n_free:
                best["leaves"] += 1
                sc = w1 * (ux * ux + uy * uy) + w2 * (vx * vx + vy * vy) + mw * moved
                bs = best["score"]
                tol = self._EXACT_TIE_REL * max(1.0, abs(bs))
                if sc < bs - tol:
                    best["score"] = sc
                    best["key"] = tuple(assign)
                    best["assign"] = list(assign)
                    best["count"] = 1
                elif abs(sc - bs) <= tol:
                    key = tuple(assign)
                    if best["key"] is None or key < best["key"]:
                        best["score"] = min(bs, sc)
                        best["key"] = key
                        best["assign"] = list(assign)
                    best["count"] += 1
                return
            bk = banned[k]
            px = partn_idx[k]
            for s in cands[k]:
                if occ_slot[s] != -1 or s in bk:
                    continue
                ax, ay = c1x[k][s], c1y[k][s]
                # 传播禁止槽并记录触碰
                touched = []
                for pj in px:
                    pb = banned[pj]
                    for nb in adj[s]:
                        if nb in enabled_set and nb not in pb:
                            pb.add(nb)
                            touched.append((pj, nb))
                # 前向检查: 每个后续管至少一个候选槽
                feasible = True
                for j in range(k + 1, n_free):
                    pbj = banned[j]
                    for sj in cands[j]:
                        if occ_slot[sj] == -1 and sj != s and sj not in pbj:
                            break
                    else:
                        feasible = False
                        break
                if feasible:
                    occ_slot[s] = free_ids[k]
                    assign[k] = s
                    nux, nuy = ux + ax, uy + ay
                    nvx, nvy = vx + c2x[k][s], vy + c2y[k][s]
                    nmoved = moved + (1 if orig[k] is not None and orig[k] != s else 0)
                    dfs(k + 1, nux, nuy, nvx, nvy, nmoved)
                    assign[k] = -1
                    occ_slot[s] = -1
                for pj, nb in touched:
                    banned[pj].discard(nb)

        dfs(0, u1x0, u1y0, u2x0, u2y0, 0)
        if best["assign"] is None:
            raise ValueError(
                "不可行: 在互斥试管隔离约束(互斥对不得落在物理相邻槽位)下不存在合法摆放。"
            )
        evaluated = best["leaves"]
        best_loc = {
            free_ids[k]: best["assign"][k] for k in range(n_free)
        }

        # 以最优摆放重建状态(录制回放时同样逐管放置, 保证确定性)
        self._enc = {}
        self._loc = {}
        self._u1 = 0j
        self._u2 = 0j
        self._steps = []
        self._track = True
        # 锁定管先放; 非锁定管随后按管号升序逐一放置
        if incremental:
            prev_map = {int(x["tube_id"]): int(x["slot"]) for x in previous}
        else:
            prev_map = {}
        locked_ids = sorted(t.id for t in self.tubes if t.locked)
        for tid in locked_ids:
            self._place(tid, self._by_id[tid].slot)
        kept_ids = {
            tid for tid in free_ids
            if incremental and prev_map.get(tid) == best_loc[tid]
        }
        if incremental:
            self._snapshot(
                "init", None,
                f"载入 {len(locked_ids)} 支锁定试管; 精确枚举在互斥约束与移动罚权下"
                f"取得最优摆放, 其中 {len(kept_ids)} 支非锁定试管沿用上一方案槽位。",
            )
        else:
            n_lock = len(locked_ids)
            if n_lock:
                self._snapshot(
                    "init", None,
                    f"载入 {n_lock} 支锁定试管作为初始约束; "
                    f"对剩余 {len(free_ids)} 支试管精确枚举全部合法摆放。",
                )
            else:
                self._snapshot(
                    "init", None,
                    f"互斥隔离精确搜索: 对 {len(free_ids)} 支试管枚举全部合法摆放, "
                    f"互斥对不得落在物理相邻槽位。",
                )
        # 非锁定管按管号升序逐管放置, 每步一条可回放快照
        for tid in free_ids:
            slot = best_loc[tid]
            t = self._by_id[tid]
            partner_ids = sorted(self._mutex_partners.get(tid, ()))
            mtxt = (
                f", 与试管 {', '.join(map(str, partner_ids))} 互斥(已避开物理相邻槽位)"
                if partner_ids else ""
            )
            kept_txt = "沿用上一方案槽位; " if tid in kept_ids else ""
            self._place(tid, slot)
            self._snapshot(
                "exact", [tid],
                f"试管 {tid}({t.mass:g}g) {kept_txt}精确放入槽位 {slot}"
                f"({self.slots[slot].angle:g}°){mtxt}。",
            )
        self._snapshot(
            "refine", None,
            f"互斥隔离精确搜索完成: 共评估 {evaluated} 种合法摆放, "
            f"取得现有评分 w₁·|U₁|² + w₂·|U₂|²"
            + (f" + {self.move_weight:g}·移动数" if self.move_weight else "")
            + " 的最小值; 平分时已按管号升序的槽位序列裁决。",
        )

        adj_edges = {
            tuple(sorted((a, b)))
            for a, nbs in self._adj.items() for b in nbs
        }
        self._exact_info = {
            "mutex_pairs": [list(p) for p in self.mutex_pairs],
            "evaluated_arrangements": evaluated,
            "best_score": best['score'],
            "kept_tube_ids": sorted(kept_ids),
            "adjacent_slot_pairs": sorted(adj_edges),
        }
        return self._build_result("exact-mutex", incremental, previous)

    # ------------------------------------------------------------------
    # 步骤快照
    # ------------------------------------------------------------------
    def _snapshot(self, kind: str, refs: Optional[list[int]], reason: str) -> None:
        u1g_mm = self.radius_mm * self._u1
        u2g_mm = self.radius_mm * self._u2
        self._steps.append({
            "seq": len(self._steps),
            "kind": kind,
            "refs": refs or [],
            "reason": reason,
            "placements": self._placement_snapshot(),
            "vectors": {
                "u1_mag": abs(u1g_mm),
                "u1_angle": (math.degrees(math.atan2(u1g_mm.imag, u1g_mm.real)) % 360.0),
                "u2_mag": abs(u2g_mm),
                "u2_angle": (math.degrees(math.atan2(u2g_mm.imag, u2g_mm.real)) % 360.0),
            },
        })

    def _placement_snapshot(self) -> list[dict[str, Any]]:
        out = []
        for t in self.tubes:
            slot = self._loc.get(t.id)
            out.append({
                "tube_id": t.id,
                "mass": t.mass,
                "locked": t.locked,
                "slot": slot,
            })
        return out

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def solve(self, previous: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        self.validate()
        self._by_id = {t.id: t for t in self.tubes}
        # origin: 增量重排时各试管的原槽位
        self._origin = {}
        if previous:
            prev_map = {int(x["tube_id"]): int(x["slot"]) for x in previous}
            for t in self.tubes:
                self._origin[t.id] = prev_map.get(t.id) if not t.locked else None

        if self.mutex_pairs:
            # 含互斥对: 完全枚举精确求评分最小的合法摆放
            return self._solve_exact(previous=previous)

        free_cnt = len([t for t in self.tubes if not t.locked])
        greedy_variants = list(range(self.GREEDY_VARIANTS + 1))
        random_variants = list(range(self.GREEDY_VARIANTS + 1, self.restarts + 1))
        if free_cnt == 0:
            variants = [0]
        elif previous is not None:
            # 增量重排: 增量种子 + 少量贪心/随机起点对照
            variants = ["incremental"] + greedy_variants[1:] + random_variants
        else:
            variants = greedy_variants + random_variants

        best_score = math.inf
        best_state = None
        best_variant = None
        for v in variants:
            self._track = False
            self._construct(v, previous=previous if v == "incremental" else None)
            self._local_search()
            sc = self._score(self._u1, self._u2, self._loc)
            if sc < best_score - 1e-12:
                best_score, best_variant = sc, v
                best_state = self._state()

        # 复跑最佳变体(确定性 RNG)并记录可解释步骤
        self._track = True
        self._anneal_recorded = 0
        self._track_best = math.inf
        self._construct(best_variant, previous=previous if best_variant == "incremental" else None)
        self._track_best = self._score(self._u1, self._u2, self._loc)
        self._local_search(record=True)

        # 安全校验: 复跑结果应与最佳一致(确定性), 否则直接恢复最佳布局
        re_score = self._score(self._u1, self._u2, self._loc)
        if re_score > best_score + 1e-6:  # pragma: no cover - 理论上不发生
            self._restore(best_state)
            self._steps = []
            self._snapshot("init", None, "输出搜索得到的最优布局。")

        incremental = previous is not None
        return self._build_result(best_variant if best_variant is not None else 0,
                                  incremental, previous)

    # ------------------------------------------------------------------
    # 结果组装
    # ------------------------------------------------------------------
    def _build_result(self, variant, incremental: bool,
                      previous: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
        R = self.radius_mm
        u1v, u2v = R * self._u1, R * self._u2
        m_total = sum(t.mass for t in self.tubes)
        metrics = self._metrics(u1v, u2v, m_total)
        mutex_partners = self._mutex_partners

        placements = []
        moved_ids: list[int] = []
        prev_map = {}
        if previous:
            prev_map = {int(x["tube_id"]): int(x["slot"]) for x in previous}
        for t in sorted(self.tubes, key=lambda x: (self._loc[x.id], x.id)):
            slot = self._loc[t.id]
            moved = False
            if incremental and not t.locked and t.id in prev_map:
                moved = prev_map[t.id] != slot
                if moved:
                    moved_ids.append(t.id)
            placements.append({
                "tube_id": t.id,
                "mass": t.mass,
                "locked": t.locked,
                "slot": slot,
                "angle": self.slots[slot].angle if slot is not None else None,
                "previous_slot": prev_map.get(t.id) if incremental else None,
                "moved": moved,
                "mutex_with": sorted(mutex_partners.get(t.id, set())),
                "u1": self._tube_vector(t, slot, 1),
                "u2": self._tube_vector(t, slot, 2),
            })

        pairs, singletons = self._extract_pairs()
        slot_state = []
        for s in self.slots:
            tid = self._enc.get(s.index)
            slot_state.append({
                "slot": s.index,
                "angle": s.angle,
                "enabled": s.enabled,
                "tube_id": tid,
                "locked": (self._by_id[tid].locked if tid is not None else False),
            })

        result = {
            "config": {
                "slot_count": len(self.slots),
                "radius_mm": R,
                "rpm": self.rpm,
                "w1": self.w1,
                "w2": self.w2,
                "move_weight": self.move_weight,
                "variant": variant,
                "incremental": incremental,
                "mutex_pair_count": len(self.mutex_pairs),
            },
            "metrics": metrics,
            "placements": placements,
            "slot_state": slot_state,
            "pairs": pairs,
            "singletons": singletons,
            "summary": self._summary_text(metrics, pairs, singletons, incremental, moved_ids),
            "moved_tube_ids": moved_ids,
            "steps": self._steps,
            "total_tubes": len(self.tubes),
        }
        if self._exact_info is not None:
            result["exact_search"] = self._exact_info
        return result

    def _tube_vector(self, t: Tube, slot: Optional[int], order: int) -> Optional[dict[str, Any]]:
        if slot is None:
            return None
        ang = _d(self.slots[slot].angle) * order
        v = t.mass * self.radius_mm * complex(math.cos(ang), math.sin(ang))
        return {"x": v.real, "y": v.imag, "mag": abs(v)}

    def _metrics(self, u1v: complex, u2v: complex, m_total: float) -> dict[str, Any]:
        omega = TWO_PI * self.rpm / 60.0
        u1_mag, u2_mag = abs(u1v), abs(u2v)
        # u1v 单位 g·mm → kg·m 需 ×1e-6
        u1_si = u1v * 1e-6
        force = omega ** 2 * abs(u1_si)          # N
        torque = force * self.radius_mm / 1000.0  # N·m
        ecc = (u1_mag / m_total) if m_total else 0.0  # mm 等效偏心距
        return {
            "u1_mag_g_mm": u1_mag,
            "u1_angle_deg": math.degrees(math.atan2(u1v.imag, u1v.real)) % 360.0,
            "u1_x_g_mm": u1v.real,
            "u1_y_g_mm": u1v.imag,
            "u2_mag_g_mm": u2_mag,
            "u2_angle_deg": math.degrees(math.atan2(u2v.imag, u2v.real)) % 360.0,
            "u2_x_g_mm": u2v.real,
            "u2_y_g_mm": u2v.imag,
            "centrifugal_force_n": force,
            "resultant_moment_n_m": torque,
            "equivalent_eccentricity_um": ecc * 1000.0,
            "total_mass_g": m_total,
            "grade": self._grade(ecc),
            "rpm": self.rpm,
            "angular_velocity_rad_s": omega,
        }

    @staticmethod
    def _grade(ecc_mm: float) -> str:
        e_um = ecc_mm * 1000.0
        if e_um <= 1.0:
            return "A"
        if e_um <= 5.0:
            return "B"
        if e_um <= 20.0:
            return "C"
        return "D"

    # ------------------------------------------------------------------
    # 对径配对分析
    # ------------------------------------------------------------------
    def _extract_pairs(self):
        n = len(self.slots)
        occupied = {s for s in self._enc if self.slots[s].enabled}
        used: set[int] = set()
        pairs: list[dict[str, Any]] = []
        singletons: list[dict[str, Any]] = []

        # 为每个占用槽找“最佳对径搭档”(偏差最小), 贪心匹配
        while True:
            best = None  # (dev, a, b)
            for a in occupied - used:
                for b in occupied - used:
                    if b <= a:
                        continue
                    dev = opposite_deviation(self.slots[a].angle, self.slots[b].angle)
                    if best is None or dev < best[0]:
                        best = (dev, a, b)
            if best is None:
                break
            dev, a, b = best
            if dev > 30.0:
                break
            used.add(a)
            used.add(b)
            pairs.append(self._pair_info(a, b, dev))

        for a in sorted(occupied - used):
            singletons.append(self._singleton_info(a))
        pairs.sort(key=lambda p: (p["slot_a"], p["slot_b"]))
        singletons.sort(key=lambda x: x["slot"])
        return pairs, singletons

    def _pair_info(self, a: int, b: int, dev: float) -> dict[str, Any]:
        ta = self._by_id[self._enc[a]]
        tb = self._by_id[self._enc[b]]
        v1 = self._c1(a, ta.mass) + self._c1(b, tb.mass)
        v2 = self._c2(a, ta.mass) + self._c2(b, tb.mass)
        ideal_single = abs(self._c1(a, ta.mass)) + abs(self._c1(b, tb.mass))
        cancel_u1 = (1.0 - abs(v1) / ideal_single) * 100.0 if ideal_single else 100.0
        locked_pair = ta.locked and tb.locked
        if dev <= 0.5:
            geo_txt = f"槽位 {a} 与 {b} 严格对径({self.slots[a].angle:g}°/{self.slots[b].angle:g}°)"
        else:
            geo_txt = (f"槽位 {a} 与 {b} 夹角 {angle_between(self.slots[a].angle, self.slots[b].angle):.1f}°"
                       f", 偏离对径 {dev:.1f}°")
        dm = abs(ta.mass - tb.mass)
        if dm < 1e-9:
            mass_txt = "两支质量相等, 一阶离心力完全反向"
        else:
            mass_txt = f"两支质量相差 {dm:g} g, 残余由较重一侧主导"
        if dev <= 0.5 and dm < 1e-9:
            verdict = "理想对置: 一阶力严格对消"
            if abs(v2) < 1e-9:
                verdict += ", 二阶分量也为零"
        elif cancel_u1 >= 90:
            verdict = f"高质量配对: 一阶力对消约 {cancel_u1:.1f}%"
        else:
            verdict = f"几何受限配对: 一阶力对消约 {cancel_u1:.1f}%, 由整体布局补偿"
        lock_txt = "; 两支均为锁定试管, 布局在其约束下完成" if locked_pair else (
            "; 含锁定试管" if (ta.locked or tb.locked) else "")
        reason = (f"试管 {ta.id}({ta.mass:g}g)↔试管 {tb.id}({tb.mass:g}g): {geo_txt}; "
                  f"{mass_txt}。{verdict}{lock_txt}。")
        return {
            "slot_a": a, "slot_b": b,
            "tube_a": ta.id, "tube_b": tb.id,
            "mass_a": ta.mass, "mass_b": tb.mass,
            "locked_a": ta.locked, "locked_b": tb.locked,
            "opposite_deviation_deg": dev,
            "mass_diff_g": dm,
            "u1_residual_g_mm": self.radius_mm * abs(v1),
            "u2_residual_g_mm": self.radius_mm * abs(v2),
            "u1_cancel_pct": max(0.0, cancel_u1),
            "reason": reason,
        }

    def _singleton_info(self, a: int) -> dict[str, Any]:
        t = self._by_id[self._enc[a]]
        # 启用槽中最接近对径的槽
        best = None
        for s in self.slots:
            if not s.enabled or s.index == a:
                continue
            dev = opposite_deviation(self.slots[a].angle, s.angle)
            if best is None or dev < best[0]:
                best = (dev, s.index)
        occupied_opp = best is not None and best[1] in self._enc
        if best is None:
            opp_txt = "转子上没有可用于对置的槽位"
        elif occupied_opp:
            opp_txt = f"对径方向槽位 {best[1]} 已被占用"
        else:
            opp_txt = f"对径方向槽位 {best[1]} 空置(偏差 {best[0]:.1f}°), 由整体合力补偿"
        lock_txt = "该试管为锁定试管, 不能移动; " if t.locked else ""
        reason = (f"试管 {t.id}({t.mass:g}g) 位于槽位 {a}({self.slots[a].angle:g}°)无对径搭档: "
                  f"{lock_txt}{opp_txt}。")
        return {
            "slot": a,
            "tube_id": t.id,
            "mass": t.mass,
            "locked": t.locked,
            "nearest_opposite_slot": best[1] if best else None,
            "nearest_opposite_deviation_deg": best[0] if best else None,
            "reason": reason,
        }

    def _summary_text(self, metrics, pairs, singletons, incremental, moved_ids) -> str:
        lines = [
            f"共放置 {len(self.tubes)} 支试管, 总质量 {metrics['total_mass_g']:g} g。"
            f"一阶不平衡量 {metrics['u1_mag_g_mm']:.2f} g·mm"
            f"(方向 {metrics['u1_angle_deg']:.1f}°), "
            f"二阶不平衡量 {metrics['u2_mag_g_mm']:.2f} g·mm, "
            f"等效偏心 {metrics['equivalent_eccentricity_um']:.2f} μm, 评级 {metrics['grade']}。"
            f"在 {self.rpm:g} rpm 下离心合力约 {metrics['centrifugal_force_n']:.2f} N, "
            f"合力矩约 {metrics['resultant_moment_n_m']:.4f} N·m。",
            f"识别出 {len(pairs)} 个对径配对、{len(singletons)} 支无搭档试管。",
        ]
        if incremental:
            lines.append(
                f"增量重排: 共移动 {len(moved_ids)} 支非锁定试管"
                + (f"(编号 {', '.join(map(str, moved_ids))})。" if moved_ids else ", 原布局完全保留。")
            )
        if self.mutex_pairs:
            pairs_txt = "; ".join(f"{a}↔{b}" for a, b in self.mutex_pairs)
            n_eval = self._exact_info.get("evaluated_arrangements") if self._exact_info else None
            lines.append(
                f"互斥试管隔离: {len(self.mutex_pairs)} 对互斥试管({pairs_txt})均不落在物理相邻槽位; "
                f"对 {n_eval if n_eval is not None else 0} 种合法摆放做了精确完全枚举, "
                f"取现有评分最小值, 平分时按管号升序的槽位序列裁决。"
            )
        return " ".join(lines)


def _parse_mutex_pairs(raw: Any) -> list[tuple[int, int]]:
    """解析互斥对输入: 接受 [{"tube_a": a, "tube_b": b}, ...] 或 [[a, b], ...]。"""
    if raw is None:
        return []
    pairs: list[tuple[int, int]] = []
    for item in raw:
        if isinstance(item, dict):
            a = item.get("tube_a", item.get("a"))
            b = item.get("tube_b", item.get("b"))
        else:
            try:
                a, b = item[0], item[1]
            except (TypeError, IndexError):
                raise ValueError("互斥对必须是两个试管编号组成的一对。")
        if a is None or b is None:
            raise ValueError("互斥对必须同时给出两支试管的编号。")
        pairs.append((int(a), int(b)))
    return pairs


def run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """API 层入口: 解析 JSON 风格的请求并求解。"""
    slots = [
        Slot(
            index=int(s["index"]),
            angle=float(s.get("angle", 360.0 * int(s["index"]) / int(payload["slot_count"]))),
            enabled=bool(s.get("enabled", True)),
        )
        for s in payload["slots"]
    ]
    tubes = [
        Tube(
            id=int(t["id"]),
            mass=float(t["mass"]),
            locked=bool(t.get("locked", False)),
            slot=(int(t["slot"]) if t.get("slot") is not None else None),
        )
        for t in payload["tubes"]
    ]
    solver = Solver(
        slots=slots,
        tubes=tubes,
        radius_mm=float(payload.get("radius_mm", 100.0)),
        rpm=float(payload.get("rpm", 4000.0)),
        w1=float(payload.get("w1", 1.0)),
        w2=float(payload.get("w2", 0.000001)),
        move_weight=float(payload.get("move_weight", 0.0)),
        restarts=int(payload.get("restarts", 10)),
        chain_steps=int(payload.get("chain_steps", 2500)),
        seed=int(payload.get("seed", 20260923)),
        mutex_pairs=_parse_mutex_pairs(payload.get("mutex_pairs")),
    )
    previous = payload.get("previous")
    return solver.solve(previous=previous)
