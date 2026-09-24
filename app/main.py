"""离心机转子配平规划器 —— FastAPI 后端。"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .balancer import run_plan
from .batch import plan_batch_sequence
from .calibration import predict_single_weight_balancing
from .compare import compare_plans
from .evacuation import plan_slot_evacuation
from .loading import plan_loading_sequence
from .recheck import recheck_tube_loading
from .weighing import worst_case_u1

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("BALANCER_DB", os.path.join(BASE_DIR, "data", "balancer.db"))


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                parent_id INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created_at)")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class SlotIn(BaseModel):
    index: int
    angle: Optional[float] = None
    enabled: bool = True


class TubeIn(BaseModel):
    id: int
    mass: float = Field(gt=0)
    locked: bool = False
    slot: Optional[int] = None


class PreviousPlacement(BaseModel):
    tube_id: int
    slot: int


class MutexPairIn(BaseModel):
    # 一对互斥试管的两个编号: 二者不得落在按角度环形排序后相邻的物理槽位
    # (禁用槽仍占据相邻关系)。含互斥对的请求限至多 7 管、10 个启用槽、12 对。
    tube_a: int
    tube_b: int


class TubeErrorIn(BaseModel):
    tube_id: int
    # 非负称重质量误差上界(g); 不得超过该管标称质量, 由校核逻辑按方案校验
    mass_error_bound_g: float = Field(ge=0)


class WeighingCheckRequest(BaseModel):
    # 逐管非负质量误差上界; 未列出的试管按 0(标称质量无误差)处理
    tube_errors: list[TubeErrorIn] = Field(default_factory=list)
    # 一阶不平衡量限值(g·mm), 以最不利 |U₁| 判定
    u1_limit_g_mm: Optional[float] = Field(default=None, ge=0)


class VibrationCalibrationRequest(BaseModel):
    # 同一转速、同一测点/相位参考下的试重前振动读数
    # 注: 有限性/非负性由校准逻辑统一校验并返回 400(避免 pydantic 回显 NaN/Inf
    # 导致错误响应本身无法 JSON 序列化); 类型错误仍由 pydantic 返回 422
    initial_amplitude: float
    initial_phase_deg: float
    # 装入试重后的振动读数
    trial_amplitude: float
    trial_phase_deg: float
    # 试重质量(g)及其空槽; 空槽必须在方案中存在、启用且空闲
    trial_mass_g: float
    trial_slot: int
    # 若干可用单枚配重质量(g), 逐个空槽枚举预测; 空列表由 pydantic 拒绝,
    # 每项必须 > 0 且有限由校准逻辑校验
    available_masses: list[float] = Field(min_length=1)


class BatchSequenceRequest(BaseModel):
    # 参与批次换装的已保存方案 ID(2~10 条, 每条至多出现一次);
    # 执行次序由服务端精确优化, 与传入顺序无关
    plan_ids: list[int] = Field(min_length=2, max_length=10)


class LoadingSequenceRequest(BaseModel):
    # 装载阶段一阶不平衡量限值(g·mm), 可空; 非空时必须非负
    u1_limit_g_mm: Optional[float] = Field(default=None, ge=0)


class SlotEvacuationRequest(BaseModel):
    # 新失效槽位索引列表(原本必须启用), 至少 1 个、不重复
    failed_slots: list[int] = Field(min_length=1)
    # 一阶不平衡量限值(g·mm), 必填的非负数
    u1_limit_g_mm: float = Field(ge=0)
    # 最多可移动管数(0~3, 控制精确枚举的搜索规模)
    max_moves: int = Field(ge=0, le=3)


class RecheckReadingIn(BaseModel):
    # 实际有管的启用槽位与天平实测质量(g); 有限性与 0.01 g 读数精度
    # 由复核逻辑统一校验并返回 400(与振动校准一致, 避免 pydantic 回显 NaN/Inf)
    slot: int
    mass: float = Field(gt=0)


class TubeRecheckRequest(BaseModel):
    # 现场实测读数(1~12 份): 实际有管的启用槽位 + 实测质量(0.01 g 精度)
    readings: list[RecheckReadingIn] = Field(min_length=1, max_length=12)
    # 统一允许偏差(g), 必填的非负数; 只有质量差不超过该偏差的边上才允许匹配
    tolerance_g: float = Field(ge=0)


class PlanRequest(BaseModel):
    name: str = "未命名方案"
    slot_count: int = Field(default=12, ge=1, le=128)
    slots: list[SlotIn] = Field(default_factory=list)
    tubes: list[TubeIn] = Field(default_factory=list)
    radius_mm: float = Field(default=100.0, gt=0)
    rpm: float = Field(default=4000.0, ge=0)
    w1: float = 1.0
    w2: float = 0.000001
    move_weight: float = 0.0
    restarts: int = Field(default=10, ge=1, le=64)
    chain_steps: int = Field(default=2500, ge=100, le=60000)
    seed: int = 20260923
    previous: Optional[list[PreviousPlacement]] = None
    parent_id: Optional[int] = None
    # 互斥试管对(至多 12 对); 非空时后端改用精确完全枚举而非启发式搜索
    mutex_pairs: list[MutexPairIn] = Field(default_factory=list)
    save: bool = True


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="离心机转子配平规划器", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "defaults": {
            "slot_count": 12,
            "radius_mm": 100.0,
            "rpm": 4000.0,
            "w1": 1.0,
            "w2": 0.000001,
            "move_weight": 0.0,
            "restarts": 10,
            "chain_steps": 2500,
        },
        "physics": {
            "u1": "一阶不平衡量 ΣmR·e^(iθ), 单位 g·mm, 决定主轴离心合力",
            "u2": "二阶不平衡量 ΣmR·e^(i2θ), 单位 g·mm, 2 倍频残余",
            "force": "F = ω²·|U1|×1e-6, 单位 N",
            "moment": "M = F·R/1000, 单位 N·m",
            "grade": "等效偏心距评级: A≤1μm, B≤5μm, C≤20μm, D>20μm",
        },
    }


def _normalize_slots(req: PlanRequest) -> list[dict[str, Any]]:
    if req.slots:
        return [s.model_dump() for s in req.slots]
    return [
        {"index": i, "angle": 360.0 * i / req.slot_count, "enabled": True}
        for i in range(req.slot_count)
    ]


def _build_payload(req: PlanRequest) -> dict[str, Any]:
    return {
        "slot_count": req.slot_count,
        "slots": _normalize_slots(req),
        "tubes": [t.model_dump() for t in req.tubes],
        "radius_mm": req.radius_mm,
        "rpm": req.rpm,
        "w1": req.w1,
        "w2": req.w2,
        "move_weight": req.move_weight,
        "restarts": req.restarts,
        "chain_steps": req.chain_steps,
        "seed": req.seed,
        "previous": (
            [p.model_dump() for p in req.previous] if req.previous is not None else None
        ),
        "mutex_pairs": [p.model_dump() for p in req.mutex_pairs],
    }


@app.post("/api/plan")
def create_plan(req: PlanRequest) -> dict[str, Any]:
    payload = _build_payload(req)
    try:
        t0 = time.perf_counter()
        result = run_plan(payload)
        solve_ms = (time.perf_counter() - t0) * 1000.0
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["solve_time_ms"] = round(solve_ms, 1)
    result["incremental"] = req.previous is not None

    record: dict[str, Any] = {
        "name": req.name,
        "request": payload,
        "result": result,
    }
    if req.save:
        record["id"] = _save_plan(req.name, payload, result, req.parent_id)
    return record


def _save_plan(name: str, payload: dict[str, Any], result: dict[str, Any],
               parent_id: Optional[int]) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO plans (name, created_at, request_json, result_json,
                               metrics_json, parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                json.dumps(result["metrics"], ensure_ascii=False),
                parent_id,
            ),
        )
        return int(cur.lastrowid)


@app.get("/api/plans")
def list_plans(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, created_at, metrics_json, parent_id
            FROM plans ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        items.append({
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "parent_id": row["parent_id"],
            "u1_mag_g_mm": metrics["u1_mag_g_mm"],
            "u2_mag_g_mm": metrics["u2_mag_g_mm"],
            "grade": metrics["grade"],
            "centrifugal_force_n": metrics["centrifugal_force_n"],
        })
    return {"items": items}


def _load_plan_row(plan_id: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id} 不存在。")
    return row


def _row_to_plan(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "parent_id": row["parent_id"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
    }


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: int) -> dict[str, Any]:
    return _row_to_plan(_load_plan_row(plan_id))


@app.get("/api/plans/compare")
def compare_saved_plans(a: int, b: int) -> dict[str, Any]:
    """比较两条已保存方案: 按试管编号合并摆放、并列指标差值、标注参数差异。"""
    if a == b:
        raise HTTPException(status_code=400, detail="请选择两条不同的方案进行比较。")
    # 两次独立取数, 保证两个 404 信息各自准确
    plan_a = _row_to_plan(_load_plan_row(a))
    plan_b = _row_to_plan(_load_plan_row(b))
    return compare_plans(plan_a, plan_b)


@app.post("/api/plans/batch-sequence")
def batch_sequence(req: BatchSequenceRequest) -> dict[str, Any]:
    """批次换装顺序规划(只读): 以空转子为起点, 精确求总操作数最少的执行次序。

    仅接受转子几何(半径、逐槽角度与启用状态)完全相同的方案; 同编号试管在
    各方案中的质量必须一致, 否则拒绝(400)。每次切换先卸下所有不在下一方案
    原槽位的试管, 再装入下一方案所需试管, 卸下/装入各计一次操作。用
    Held-Karp 子集动态规划精确求最小总操作数, 同成本按方案 ID 序列升序
    裁决; 逐批返回卸装清单、单次与累计操作数。结果不保存、不修改原方案。
    """
    # 逐条取数, 保证不存在的方案各自返回准确的 404
    plans = [_row_to_plan(_load_plan_row(pid)) for pid in req.plan_ids]
    try:
        return plan_batch_sequence(plans)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/plan/{plan_id}")
def delete_plan(plan_id: int) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="方案不存在。")
    return {"deleted": plan_id}


@app.get("/api/plan/{plan_id}/export")
def export_plan(plan_id: int) -> JSONResponse:
    plan = get_plan(plan_id)
    fname = f"balance-plan-{plan_id}.json"
    return JSONResponse(
        plan,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/plan/{plan_id}/weighing-check")
def weighing_check(plan_id: int, req: WeighingCheckRequest) -> dict[str, Any]:
    """对已保存方案做称重误差校核(只读): 固定落槽与角度取自方案, 不覆盖原方案。

    各管实际质量在 [m-e, m+e] 内可独立正负变化, 服务端用二维 zonotope 顶点
    枚举精确求最不利 |U₁| 及一组对应实际质量(不枚举 2^n 组合, 也不把误差
    绝对值标量相加), 同时返回标称 |U₁| 作对照。
    """
    row = _load_plan_row(plan_id)
    plan = _row_to_plan(row)
    result = plan["result"]
    placements = result.get("placements", [])
    placed_ids = {
        int(p["tube_id"]) for p in placements if p.get("slot") is not None
    }

    errors: dict[int, float] = {}
    for item in req.tube_errors:
        if item.tube_id in errors:
            raise HTTPException(
                status_code=400,
                detail=f"试管 {item.tube_id} 的误差上界重复填写。",
            )
        if item.tube_id not in placed_ids:
            raise HTTPException(
                status_code=400,
                detail=f"试管 {item.tube_id} 不在方案 {plan_id} 的摆放明细中。",
            )
        errors[item.tube_id] = item.mass_error_bound_g

    radius_mm = float(result["config"]["radius_mm"])
    try:
        check = worst_case_u1(
            placements=placements,
            radius_mm=radius_mm,
            errors=errors,
            u1_limit_g_mm=req.u1_limit_g_mm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rpm = float(result["config"]["rpm"])
    omega = 2.0 * math.pi * rpm / 60.0
    check["rpm"] = rpm
    check["angular_velocity_rad_s"] = omega
    check["nominal"]["centrifugal_force_n"] = (
        omega ** 2 * check["nominal"]["u1_mag_g_mm"] * 1e-6
    )
    check["worst_case"]["centrifugal_force_n"] = (
        omega ** 2 * check["worst_case"]["u1_mag_g_mm"] * 1e-6
    )
    check["plan"] = {
        "id": plan["id"],
        "name": plan["name"],
        "created_at": plan["created_at"],
    }
    return check


@app.post("/api/plan/{plan_id}/vibration-calibration")
def vibration_calibration(plan_id: int, req: VibrationCalibrationRequest) -> dict[str, Any]:
    """对已保存方案做实测振动校准(只读): 影响系数法 + 单枚配重预测。

    槽位几何(角度/启用/占用)与半径均只读取自方案; 试重槽必须是方案中的
    空闲启用槽。由试重前/后同转速复数振动差与试重质径积求影响系数,
    再在每个空闲启用槽位上对每枚可用配重预测残余振动并按幅值排序。
    试重视为已取下, 结果不保存、不覆盖原方案。
    """
    row = _load_plan_row(plan_id)
    plan = _row_to_plan(row)
    result = plan["result"]
    radius_mm = float(result["config"]["radius_mm"])
    try:
        calib = predict_single_weight_balancing(
            slot_state=result.get("slot_state", []),
            radius_mm=radius_mm,
            initial_amplitude=req.initial_amplitude,
            initial_phase_deg=req.initial_phase_deg,
            trial_amplitude=req.trial_amplitude,
            trial_phase_deg=req.trial_phase_deg,
            trial_mass_g=req.trial_mass_g,
            trial_slot=req.trial_slot,
            available_masses=list(req.available_masses),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    calib["rpm"] = float(result["config"]["rpm"])
    calib["plan"] = {
        "id": plan["id"],
        "name": plan["name"],
        "created_at": plan["created_at"],
    }
    return calib


@app.post("/api/plan/{plan_id}/loading-sequence")
def loading_sequence(plan_id: int, req: LoadingSequenceRequest) -> dict[str, Any]:
    """对已保存方案规划逐管安全装载次序(只读): 空转子起步, 每步只装一支。

    各管最终槽位与方案完全一致; 按方案中的质量、槽角与半径计算各中间
    状态的 |U₁|, 用子集动态规划(2^n, n ≤ 16)精确求全过程峰值 |U₁| 最小
    的装载次序, 并列最优按试管编号序列升序裁决。可填写装载阶段限值,
    逐步返回装入管号、槽位、当步 |U₁|、累计峰值与超限步骤; 超过 16 支
    试管明确拒绝(400)。结果不保存、不覆盖原方案。
    """
    plan = _row_to_plan(_load_plan_row(plan_id))
    try:
        return plan_loading_sequence(plan, u1_limit_g_mm=req.u1_limit_g_mm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/plan/{plan_id}/slot-evacuation")
def slot_evacuation(plan_id: int, req: SlotEvacuationRequest) -> dict[str, Any]:
    """对已保存方案做失效槽位应急迁移规划(只读): 基于原质量、槽角与半径。

    锁定试管不得移动, 失效槽位不得占用; 在最多移动 max_moves(0~3) 支
    非锁定试管的上限内, 按移动管数分层完全枚举(管数 ≤ 10、启用槽 ≤ 16),
    精确寻找满足一阶不平衡量限值的摆放: 先使移动管数最少, 再使最终
    |U₁| 最小, 并列时按管号升序的目标槽位序列裁决。返回逐管原槽/目标槽、
    移动数与最终 |U₁|; 无可行摆放时明确说明, 并附移动上限内 |U₁| 最小
    的对照摆放。结果不保存、不覆盖原方案。
    """
    plan = _row_to_plan(_load_plan_row(plan_id))
    try:
        return plan_slot_evacuation(
            plan,
            failed_slots=list(req.failed_slots),
            u1_limit_g_mm=req.u1_limit_g_mm,
            max_moves=req.max_moves,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/plan/{plan_id}/tube-recheck")
def tube_recheck(plan_id: int, req: TubeRecheckRequest) -> dict[str, Any]:
    """对已保存方案做现场装管复核(只读): 实测读数与方案试管的一对一匹配。

    按启用槽录入实际有管槽位及天平实测质量(0.01 g 精度, 至多 12 份)与
    统一允许偏差; 服务端只在质量差不超过偏差的边上求一对一匹配, 用子集
    动态规划精确求总绝对质量差最小的最优匹配, 并对每个实测槽汇总所有
    最优匹配可能对应的试管: 唯一时按原方案槽位标为正确或错槽, 多解时
    标为身份不确定(不任取一解断言错放)。读数数量与方案管数不等或不
    存在完整匹配时明确说明原因。结果不保存、不覆盖原方案。
    """
    plan = _row_to_plan(_load_plan_row(plan_id))
    try:
        return recheck_tube_loading(
            plan,
            readings=[r.model_dump() for r in req.readings],
            tolerance_g=req.tolerance_g,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
