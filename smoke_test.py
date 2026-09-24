"""无第三方依赖环境下, 用最小桩模块驱动 app/main.py 的端点逻辑做端到端冒烟测试。"""
import sys, types, json, os, tempfile, asyncio

# ---- 桩: pydantic ----
pyd = types.ModuleType("pydantic")
class FieldInfo:
    def __init__(self, default=None, **kw):
        self.default = default; self.kw = kw
def Field(default=None, **kw):
    if default is None and "default_factory" in kw:
        return FieldInfo(default=kw["default_factory"](), **kw)
    return FieldInfo(default=default, **kw)
class BaseModel:
    def __init__(self, **data):
        cls = type(self)
        for name, f in cls.model_fields().items():
            val = data.get(name, f.default if not callable(getattr(f.default, "__call__", None)) else None)
            if val is None and isinstance(f.default, (list,)):
                val = list(f.default)
            setattr(self, name, val)
        self.__post_init_check__(data)
    def __post_init_check__(self, data):
        for k in data:
            if not hasattr(type(self), "_ann"): pass
    def model_dump(self):
        return {k: getattr(self, k) for k in type(self).model_fields()}
    @classmethod
    def model_fields(cls):
        return getattr(cls, "_fields", {})
    @classmethod
    def parse(cls, data):
        # 简易类型转换(数字/布尔/嵌套)
        return cls(**data)
def model_validate(cls, data):
    return _build(cls, data)
def _build(cls, data):
    obj = cls.__new__(cls)
    fields = cls.model_fields()
    for name, f in fields.items():
        val = data.get(name, _factory_default(f))
        ann = cls._annotations.get(name)
        val = _convert(val, ann)
        setattr(obj, name, val)
    return obj
def _factory_default(f):
    return f.default
def _convert(val, ann):
    if val is None: return None
    import typing

pyd.BaseModel = BaseModel
pyd.Field = Field

# 更健壮:重写 BaseModel 以处理嵌套 list[Model] 与 Optional
class BM:
    def __init__(self, **kw):
        for name, f in type(self).model_fields().items():
            setattr(self, name, kw.get(name, _default(f)))
        self._validate(kw)
    def _validate(self, kw):
        allowed = set(type(self).model_fields())
        for k in kw:
            if k not in allowed: raise ValueError(f"unexpected field {k}")
    def model_dump(self):
        out = {}
        for k, f in type(self).model_fields().items():
            v = getattr(self, k)
            if isinstance(v, list):
                out[k] = [x.model_dump() if hasattr(x, "model_dump") else x for x in v]
            elif hasattr(v, "model_dump"):
                out[k] = v.model_dump()
            else:
                out[k] = v
        return out
    @classmethod
    def model_fields(cls):
        return cls._fields
    @classmethod
    def validate(cls, data):
        return _build_model(cls, data)
def _default(f):
    d = f.default
    return list(d) if isinstance(d, list) else d
def _build_model(cls, data):
    obj = cls.__new__(cls)
    for name, f in cls.model_fields().items():
        raw = data.get(name, _default(f))
        setattr(obj, name, _coerce(raw, cls._annotations.get(name)))
    return obj
def _coerce(raw, ann):
    if raw is None: return None
    # 解析泛型
    origin = getattr(ann, "__origin__", None)
    if origin is list:
        inner = ann.__args__[0]
        if isinstance(inner, type) and issubclass(inner, BM):
            return [_build_model(inner, x) for x in raw]
        return list(raw)
    if isinstance(ann, type) and issubclass(ann, BM):
        return _build_model(ann, raw)
    return raw
pyd.BaseModel = BM

# ---- 桩: fastapi ----
fa = types.ModuleType("fastapi")
fa.__path__ = []
class FileResponse:
    def __init__(self, path): self.path = path
class JSONResponse:
    def __init__(self, content, headers=None):
        self.content = content; self.headers = headers or {}
far = types.ModuleType("fastapi.responses")
far.FileResponse = FileResponse
far.JSONResponse = JSONResponse
fa.responses = far
class HTTPException(Exception):
    def __init__(self, status_code=400, detail=""):
        self.status_code=status_code; self.detail=detail
class _RouterLike:
    def __init__(self): self.routes=[]
    def add(self, method, path, fn): self.routes.append((method, path, fn))
    def get(self,p,*a,**k):
        def d(f): self.add("GET",p,f); return f
        return d
    def post(self,p,*a,**k):
        def d(f): self.add("POST",p,f); return f
        return d
    def delete(self,p,*a,**k):
        def d(f): self.add("DELETE",p,f); return f
        return d
class FastAPI(_RouterLike):
    def __init__(self, title="", version=""):
        super().__init__(); self.title=title; self.startup=[]
    def on_event(self, ev):
        def d(f):
            if ev=="startup": self.startup.append(f)
            return f
        return d
    def mount(self,*a,**k): pass
fa.FastAPI=FastAPI; fa.HTTPException=HTTPException

st=types.ModuleType("fastapi.staticfiles")
class StaticFiles:
    def __init__(self,directory=None): self.directory=directory
st.StaticFiles=StaticFiles

sys.modules["pydantic"]=pyd
sys.modules["fastapi"]=fa
sys.modules["fastapi.responses"]=far
sys.modules["fastapi.staticfiles"]=st

# ---- 导入被测应用 ----
tmpdir = tempfile.mkdtemp()
os.environ["BALANCER_DB"] = os.path.join(tmpdir, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import main  # noqa
from app.main import app

for f in app.startup: f()

def find(method, path):
    import re
    path = path.split("?", 1)[0]
    for m,p,fn in app.routes:
        if m!=method: continue
        # 把 {param} 转为单段正则([^/]+)
        pat = "^" + re.sub(r"\{[^}]+\}", r"([^/]+)", p) + "$"
        mmx = re.match(pat, path)
        if mmx:
            args = []
            for a in mmx.groups():
                args.append(int(a) if a.isdigit() else a)
            return fn, tuple(args)
    return None

def call(method, path, body=None):
    raw_query = path.split("?", 1)[1] if "?" in path else ""
    r = find(method,path)
    if isinstance(r, tuple): fn, args = r
    else: fn, args = r, ()
    if raw_query:
        import urllib.parse
        qargs = {}
        for k, v in urllib.parse.parse_qsl(raw_query):
            qargs[k] = int(v) if v.lstrip("-").isdigit() else v
        args = tuple(qargs.get(p) for p in fn.__code__.co_varnames[:fn.__code__.co_argcount])
    if body is not None:
        model_cls = main.PlanRequest
        req = _build_concrete(model_cls, body)
        return fn(req)
    return fn(*args)

def _build_concrete(cls, data):
    """根据 main.py 中具体 Pydantic 模型做嵌套转换。"""
    def slot(d):
        o = types.SimpleNamespace(**d)
        o.model_dump = lambda d=d: dict(d)
        return o
    def tube(d):
        o = types.SimpleNamespace(
            id=int(d["id"]), mass=float(d["mass"]),
            locked=bool(d.get("locked", False)),
            slot=(int(d["slot"]) if d.get("slot") is not None else None))
        o.model_dump = lambda: {"id": o.id, "mass": o.mass, "locked": o.locked, "slot": o.slot}
        return o
    def prev(d):
        o = types.SimpleNamespace(tube_id=int(d["tube_id"]), slot=int(d["slot"]))
        o.model_dump = lambda: {"tube_id": o.tube_id, "slot": o.slot}
        return o
    def mpair(d):
        if isinstance(d, dict):
            a = d.get("tube_a", d.get("a")); b = d.get("tube_b", d.get("b"))
        else:
            a, b = d[0], d[1]
        o = types.SimpleNamespace(tube_a=int(a), tube_b=int(b))
        o.model_dump = lambda: {"tube_a": o.tube_a, "tube_b": o.tube_b}
        return o
    defaults = {
        "name":"未命名方案","slot_count":12,"slots":[],
        "radius_mm":100.0,"rpm":4000.0,"w1":1.0,"w2":0.000001,
        "move_weight":0.0,"restarts":10,"chain_steps":2500,
        "seed":20260923,"previous":None,"parent_id":None,
        "mutex_pairs":[],"save":True,
    }
    merged = {**defaults, **data}
    return types.SimpleNamespace(
        name=merged["name"],
        slot_count=int(merged["slot_count"]),
        slots=[slot(d) for d in merged["slots"]],
        tubes=[tube(d) for d in merged["tubes"]],
        radius_mm=float(merged["radius_mm"]), rpm=float(merged["rpm"]),
        w1=float(merged["w1"]), w2=float(merged["w2"]),
        move_weight=float(merged["move_weight"]),
        restarts=int(merged["restarts"]), chain_steps=int(merged["chain_steps"]),
        seed=int(merged["seed"]),
        previous=([prev(d) for d in merged["previous"]] if merged["previous"] is not None else None),
        mutex_pairs=[mpair(d) for d in merged["mutex_pairs"]],
        parent_id=merged["parent_id"], save=bool(merged.get("save", True)),
    )

# ---- 用例 ----
slots=[{"index":i,"angle":i*30,"enabled": i!=6} for i in range(12)]
req_body={"name":"冒烟方案","slot_count":12,"slots":slots,
          "tubes":[{"id":1,"mass":50},{"id":2,"mass":50},{"id":3,"mass":51},
                   {"id":4,"mass":49},{"id":5,"mass":30,"locked":True,"slot":0},
                   {"id":6,"mass":30}],
          "radius_mm":100,"rpm":4000,"restarts":4,"chain_steps":800}
rec = call("POST","/api/plan",req_body)
assert rec["id"]==1, rec.keys()
m=rec["result"]["metrics"]
print("plan#1 U1=%.2f U2=%.2f grade=%s F=%.3f M=%.4f steps=%d" % (
    m["u1_mag_g_mm"],m["u2_mag_g_mm"],m["grade"],
    m["centrifugal_force_n"],m["resultant_moment_n_m"],len(rec["result"]["steps"])))
# locked respected
assert next(p for p in rec["result"]["placements"] if p["tube_id"]==5)["slot"]==0
# disabled respected
assert all(p["slot"]!=6 for p in rec["result"]["placements"])

# 400 validation
bad=dict(req_body); bad["tubes"]=bad["tubes"]*4
try:
    call("POST","/api/plan",bad); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code==400; print("validation 400 OK:",e.detail)

# 增量重排
inc=dict(req_body); inc["name"]="增量方案"
inc["tubes"]=[dict(t) for t in req_body["tubes"]]+[{"id":7,"mass":20}]
inc["previous"]=[{"tube_id":p["tube_id"],"slot":p["slot"]} for p in rec["result"]["placements"]]
inc["parent_id"]=1
rec2=call("POST","/api/plan",inc)
print("plan#2 incremental U1=%.2f moved=%s" % (
    rec2["result"]["metrics"]["u1_mag_g_mm"], rec2["result"]["moved_tube_ids"]))

# 方案比较
cmp12 = call("GET", "/api/plans/compare?a=1&b=2")
assert cmp12["plan_a"]["id"] == 1 and cmp12["plan_b"]["id"] == 2
# plan#2 比 plan#1 多一支试管 -> 有 1 条 added, 无 removed
assert cmp12["placement_counts"]["added"] == 1, cmp12["placement_counts"]
added = [r for r in cmp12["placements"] if r["status"] == "added"]
assert added and added[0]["tube_id"] == 7
# 试管清单不同 -> 非同条件, 必须在参数差异中体现
assert cmp12["same_conditions"] is False
assert "tubes" in cmp12["differing_groups"]
# 指标并列含一阶/二阶模值与角度, 差值为 B-A
um = {x["key"]: x for x in cmp12["metrics"]}
assert set(("u1_mag_g_mm", "u2_mag_g_mm", "u1_angle_deg", "u2_angle_deg")) <= set(um)
assert abs(um["u1_mag_g_mm"]["delta_b_minus_a"]
           - (rec2["result"]["metrics"]["u1_mag_g_mm"]
              - rec["result"]["metrics"]["u1_mag_g_mm"])) < 1e-6
print("compare added/diff-condition OK")

# 同条件比较: 用相同输入再求解保存一份(plan#3)
same = dict(req_body); same["name"] = "同条件副本"
rec3 = call("POST", "/api/plan", same)
cmp13 = call("GET", "/api/plans/compare?a=1&b=3")
assert cmp13["same_conditions"] is True, cmp13["differing_groups"]
assert not cmp13["input_diff"]["rotor"]
assert not cmp13["input_diff"]["solver"]
assert not cmp13["input_diff"]["tubes"]
assert all(r["status"] == "unchanged" for r in cmp13["placements"])
assert all(abs(r["delta_b_minus_a"]) < 1e-9
           for r in cmp13["metrics"] if not r["is_angle"])
print("compare same-conditions OK")

# a==b 拒绝; 不存在 404
try:
    call("GET", "/api/plans/compare?a=1&b=1"); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code == 400
try:
    call("GET", "/api/plans/compare?a=1&b=999"); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
print("compare validation OK")

# ---- 称重误差校核(只读) ----
import math as _math
def wc_route(plan_id):
    found = find("POST", f"/api/plan/{plan_id}/weighing-check")
    return (found if isinstance(found, tuple) else (found, ()))
def wc_call(plan_id, errors, limit=None):
    fn, args = wc_route(plan_id)
    req = types.SimpleNamespace(
        tube_errors=[types.SimpleNamespace(tube_id=int(t),
                                          mass_error_bound_g=float(e))
                     for t, e in errors],
        u1_limit_g_mm=limit,
    )
    return fn(*args, req)

nom_u1 = rec["result"]["metrics"]["u1_mag_g_mm"]
masses = {p["tube_id"]: p["mass"] for p in rec["result"]["placements"]}
errs = [(tid, min(0.5, m)) for tid, m in masses.items()]
wc = wc_call(1, errs)
# 标称值必须与原方案指标一致(固定落槽/角度)
assert abs(wc["nominal"]["u1_mag_g_mm"] - nom_u1) < 1e-6, wc["nominal"]
# 最不利值不小于标称值; 实际质量落在 [m-e, m+e] 且向量复算与最不利值一致
assert wc["worst_case"]["u1_mag_g_mm"] + 1e-7 >= nom_u1
ux = uy = 0.0
for row in wc["tubes"]:
    m, e = masses[row["tube_id"]], row["mass_error_bound_g"]
    assert m - e - 1e-9 <= row["actual_mass_g"] <= m + e + 1e-9
    a = _math.radians(row["angle_deg"])
    ux += row["actual_mass_g"] * _math.cos(a)
    uy += row["actual_mass_g"] * _math.sin(a)
recomputed = 100.0 * _math.hypot(ux, uy)
assert abs(recomputed - wc["worst_case"]["u1_mag_g_mm"]) < 1e-5, (
    recomputed, wc["worst_case"]["u1_mag_g_mm"])
# 用 2^n 全组合枚举交叉验证最不利值
placed = [p for p in rec["result"]["placements"]]
emap = dict(errs)
brute = 0.0
for mask in range(1 << len(placed)):
    bx = by = 0.0
    for i, p in enumerate(placed):
        s = 1.0 if (mask >> i) & 1 else -1.0
        mm = p["mass"] + s * emap[p["tube_id"]]
        a = _math.radians(p["angle"])
        bx += mm * _math.cos(a); by += mm * _math.sin(a)
    brute = max(brute, 100.0 * _math.hypot(bx, by))
assert abs(brute - wc["worst_case"]["u1_mag_g_mm"]) < 1e-5, (brute, wc["worst_case"])
# 限值判定: 取最不利值+1 合格 / -1 超限 / 0 限值必超限(标称非零)
wc_ok = wc_call(1, errs, wc["worst_case"]["u1_mag_g_mm"] + 1.0)
assert wc_ok["within_limit"] is True and wc_ok["excess_g_mm"] == 0.0
wc_bad = wc_call(1, errs, max(0.0, wc["worst_case"]["u1_mag_g_mm"] - 1.0))
assert wc_bad["within_limit"] is False
assert abs(wc_bad["excess_g_mm"] - 1.0) < 1e-6
wc_nolim = wc_call(1, errs)
assert wc_nolim["within_limit"] is None and wc_nolim["excess_g_mm"] is None
# 缺省(全部误差为 0): 最不利 == 标称
wc_zero = wc_call(1, [])
assert abs(wc_zero["worst_case"]["u1_mag_g_mm"] - nom_u1) < 1e-7
print("weighing-check worst=%.3f nominal=%.3f brute=%.3f naive=%.3f OK" % (
    wc["worst_case"]["u1_mag_g_mm"], nom_u1, brute, wc["naive_abs_worst_g_mm"]))

# 校验错误: 误差超标称质量 / 试管不存在 / 重复填写 -> 400
tid0, m0 = next(iter(masses.items()))
for bad_body in (
    [(tid0, m0 + 0.01)],        # 超过标称质量
    [(99999, 0.1)],             # 方案中不存在的试管
    [(tid0, 0.1), (tid0, 0.2)],# 重复填写
):
    try:
        wc_call(1, bad_body); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code == 400, e
# 方案不存在 -> 404
try:
    wc_call(999, [(1, 0.1)]); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
# 只读: 校核后原方案指标不变
assert call("GET", "/api/plan/1")["result"]["metrics"]["u1_mag_g_mm"] == nom_u1
print("weighing-check validation/read-only OK")

# ---- 实测振动校准(只读, 影响系数法) ----
def vc_route(plan_id):
    found = find("POST", f"/api/plan/{plan_id}/vibration-calibration")
    return (found if isinstance(found, tuple) else (found, ()))
def vc_call(plan_id, body):
    fn, args = vc_route(plan_id)
    req = types.SimpleNamespace(**body)
    return fn(*args, req)

disabled = {s["index"] for s in req_body["slots"] if not s["enabled"]}
occupied = {p["slot"] for p in rec["result"]["placements"]}
vc_trial = next(i for i in range(12) if i not in occupied and i not in disabled)
# 合成线性系统: V0=5∠30°, α=1e-4∠60° (振动单位/g·mm)
_v0 = 5.0 * complex(_math.cos(_math.radians(30)), _math.sin(_math.radians(30)))
_alpha = 1e-4 * complex(_math.cos(_math.radians(60)), _math.sin(_math.radians(60)))
_ut = 5.0 * 100.0 * complex(
    _math.cos(_math.radians(30 * vc_trial)),
    _math.sin(_math.radians(30 * vc_trial)))
_v1 = _v0 + _alpha * _ut
vc_body = {
    "initial_amplitude": 5.0, "initial_phase_deg": 30.0,
    "trial_amplitude": abs(_v1),
    "trial_phase_deg": _math.degrees(_math.atan2(_v1.imag, _v1.real)) % 360.0,
    "trial_mass_g": 5.0, "trial_slot": vc_trial,
    "available_masses": [2.0, 5.0, 8.0],
}
vc = vc_call(1, vc_body)
_got = complex(vc["influence_coefficient"]["x"], vc["influence_coefficient"]["y"])
assert abs(_got - _alpha) < 1e-15, _got
# 试重槽必须空闲启用
free_vc = {f["slot"] for f in vc["free_enabled_slots"]}
assert vc_trial in free_vc and occupied.isdisjoint(free_vc)
# 候选按残余幅值升序, 首项即推荐
vc_amps = [c["predicted"]["amplitude"] for c in vc["candidates"]]
assert vc_amps == sorted(vc_amps)
assert vc["best"]["rank"] == 1 and vc["candidate_count"] == len(free_vc) * 3
# 独立复算首项预测向量: V0 + α·m·R·e^{iθ}
_b = vc["best"]
_u = _b["mass_g"] * 100.0 * complex(
    _math.cos(_math.radians(_b["angle_deg"])),
    _math.sin(_math.radians(_b["angle_deg"])))
_pred = _v0 + _alpha * _u
assert abs(_b["predicted"]["amplitude"] - abs(_pred)) < 1e-9
assert abs(_b["improvement"] - (5.0 - abs(_pred))) < 1e-9
# 试重已取下: 同质量配重放回试重槽, 预测应等于装入试重时读数 V1
_same = next(c for c in vc["candidates"]
             if c["slot"] == vc_trial and c["mass_g"] == 5.0)
assert abs(_same["predicted"]["amplitude"] - abs(_v1)) < 1e-9
# 校验错误: 试重槽已占用 / 不存在; 读数无变化(影响系数近零) -> 400
for bad_vc in (
    {**vc_body, "trial_slot": next(iter(occupied))},
    {**vc_body, "trial_slot": 99},
    {**vc_body, "trial_amplitude": 5.0, "trial_phase_deg": 30.0},
):
    try:
        vc_call(1, bad_vc); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code == 400, e
# 方案不存在 -> 404
try:
    vc_call(999, vc_body); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
# 只读: 校准后原方案指标不变
assert call("GET", "/api/plan/1")["result"]["metrics"]["u1_mag_g_mm"] == nom_u1
print("vibration-calibration best slot=%s mass=%s residual=%.4f OK" % (
    vc["best"]["slot"], vc["best"]["mass_g"], vc["best"]["predicted"]["amplitude"]))


# 列表 / 详情 / 导出 / 删除
lst=call("GET","/api/plans")
assert len(lst["items"])==3
detail=call("GET","/api/plan/2")
assert detail["result"]["total_tubes"]==7
exp=call("GET","/api/plan/2/export")
assert json.loads(json.dumps(exp.content))["id"]==2
d=call("DELETE","/api/plan/1")
assert d["deleted"]==1
try:
    call("GET","/api/plan/1"); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code==404
print("list/detail/export/delete OK")

# health/config
assert call("GET","/api/health")["status"]=="ok"
assert "physics" in call("GET","/api/config")
print("health/config OK")

# 纯等量完美平衡
perf=dict(req_body); perf["tubes"]=[{"id":i,"mass":50} for i in range(1,5)]
perf["slots"]=[{"index":i,"angle":i*30,"enabled":True} for i in range(12)]
rp=call("POST","/api/plan",perf)
assert rp["result"]["metrics"]["u1_mag_g_mm"]<1e-6
print("perfect balance U1=0 OK")

# ---- 批次换装顺序(只读, Held-Karp 精确求序) ----
import itertools as _it

def bs_call(ids):
    found = find("POST", "/api/plans/batch-sequence")
    fn, args = (found if isinstance(found, tuple) else (found, ()))
    return fn(*args, types.SimpleNamespace(plan_ids=list(ids)))

def _occ_map(plan):
    return {int(p["slot"]): int(p["tube_id"])
            for p in plan["result"]["placements"] if p["slot"] is not None}

def _brute_batch(plans):
    """全排列参考: (最小总操作数, 升序裁决后的方案 ID 序列)。"""
    occ=[_occ_map(p) for p in plans]
    ids=[p["id"] for p in plans]
    best=None; seq=None
    for perm in _it.permutations(range(len(plans))):
        total=0; prev={}
        for i in perm:
            cur=occ[i]
            keep=sum(1 for s,t in prev.items() if cur.get(s)==t)
            total+=len(prev)+len(cur)-2*keep
            prev=cur
        s=tuple(ids[i] for i in perm)
        if best is None or total<best:
            best,seq=total,s
        elif total==best:
            seq=min(seq,s)
    return best,list(seq)

def _check_batch(ids):
    plans=[call("GET",f"/api/plan/{i}") for i in ids]
    r=bs_call(ids)
    best,seq=_brute_batch(plans)
    assert r["total_operations"]==best,(r["total_operations"],best)
    assert r["order"]==seq,(r["order"],seq)
    assert r["plan_count"]==len(ids)
    assert r["batches"][0]["unload"]==[]          # 空转子起步
    assert r["batches"][-1]["cumulative_operations"]==best
    assert sum(b["operations"] for b in r["batches"])==best
    cum=0
    for b in r["batches"]:
        assert b["operations"]==b["unload_count"]+b["load_count"]
        assert b["unload_count"]==len(b["unload"])
        assert b["load_count"]==len(b["load"])
        cum+=b["operations"]
        assert b["cumulative_operations"]==cum
    return r

# plan#2/#3 同几何(槽6禁用, R=100); 再存一条同几何不同布局的方案
b5=dict(req_body); b5["name"]="批次第三方案"; b5["seed"]=777
rec5=call("POST","/api/plan",b5)
m2=call("GET","/api/plan/2")["result"]["metrics"]["u1_mag_g_mm"]
bs2=_check_batch([3,2])            # 传入顺序打乱, 结果应与暴力枚举一致
bs3=_check_batch([rec5["id"],3,2])
print("batch-sequence [2,3] total=%d order=%s; [2,3,%d] total=%d order=%s OK" % (
    bs2["total_operations"],bs2["order"],rec5["id"],
    bs3["total_operations"],bs3["order"]))

# 拒绝: 几何不一致(plan#4 全槽启用) / 数量不足 / 重复 ID
for bad_ids in ([2,4],[2],[2,2],[2,3,2]):
    try:
        bs_call(bad_ids); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code==400,(bad_ids,e)
# 方案不存在 -> 404
try:
    bs_call([2,999]); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code==404
# 同编号试管质量不一致 -> 400
b6=dict(req_body); b6["name"]="质量冲突方案"
b6["tubes"]=[dict(t) for t in req_body["tubes"]]
b6["tubes"][0]["mass"]=99.0       # 试管 1 原 50 g
rec6=call("POST","/api/plan",b6)
try:
    bs_call([2,rec6["id"]]); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code==400 and "质量" in str(e.detail)
# 只读: 规划后原方案指标不变
assert call("GET","/api/plan/2")["result"]["metrics"]["u1_mag_g_mm"]==m2
print("batch-sequence validation/read-only OK")

# ---- 逐管安全装载(只读, 子集 DP 精确求最小峰值次序) ----
def ls_call(plan_id, limit="KEEP"):
    found = find("POST", f"/api/plan/{plan_id}/loading-sequence")
    fn, args = (found if isinstance(found, tuple) else (found, ()))
    req = types.SimpleNamespace(
        u1_limit_g_mm=(None if limit == "KEEP" else limit))
    return fn(*args, req)

def _brute_loading(plan):
    """全排列参考: (最小峰值, 升序裁决后的试管编号序列, 最优次序条数)。"""
    res = plan["result"]; R = res["config"]["radius_mm"]
    vec = {}
    for p in res["placements"]:
        if p["slot"] is None: continue
        a = _math.radians(p["angle"])
        vec[p["tube_id"]] = R * p["mass"] * complex(
            _math.cos(a), _math.sin(a))
    ids = sorted(vec)
    best = None; seq = None; cnt = 0
    for perm in _it.permutations(ids):
        ux = uy = 0.0; pk = 0.0
        for tid in perm:
            v = vec[tid]; ux += v.real; uy += v.imag
            pk = max(pk, _math.hypot(ux, uy))
        if best is None or pk < best - 1e-9 * max(1.0, best):
            best, seq, cnt = pk, perm, 1
        elif pk <= best + 1e-9 * max(1.0, best):
            cnt += 1; seq = min(seq, perm)
    return best, list(seq), cnt

# plan#2 共 7 支试管, 与全排列暴力枚举对照
plan2 = call("GET", "/api/plan/2")
ls = ls_call(2)
b_best, b_seq, b_cnt = _brute_loading(plan2)
assert ls["tube_count"] == 7 and len(ls["steps"]) == 7
assert abs(ls["peak_u1_g_mm"] - b_best) < 1e-6 * max(1.0, b_best), (ls["peak_u1_g_mm"], b_best)
assert ls["order"] == b_seq, (ls["order"], b_seq)
assert ls["optimal_order_count"] == b_cnt, (ls["optimal_order_count"], b_cnt)
assert ls["tie_broken"] == (b_cnt > 1)
# 逐步: 每步恰装一支且最终槽位与方案一致; 累计峰值单调, 末值=最小峰值
plan_slots = {p["tube_id"]: p["slot"] for p in plan2["result"]["placements"]}
assert [s["tube_id"] for s in ls["steps"]] == b_seq
assert all(s["slot"] == plan_slots[s["tube_id"]] for s in ls["steps"])
peaks = [s["peak_so_far_g_mm"] for s in ls["steps"]]
assert peaks == sorted(peaks)
assert abs(peaks[-1] - ls["peak_u1_g_mm"]) < 1e-9
# 最终 |U₁| 与方案标称一致
assert abs(ls["steps"][-1]["u1_mag_g_mm"] - m2) < 1e-6
assert abs(ls["final_u1_g_mm"] - m2) < 1e-6
# 限值判定: 峰值处合格 / 峰值-1 超限且列出超限步 / 不填不判定
ls_ok = ls_call(2, ls["peak_u1_g_mm"])
assert ls_ok["within_limit"] is True and ls_ok["exceeded_steps"] == []
assert all(s["exceeds_limit"] is False for s in ls_ok["steps"])
ls_bad = ls_call(2, max(0.0, ls["peak_u1_g_mm"] - 1.0))
assert ls_bad["within_limit"] is False and ls_bad["exceeded_steps"]
assert ls_bad["exceeded_count"] == len(ls_bad["exceeded_steps"])
hit = [s["seq"] for s in ls_bad["steps"] if s["exceeds_limit"]]
assert hit == ls_bad["exceeded_steps"]
ls_nl = ls_call(2)
assert ls_nl["within_limit"] is None and ls_nl["u1_limit_g_mm"] is None
assert all(s["exceeds_limit"] is None for s in ls_nl["steps"])
print("loading-sequence peak=%.3f order=%s count=%d OK" % (
    ls["peak_u1_g_mm"], ls["order"], ls["optimal_order_count"]))

# 方案不存在 -> 404; 负限值 -> 400
try:
    ls_call(999); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
try:
    ls_call(2, -1.0); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code == 400
# 超过 16 支 -> 明确拒绝(400)
big = dict(req_body); big["name"] = "17支方案"
big["slot_count"] = 24
big["slots"] = [{"index": i, "angle": i * 15.0, "enabled": True} for i in range(24)]
big["tubes"] = [{"id": i + 1, "mass": 10.0 + i} for i in range(17)]
recb = call("POST", "/api/plan", big)
try:
    ls_call(recb["id"]); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code == 400 and "16" in str(e.detail), e.detail
# 只读: 规划后原方案指标不变
assert call("GET", "/api/plan/2")["result"]["metrics"]["u1_mag_g_mm"] == m2
print("loading-sequence validation/read-only OK")

# ---- 失效槽位应急迁移(只读, 按移动数分层完全枚举) ----
def ev_route(plan_id):
    found = find("POST", f"/api/plan/{plan_id}/slot-evacuation")
    return (found if isinstance(found, tuple) else (found, ()))
def ev_call(plan_id, failed, limit, max_moves):
    fn, args = ev_route(plan_id)
    req = types.SimpleNamespace(failed_slots=list(failed),
                                u1_limit_g_mm=limit, max_moves=max_moves)
    return fn(*args, req)

# 确定性方案 A: 两支 50g 全部锁定在对径槽 0/6, 原 |U₁|=0
ev_body = dict(req_body)
ev_body["name"] = "应急迁移方案"
ev_body["slots"] = [{"index": i, "angle": i * 30, "enabled": True}
                    for i in range(12)]
ev_body["tubes"] = [
    {"id": 1, "mass": 50, "locked": True, "slot": 0},
    {"id": 2, "mass": 50, "locked": True, "slot": 6},
]
recv = call("POST", "/api/plan", ev_body)
evid = recv["id"]
assert abs(recv["result"]["metrics"]["u1_mag_g_mm"]) < 1e-6
# 空失效槽 + 限值 0 → 0 移动可行, 最终 |U₁|=0
ev0 = ev_call(evid, [3], 0.0, 0)
assert ev0["feasible"] is True and ev0["moves_required"] == 0
assert abs(ev0["final_u1"]["u1_mag_g_mm"]) < 1e-7
assert ev0["moves"] == []
# 锁定管所在槽失效 → 结构性不可行, 无最终摆放
evl = ev_call(evid, [0], 1e9, 3)
assert evl["feasible"] is False and evl["final_u1"] is None
assert [r["tube_id"] for r in evl["locked_on_failed_slots"]] == [1]
print("slot-evacuation locked/empty cases OK")

# 确定性方案 B: 管 1 锁定在槽 0, 管 2 自由(求解器置于对径槽, 原 |U₁|≈0)
ev2_body = dict(ev_body)
ev2_body["name"] = "应急迁移二"
ev2_body["tubes"] = [
    {"id": 1, "mass": 50, "locked": True, "slot": 0},
    {"id": 2, "mass": 50},
]
recv2 = call("POST", "/api/plan", ev2_body)
evid2 = recv2["id"]
assert abs(recv2["result"]["metrics"]["u1_mag_g_mm"]) < 1e-6
free_slot = next(p["slot"] for p in recv2["result"]["placements"]
                 if p["tube_id"] == 2)
# 失效槽为空且限值宽松 → 0 移动
ev_f0 = ev_call(evid2, [9], 1e9, 3)
assert ev_f0["feasible"] is True and ev_f0["moves_required"] == 0
# 管 2 原槽失效 → 必须迁移; 大限值下 1 次移动即可, 管 1 锁定不动
ev_f1 = ev_call(evid2, [free_slot], 1e9, 3)
assert ev_f1["feasible"] is True and ev_f1["moves_required"] == 1
mv = ev_f1["moves"][0]
assert mv["tube_id"] == 2 and mv["from_slot"] == free_slot and mv["to_slot"] != free_slot
row1 = next(t for t in ev_f1["tubes"] if t["tube_id"] == 1)
assert row1["final_slot"] == 0 and row1["moved"] is False
# 目标槽唯一且不占用失效槽
targets = [t["final_slot"] for t in ev_f1["tubes"]]
assert len(targets) == len(set(targets)) and free_slot not in targets
# 最终 |U₁| 独立复算
ux = uy = 0.0
for t in ev_f1["tubes"]:
    a = _math.radians(t["final_angle_deg"])
    ux += t["mass_g"] * _math.cos(a)
    uy += t["mass_g"] * _math.sin(a)
assert abs(100.0 * _math.hypot(ux, uy) - ev_f1["final_u1"]["u1_mag_g_mm"]) < 1e-5
# 限值 0、移动上限 0 且必须迁 1 支 → 不可行, 无对照摆放
ev_bad = ev_call(evid2, [free_slot], 0.0, 0)
assert ev_bad["feasible"] is False and ev_bad["best_effort"] is None
assert ev_bad["forced_move_count"] == 1
# 锁定管在槽 0 固定不动: 唯一可移动的管 2 必须回到对径槽 6 才能完全平衡,
# 而槽 6 已失效 → 即使允许 3 支移动也无法达到限值 0
ev_lock_no = ev_call(evid2, [free_slot], 0.0, 3)
assert ev_lock_no["feasible"] is False and ev_lock_no["best_effort"]["moves_used"] == 1
print("slot-evacuation forced-move/tie cases OK")

# 确定性方案 C: 两支 50g 均非锁定(求解器置于对径槽 0/6); 失效其中一管槽,
# 限值 0 下需 2 支共同迁到新对径对(槽 1/7 等, 并列取目标槽序列最小者)
ev3_body = dict(ev_body)
ev3_body["name"] = "应急迁移三"
ev3_body["tubes"] = [{"id": 1, "mass": 50}, {"id": 2, "mass": 50}]
recv3 = call("POST", "/api/plan", ev3_body)
evid3 = recv3["id"]
assert abs(recv3["result"]["metrics"]["u1_mag_g_mm"]) < 1e-6
pl3 = {(p["tube_id"], p["slot"]) for p in recv3["result"]["placements"]}
failed3 = next(p["slot"] for p in recv3["result"]["placements"]
               if p["tube_id"] == 1)
# 限值宽松 → 1 支移动即可; 限值 0 → 需要 2 支移动, 最终 |U₁|=0
ev3_1 = ev_call(evid3, [failed3], 1e9, 3)
assert ev3_1["feasible"] is True and ev3_1["moves_required"] == 1
ev_perfect = ev_call(evid3, [failed3], 0.0, 3)
assert ev_perfect["feasible"] is True and ev_perfect["moves_required"] == 2
assert abs(ev_perfect["final_u1"]["u1_mag_g_mm"]) < 1e-7
assert {t["final_slot"] for t in ev_perfect["tubes"]}.isdisjoint({failed3})
# 并列裁决: 目标槽序列 (管1目标, 管2目标) 字典序最小;
# 完美对为避开失效槽的全部对径槽对(正反两种分配)
key = tuple(t["final_slot"] for t in
            sorted(ev_perfect["tubes"], key=lambda t: t["tube_id"]))
pairs = [(a, (a + 6) % 12) for a in range(6)
         if failed3 not in (a, (a + 6) % 12)]
cands = [c for ab in pairs for c in (ab, ab[::-1])]
assert key == min(cands), (key, min(cands))
print("slot-evacuation perfect 2-move tie-break OK")

# 校验错误: 404 / 移动上限越界 / 失效槽不存在 / 失效槽原本禁用 / 17 管超限
try:
    ev_call(999, [1], 1.0, 1); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
for bad_ev in (
    (evid2, [99], 1.0, 1),     # 槽位不存在
    (2, [6], 1.0, 1),          # 槽 6 在 plan#2 中本就禁用
    (evid2, [1], 1.0, 4),      # 移动上限超过 3
    (evid2, [], 1.0, 1),       # 失效槽为空
    (evid2, [1], -1.0, 1),     # 负限值
):
    try:
        ev_call(*bad_ev); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code == 400, bad_ev
try:
    ev_call(recb["id"], [1], 1.0, 1); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code == 400 and "10" in str(e.detail), e.detail
# 只读: 规划后三条方案指标均不变
assert call("GET", f"/api/plan/{evid}")["result"]["metrics"]["u1_mag_g_mm"] == \
    recv["result"]["metrics"]["u1_mag_g_mm"]
assert call("GET", f"/api/plan/{evid2}")["result"]["metrics"]["u1_mag_g_mm"] == \
    recv2["result"]["metrics"]["u1_mag_g_mm"]
assert call("GET", f"/api/plan/{evid3}")["result"]["metrics"]["u1_mag_g_mm"] == \
    recv3["result"]["metrics"]["u1_mag_g_mm"]
print("slot-evacuation validation/read-only OK")

# ---- 现场装管复核(只读, 子集 DP 最小总绝对质量差匹配) ----
def rc_call(plan_id, readings, tol):
    found = find("POST", f"/api/plan/{plan_id}/tube-recheck")
    fn, args = (found if isinstance(found, tuple) else (found, ()))
    def _reading(s, m):
        o = types.SimpleNamespace(slot=int(s), mass=float(m))
        o.model_dump = lambda: {"slot": o.slot, "mass": o.mass}
        return o
    req = types.SimpleNamespace(
        readings=[_reading(s, m) for s, m in readings],
        tolerance_g=float(tol),
    )
    return fn(*args, req)

# 确定性方案: 四支不同质量试管全部锁定在槽 0/3/6/9(槽 2/11 禁用)
rc_body = dict(ev_body)
rc_body["name"] = "装管复核方案"
rc_body["slots"] = [{"index": i, "angle": i * 30, "enabled": i not in (2, 11)}
                    for i in range(12)]
rc_body["tubes"] = [
    {"id": 1, "mass": 50, "locked": True, "slot": 0},
    {"id": 2, "mass": 51, "locked": True, "slot": 3},
    {"id": 3, "mass": 52, "locked": True, "slot": 6},
    {"id": 4, "mass": 53, "locked": True, "slot": 9},
]
rec_rc = call("POST", "/api/plan", rc_body)
rcid = rec_rc["id"]
# 全部按方案装管且读数精确: 唯一匹配, 全部正确
rc_ok = rc_call(rcid, [(0, 50.0), (3, 51.0), (6, 52.0), (9, 53.0)], 0.05)
assert rc_ok["complete"] is True and rc_ok["optimal_matching_count"] == 1
assert [x["status"] for x in rc_ok["readings"]] == ["correct"] * 4
assert rc_ok["summary"] == {"correct": 4, "wrong_slot": 0, "uncertain": 0}
assert abs(rc_ok["total_abs_deviation_g"]) < 1e-9
# 管 1/2 装槽互换(读数仍在原槽): 唯一匹配, 槽 0/3 错槽, 槽 6/9 正确
rc_sw = rc_call(rcid, [(0, 51.0), (3, 50.0), (6, 52.0), (9, 53.0)], 0.05)
st = {x["slot"]: x for x in rc_sw["readings"]}
assert st[0]["status"] == "wrong_slot" and st[0]["matched_tube_id"] == 2
assert st[3]["status"] == "wrong_slot" and st[3]["matched_tube_id"] == 1
assert st[6]["status"] == "correct" and st[9]["status"] == "correct"
assert rc_sw["summary"]["wrong_slot"] == 2
tt = {x["tube_id"]: x for x in rc_sw["tubes"]}
assert tt[1]["candidate_slots"] == [3] and tt[2]["candidate_slots"] == [0]
# 读数数量不等: 说明原因, 不做匹配
rc_ne = rc_call(rcid, [(0, 50.0), (3, 51.0)], 0.05)
assert rc_ne["complete"] is False and "不等" in rc_ne["reason"]
# 不存在完整匹配: 管 2(51 g)在偏差内无兼容读数, 最多匹配 3/4 对
rc_no = rc_call(rcid, [(0, 50.0), (3, 50.0), (6, 52.0), (9, 53.0)], 0.05)
assert rc_no["complete"] is False and rc_no["max_matched_pairs"] == 3
assert [t["tube_id"] for t in rc_no["tubes_without_candidate"]] == [2]
assert "不存在完整的一对一匹配" in rc_no["reason"]
print("tube-recheck correct/wrong-slot/incomplete OK")

# 等质量方案: 最优匹配不唯一 -> 身份不确定, 不得断言错放
rc2_body = dict(ev_body)
rc2_body["name"] = "装管复核等质量方案"
rc2_body["tubes"] = [
    {"id": 1, "mass": 50, "locked": True, "slot": 0},
    {"id": 2, "mass": 50, "locked": True, "slot": 6},
]
rec_rc2 = call("POST", "/api/plan", rc2_body)
rc_unc = rc_call(rec_rc2["id"], [(0, 50.0), (6, 50.0)], 0.05)
assert rc_unc["complete"] is True and rc_unc["optimal_matching_count"] == 2
assert [x["status"] for x in rc_unc["readings"]] == ["uncertain"] * 2
assert all(x["matched_tube_id"] is None for x in rc_unc["readings"])
assert "身份不确定" in rc_unc["reason"]
# 与独立暴力枚举对照(全排列求最小总差 + 候选集合)
def _brute_recheck(plan, readings, tol):
    tubes = sorted(
        ((p["tube_id"], p["slot"], p["mass"])
         for p in plan["result"]["placements"] if p["slot"] is not None),
        key=lambda x: x[0])
    rd = sorted(readings)
    n = len(tubes)
    best = None; cnt = 0; cand = [set() for _ in range(n)]
    for perm in _it.permutations(range(n)):
        tot = 0.0; ok = True
        for k in range(n):
            d = abs(tubes[k][2] - rd[perm[k]][1])
            if d > tol + 1e-9:
                ok = False; break
            tot += d
        if not ok:
            continue
        if best is None or tot < best - 1e-9 * max(1.0, best):
            best, cnt = tot, 1
            cand = [set() for _ in range(n)]
            for k in range(n):
                cand[perm[k]].add(tubes[k][0])
        elif tot <= best + 1e-9 * max(1.0, best):
            cnt += 1
            for k in range(n):
                cand[perm[k]].add(tubes[k][0])
    return best, cnt, cand
plan2 = call("GET", f"/api/plan/{rcid}")
_rd = [(0, 50.01), (3, 51.02), (6, 51.99), (9, 53.0)]
rc_rb = rc_call(rcid, _rd, 0.05)
b_best, b_cnt, b_cand = _brute_recheck(plan2, _rd, 0.05)
assert abs(rc_rb["total_abs_deviation_g"] - b_best) < 1e-9
assert rc_rb["optimal_matching_count"] == b_cnt
for row in rc_rb["readings"]:
    j = [r[0] for r in sorted(_rd)].index(row["slot"])
    assert {c["tube_id"] for c in row["candidates"]} == b_cand[j]
print("tube-recheck brute-force cross-check OK")

# 校验错误: 精度不足 / 禁用槽 / 槽位重复 / 13 份读数 / 负偏差 -> 400; 方案不存在 -> 404
for bad_rc in (
    ([(0, 50.005), (3, 51.0), (6, 52.0), (9, 53.0)], 0.05),   # 非 0.01 整数倍
    ([(0, 50.0), (3, 51.0), (6, 52.0), (2, 53.0)], 0.05),     # 槽 2 为禁用槽
    ([(0, 50.0), (0, 51.0), (6, 52.0), (9, 53.0)], 0.05),     # 槽位重复
    ([(i, 50.0) for i in range(13)], 0.05),                   # 超过 12 份
    ([(0, 50.0), (3, 51.0), (6, 52.0), (9, 53.0)], -0.1),     # 负偏差
):
    try:
        rc_call(rcid, *bad_rc); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code == 400, (bad_rc, e)
try:
    rc_call(999, [(0, 50.0)], 0.05); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
# 只读: 复核后原方案指标不变
assert call("GET", f"/api/plan/{rcid}")["result"]["metrics"]["u1_mag_g_mm"] == \
    rec_rc["result"]["metrics"]["u1_mag_g_mm"]
print("tube-recheck validation/read-only OK")


# ---- 互斥试管隔离(精确完全枚举; 禁用槽仍占物理相邻关系) ----
import itertools as _itx

def mutex_plan_body(tubes, pairs, disabled=(), n_slots=12, **ov):
    body = {
        "name": "互斥方案", "slot_count": n_slots,
        "slots": [{"index": i, "angle": 360.0 * i / n_slots,
                   "enabled": i not in disabled} for i in range(n_slots)],
        "tubes": tubes, "radius_mm": 100.0, "rpm": 4000,
        "w1": 1.0, "w2": 1e-6, "restarts": 4, "chain_steps": 200,
        "mutex_pairs": [{"tube_a": a, "tube_b": b} for a, b in pairs],
    }
    body.update(ov)
    return body

def brute_mutex(body):
    """全排列参考: (最小评分, 管号升序槽位序列, 合法摆摆放数); 锁定管固定。"""
    slots = body["slots"]
    order = sorted(slots, key=lambda s: (s["angle"] % 360, s["index"]))
    adj = {s["index"]: set() for s in slots}
    for i, s in enumerate(order):
        a, b = s["index"], order[(i + 1) % len(order)]["index"]
        if a != b:
            adj[a].add(b); adj[b].add(a)
    pairs = [tuple(sorted((q["tube_a"], q["tube_b"]))) for q in body["mutex_pairs"]]
    tubes = sorted(body["tubes"], key=lambda t: t["id"])
    fix = {t["id"]: t["slot"] for t in tubes if t.get("locked")}
    free_slots = [s["index"] for s in slots if s["enabled"] and s["index"] not in fix.values()]
    free_t = [t for t in tubes if t["id"] not in fix]
    # 锁定-锁定互斥相邻 -> 结构性不可行
    for a, b in pairs:
        if a in fix and b in fix and fix[b] in adj[fix[a]]:
            return None, None, 0
    best = None; bestkey = None; cnt = 0
    for comb in _itx.permutations(free_slots, len(free_t)):
        loc = dict(fix)
        loc.update({free_t[i]["id"]: comb[i] for i in range(len(free_t))})
        if any(loc[b] in adj[loc[a]] for a, b in pairs):
            continue
        cnt += 1
        ux = uy = ux2 = uy2 = 0.0
        for t in tubes:
            a = _math.radians(360.0 * loc[t["id"]] / body["slot_count"])
            m = t["mass"]
            ux += m * _math.cos(a); uy += m * _math.sin(a)
            ux2 += m * _math.cos(2 * a); uy2 += m * _math.sin(2 * a)
        sc = (ux * ux + uy * uy) + 1e-6 * (ux2 * ux2 + uy2 * uy2)
        key = tuple(loc[t["id"]] for t in tubes)
        if best is None or sc < best - 1e-9 * max(1.0, abs(best)):
            best, bestkey = sc, key
        elif abs(sc - best) <= 1e-9 * max(1.0, abs(best)) and key < bestkey:
            bestkey = key
    return best, bestkey, cnt

mx_tubes = [{"id": i, "mass": m} for i, m in
            zip(range(1, 7), (50, 50, 51, 49, 30, 30))]
mx_body = mutex_plan_body(mx_tubes, [(1, 2), (3, 4)], disabled=(10, 11))
mx_rec = call("POST", "/api/plan", mx_body)
assert mx_rec["id"], "互斥方案应正常保存"
mx_r = mx_rec["result"]
assert mx_r["config"]["variant"] == "exact-mutex"
assert mx_r["config"]["mutex_pair_count"] == 2
assert len(mx_r["exact_search"]["mutex_pairs"]) == 2
mx_loc = {p["tube_id"]: p["slot"] for p in mx_r["placements"]}
# 物理相邻关系: 禁用槽 10/11 仍占位, 9 与 0 不相邻
mx_order = sorted(mx_body["slots"], key=lambda s: (s["angle"] % 360, s["index"]))
mx_adj = {s["index"]: set() for s in mx_body["slots"]}
for i, s in enumerate(mx_order):
    a, b = s["index"], mx_order[(i + 1) % 12]["index"]
    mx_adj[a].add(b); mx_adj[b].add(a)
for a, b in ((1, 2), (3, 4)):
    assert mx_loc[b] not in mx_adj[mx_loc[a]], (a, b, mx_loc)
# 与全排列参考一致(最小评分 + 平分裁决 + 叶子数)
bs, bkey, bcnt = brute_mutex(mx_body)
assert mx_r["exact_search"]["evaluated_arrangements"] == bcnt
key = tuple(mx_loc[t["id"]] for t in sorted(mx_body["tubes"], key=lambda t: t["id"]))
assert key == bkey, (key, bkey)
# 每支试管都带互斥搭档标注
pw = {p["tube_id"]: p["mutex_with"] for p in mx_r["placements"]}
assert pw[1] == [2] and pw[2] == [1] and pw[5] == []
# 回放步骤末步与指标一致且互斥约束全程成立
last_step = mx_r["steps"][-1]
assert abs(last_step["vectors"]["u1_mag"] - mx_r["metrics"]["u1_mag_g_mm"]) < 1e-6
for st in mx_r["steps"]:
    sm = {p["tube_id"]: p["slot"] for p in st["placements"] if p["slot"] is not None}
    for a, b in ((1, 2), (3, 4)):
        if a in sm and b in sm:
            assert sm[b] not in mx_adj[sm[a]], st["seq"]
# 请求落库保留互斥对(结果回放/再载入)
saved_req = call("GET", f"/api/plan/{mx_rec['id']}")["request"]
assert [(q["tube_a"], q["tube_b"]) for q in saved_req["mutex_pairs"]] == [(1, 2), (3, 4)]
print("mutex-pair exact search OK: evaluated=%d key=%s U1=%.3f" % (
    bcnt, key, mx_r["metrics"]["u1_mag_g_mm"]))

# 禁用槽占据物理相邻: 仅槽 0/2 启用(槽 1 禁用介于其间) -> 不相邻, 可行; 仅 0/1 -> 相邻, 不可行
two_body = mutex_plan_body(
    [{"id": 1, "mass": 50}, {"id": 2, "mass": 50}], [(1, 2)],
    n_slots=12)
two_body["slots"] = [{"index": i, "angle": i * 30, "enabled": i in (0, 2)}
                     for i in range(12)]
two_r = call("POST", "/api/plan", two_body)
two_loc = {p["tube_id"]: p["slot"] for p in two_r["result"]["placements"]}
assert set(two_loc.values()) == {0, 2}
adj_body = mutex_plan_body(
    [{"id": 1, "mass": 50}, {"id": 2, "mass": 50}], [(1, 2)], n_slots=12)
adj_body["slots"] = [{"index": i, "angle": i * 30, "enabled": i in (0, 1)}
                     for i in range(12)]
try:
    call("POST", "/api/plan", adj_body); raise SystemExit("should 400: no legal placement")
except main.HTTPException as e:
    assert e.status_code == 400 and "不可行" in e.detail, e.detail

# 锁定-锁定互斥相邻 -> 400 且不保存; 无合法摆放 -> 400 且不保存
n_before = len(call("GET", "/api/plans", )["items"])
lock_conflict = mutex_plan_body(
    [{"id": 1, "mass": 50, "locked": True, "slot": 0},
     {"id": 2, "mass": 50, "locked": True, "slot": 1}], [(1, 2)],
    disabled=(10, 11))
try:
    call("POST", "/api/plan", lock_conflict); raise SystemExit("should 400: locked conflict")
except main.HTTPException as e:
    assert e.status_code == 400 and "不可行" in e.detail, e.detail
no_room = mutex_plan_body(
    [{"id": 1, "mass": 50, "locked": True, "slot": 0},
     {"id": 2, "mass": 50, "locked": True, "slot": 2},
     {"id": 3, "mass": 50}], [(1, 3), (2, 3)], n_slots=3)
no_room["slots"] = [{"index": i, "angle": i * 120, "enabled": True} for i in range(3)]
try:
    call("POST", "/api/plan", no_room); raise SystemExit("should 400: infeasible")
except main.HTTPException as e:
    assert e.status_code == 400 and "不可行" in e.detail, e.detail
assert len(call("GET", "/api/plans")["items"]) == n_before, "不可行请求不得保存方案"

# 限额: 8 管 / 11 启用槽 / 13 对 / 自互斥 / 重复对 / 未知管号 -> 400
eight = mutex_plan_body([{"id": i, "mass": 10} for i in range(1, 9)], [(1, 2)],
                        disabled=(10, 11))
eleven = mutex_plan_body([{"id": 1, "mass": 10}, {"id": 2, "mass": 10}], [(1, 2)])
eleven["slots"][11]["enabled"] = True
thirteen = mutex_plan_body([{"id": i, "mass": 10} for i in range(1, 7)],
                           [(1, 2)] * 13, disabled=(10, 11))
self_pair = mutex_plan_body([{"id": 1, "mass": 10}, {"id": 2, "mass": 10}],
                            [(1, 1)], disabled=(10, 11))
dup_pair = mutex_plan_body([{"id": 1, "mass": 10}, {"id": 2, "mass": 10}],
                           [(1, 2), (2, 1)], disabled=(10, 11))
unknown = mutex_plan_body([{"id": 1, "mass": 10}, {"id": 2, "mass": 10}],
                          [(1, 9)], disabled=(10, 11))
for bad in (eight, eleven, thirteen, self_pair, dup_pair, unknown):
    try:
        call("POST", "/api/plan", bad); raise SystemExit("should 400: " + str(bad.get("mutex_pairs")))
    except main.HTTPException as e:
        assert e.status_code == 400, e
print("mutex-pair validation/infeasible/not-saved OK")

# 含互斥对的增量重排: 新增试管, 沿用质量/锁定/权重/增量输入, 结果仍精确最优
inc_body = json.loads(json.dumps(mx_body))
inc_body["name"] = "互斥增量方案"
inc_body["tubes"].append({"id": 7, "mass": 20})
inc_body["previous"] = [{"tube_id": p["tube_id"], "slot": p["slot"]}
                        for p in mx_r["placements"]]
inc_body["parent_id"] = mx_rec["id"]
inc_rec = call("POST", "/api/plan", inc_body)
inc_r = inc_rec["result"]
assert inc_r["config"]["incremental"] is True
assert inc_r["config"]["variant"] == "exact-mutex"
bs2, bkey2, bcnt2 = brute_mutex(inc_body)
inc_loc = {p["tube_id"]: p["slot"] for p in inc_r["placements"]}
key2 = tuple(inc_loc[t["id"]] for t in sorted(inc_body["tubes"], key=lambda t: t["id"]))
assert key2 == bkey2, (key2, bkey2)
assert inc_r["exact_search"]["evaluated_arrangements"] == bcnt2
# 增量输入保留: 未移动管沿用旧槽
for p in inc_r["placements"]:
    if p["tube_id"] != 7 and p["tube_id"] not in inc_r["moved_tube_ids"]:
        old = next(q for q in mx_r["placements"] if q["tube_id"] == p["tube_id"])
        assert p["slot"] == old["slot"]
# 保存方案同样保留互斥对
assert [(q["tube_a"], q["tube_b"])
        for q in call("GET", f"/api/plan/{inc_rec['id']}")["request"]["mutex_pairs"]] == [(1, 2), (3, 4)]
print("mutex-pair incremental OK: evaluated=%d moved=%s" % (
    bcnt2, inc_r["moved_tube_ids"]))

# 无互斥对: 请求缺省 mutex_pairs, 沿用启发式求解
plain = dict(req_body)
plain["name"] = "无互斥普通方案"
plain["slots"] = [{"index": i, "angle": i * 30, "enabled": True} for i in range(12)]
plain_rec = call("POST", "/api/plan", plain)
assert "exact_search" not in plain_rec["result"]
assert plain_rec["result"]["config"]["mutex_pair_count"] == 0
assert plain_rec["request"]["mutex_pairs"] == []
print("mutex-pair absent heuristic path OK")
print("MUTEX SMOKE TESTS PASS")

print("BACKEND SMOKE TESTS PASS")
