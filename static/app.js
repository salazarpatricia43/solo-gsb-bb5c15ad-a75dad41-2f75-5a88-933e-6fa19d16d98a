/* 离心机转子配平规划器 —— 原生前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  slots: [],            // {index, angle, enabled}
  tubes: [],            // {id, mass, locked, slot}
  mutexPairs: [],       // {a, b} 互斥试管对
  result: null,         // 最新计算结果
  record: null,         // 含 id/name/request
  replay: { steps: [], pos: 0, timer: null },
  nextTubeId: 1,
};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.className = "toast"), 3200);
}
function fmt(x, d = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  if (Math.abs(x) >= 1000) return x.toFixed(0);
  return Number(x).toFixed(d);
}
function angleNorm(a) { return ((a % 360) + 360) % 360; }

// ---------------------------------------------------------------------------
// 输入:槽位
// ---------------------------------------------------------------------------
function genSlots() {
  const n = Math.max(2, Math.min(128, parseInt($("slotCount").value) || 12));
  $("slotCount").value = n;
  state.slots = Array.from({ length: n }, (_, i) => ({
    index: i, angle: +(360 * i / n).toFixed(4), enabled: true,
  }));
  renderSlotEditor();
  drawRotor(null);
}

function renderSlotEditor() {
  const box = $("slotEditor");
  box.innerHTML = "";
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>槽</th><th>角度°</th><th>启用</th></tr></thead>";
  const tb = document.createElement("tbody");
  state.slots.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.index}</td>
      <td><input type="number" step="any" value="${s.angle}" data-role="angle" data-idx="${s.index}" style="width:90px"></td>
      <td><input type="checkbox" ${s.enabled ? "checked" : ""} data-role="en" data-idx="${s.index}"></td>`;
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  box.appendChild(table);
  table.addEventListener("change", (e) => {
    const idx = +e.target.dataset.idx;
    const sl = state.slots[idx];
    if (e.target.dataset.role === "angle") sl.angle = parseFloat(e.target.value) || 0;
    if (e.target.dataset.role === "en") sl.enabled = e.target.checked;
    drawRotor(state.result);
  });
}

// ---------------------------------------------------------------------------
// 输入:试管
// ---------------------------------------------------------------------------
function addTube(t) {
  const id = t?.id ?? state.nextTubeId++;
  state.nextTubeId = Math.max(state.nextTubeId, id + 1);
  state.tubes.push({
    id,
    mass: t?.mass ?? 50,
    locked: t?.locked ?? false,
    slot: t?.slot ?? null,
  });
  renderTubes();
}

function renderTubes() {
  const tb = $("tubeTable").querySelector("tbody");
  tb.innerHTML = "";
  state.tubes.forEach((t, i) => {
    const tr = document.createElement("tr");
    tr.className = "tube-row";
    const opts = ['<option value="">—</option>']
      .concat(state.slots.map((s) => `<option value="${s.index}" ${t.slot === s.index ? "selected" : ""}>${s.index}</option>`))
      .join("");
    tr.innerHTML = `
      <td><input type="number" data-f="id" value="${t.id}" style="width:52px"></td>
      <td><input type="number" step="any" data-f="mass" value="${t.mass}"></td>
      <td style="text-align:center"><input type="checkbox" data-f="locked" ${t.locked ? "checked" : ""}></td>
      <td><select data-f="slot" ${t.locked ? "" : "disabled"}>${opts}</select></td>
      <td><button class="iconbtn" data-act="del">删</button></td>`;
    tr.addEventListener("input", (e) => {
      const f = e.target.dataset.f;
      if (!f) return;
      if (f === "id") t.id = parseInt(e.target.value) || t.id;
      if (f === "mass") t.mass = parseFloat(e.target.value) || 0;
      if (f === "locked") {
        t.locked = e.target.checked;
        renderTubes();
      }
      if (f === "slot") t.slot = e.target.value === "" ? null : parseInt(e.target.value);
    });
    tr.querySelector('[data-act="del"]').onclick = () => {
      state.tubes.splice(i, 1);
      renderTubes();
    };
    tb.appendChild(tr);
  });
  $("tubeCount").textContent = `共 ${state.tubes.length} 支`;
  // 试管清单变动后刷新互斥对下拉项(保留已选编号)
  if (state.mutexPairs?.length) renderMutexPairs();
}

// ---------------------------------------------------------------------------
// 输入:互斥试管对
// ---------------------------------------------------------------------------
function addMutexPair(p) {
  if (state.mutexPairs.length >= 12) {
    toast("互斥试管对最多 12 对。", true);
    return;
  }
  state.mutexPairs.push({ a: p?.a ?? null, b: p?.b ?? null });
  renderMutexPairs();
}

function renderMutexPairs() {
  const tb = $("mutexTable").querySelector("tbody");
  tb.innerHTML = "";
  const optsFor = (sel) => ['<option value="">选择管号</option>']
    .concat(state.tubes.map((t) =>
      `<option value="${t.id}" ${sel === t.id ? "selected" : ""}>${t.id} (${t.mass}g)</option>`))
    .join("");
  state.mutexPairs.forEach((mp, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><select data-f="a">${optsFor(mp.a)}</select></td>
      <td class="small" style="text-align:center">↮ 不得相邻</td>
      <td><select data-f="b">${optsFor(mp.b)}</select></td>
      <td><button class="iconbtn" data-act="del">删</button></td>`;
    tr.addEventListener("change", (e) => {
      const f = e.target.dataset.f;
      if (!f) return;
      const v = e.target.value;
      mp[f] = v === "" ? null : parseInt(v, 10);
    });
    tr.querySelector('[data-act="del"]').onclick = () => {
      state.mutexPairs.splice(i, 1);
      renderMutexPairs();
    };
    tb.appendChild(tr);
  });
  $("mutexCount").textContent = `共 ${state.mutexPairs.length} / 12 对` +
    (state.mutexPairs.length ? " · 含互斥对时限 7 管 / 10 启用槽" : "");
}

// ---------------------------------------------------------------------------
// SVG 转子绘制
// ---------------------------------------------------------------------------
const SVG_NS = "http://www.w3.org/2000/svg";
function el(tag, attrs = {}) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}
function polar(cx, cy, r, angleDeg) {
  // 0° 在正上方, 顺时针
  const a = (angleDeg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

function drawRotor(result, replayPlacements = null, replayVectors = null) {
  const svg = $("rotor");
  svg.innerHTML = `
    <defs>
      <marker id="arrow1" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6 Z" fill="#4f9dff"/>
      </marker>
      <marker id="arrow2" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6 Z" fill="#36d399"/>
      </marker>
    </defs>
    <circle class="hub" cx="210" cy="210" r="30"/>
    <text x="210" y="206" text-anchor="middle" fill="#94a3c0" font-size="9">R=${$("radius").value}mm</text>
    <text x="210" y="220" text-anchor="middle" fill="#94a3c0" font-size="9">${$("rpm").value}rpm</text>`;

  if (!state.slots.length) return;
  const R = 158;
  const tubeBySlot = new Map();
  const movedSlots = new Set();
  let placements;
  if (replayPlacements) {
    placements = replayPlacements;
  } else if (result) {
    placements = result.placements;
    result.placements.forEach((p) => {
      if (p.moved) movedSlots.add(p.slot);
    });
  }
  if (placements) placements.forEach((p) => { if (p.slot !== null) tubeBySlot.set(p.slot, p); });

  // 对径配对辅助线
  if (result && !replayPlacements) {
    result.pairs.forEach((pr) => {
      const a = state.slots[pr.slot_a], b = state.slots[pr.slot_b];
      if (!a || !b) return;
      const [x1, y1] = polar(210, 210, R, a.angle);
      const [x2, y2] = polar(210, 210, R, b.angle);
      svg.appendChild(el("line", {
        x1, y1, x2, y2,
        stroke: pr.opposite_deviation_deg <= 0.5 ? "#36d39955" : "#fbbd2344",
        "stroke-width": 1, "stroke-dasharray": "4 4",
      }));
    });
  }

  // 互斥隔离: 物理相邻环(含禁用槽)与互斥对连线
  const mutexPairs = result?.exact_search?.mutex_pairs || [];
  if (mutexPairs.length) {
    // 按角度环形排序(同角按索引), 禁用槽仍参与, 构成物理相邻多边形
    const order = [...state.slots].sort((s, t) =>
      (angleNorm(s.angle) - angleNorm(t.angle)) || (s.index - t.index));
    const pts = order.map((s) => polar(210, 210, R, s.angle))
      .map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    svg.appendChild(el("polygon", {
      points: pts, fill: "none", stroke: "#6b779466", "stroke-width": 1,
      "stroke-dasharray": "2 5",
    }));
    // 互斥对两支试管当前所在槽位的红色连线
    const tubeBySlotM = new Map();
    if (placements) placements.forEach((p) => { if (p.slot !== null && p.slot !== undefined) tubeBySlotM.set(p.tube_id, p.slot); });
    mutexPairs.forEach(([a, b]) => {
      const sa = state.slots[tubeBySlotM.get(a)];
      const sb = state.slots[tubeBySlotM.get(b)];
      if (!sa || !sb) return;
      const [x1, y1] = polar(210, 210, R + 6, sa.angle);
      const [x2, y2] = polar(210, 210, R + 6, sb.angle);
      svg.appendChild(el("line", {
        x1, y1, x2, y2, stroke: "#f87272", "stroke-width": 1.6,
        "stroke-dasharray": "6 3", opacity: 0.85,
      }));
    });
  }

  state.slots.forEach((s) => {
    const [x, y] = polar(210, 210, R, s.angle);
    const g = el("g", { class: "slot-node" + (s.enabled ? "" : " disabled"),
      transform: `translate(${x.toFixed(2)},${y.toFixed(2)})` });
    const occ = tubeBySlot.get(s.index);
    if (occ) {
      if (occ.locked) g.classList.add("locked");
      if (movedSlots.has(s.index)) g.classList.add("moved");
    }
    g.appendChild(el("circle", { class: "ring", r: occ ? 20 : 15 }));
    if (occ) {
      const t1 = el("text", { class: "tube-label", y: -2 });
      t1.textContent = "T" + occ.tube_id;
      g.appendChild(t1);
      const t2 = el("text", { y: 9, "font-size": 9, fill: "#cdd8ee", "text-anchor": "middle" });
      t2.textContent = occ.mass + "g";
      g.appendChild(t2);
      const t3 = el("text", { y: 32, "font-size": 9, fill: "#94a3c0", "text-anchor": "middle" });
      t3.textContent = "#" + s.index;
      g.appendChild(t3);
    } else {
      const t1 = el("text", { y: 3, "font-size": 10 });
      t1.textContent = s.index;
      g.appendChild(t1);
    }
    g.addEventListener("click", () => {
      // 仅点击空槽时切换启用以避免误触; 禁用槽点击重新启用
      if (!occ) {
        s.enabled = !s.enabled;
        renderSlotEditor();
        drawRotor(result, replayPlacements);
      }
    });
    g.addEventListener("mouseenter", () => g.classList.add("hover"));
    g.addEventListener("mouseleave", () => g.classList.remove("hover"));
    svg.appendChild(g);
  });

  // 不平衡向量(回放时显示该步瞬时向量)
  const vectors = replayVectors || (replayPlacements ? null : result?.metrics) || null;
  if (vectors) {
    const scale1 = Math.min(130 / Math.max(vectors.u1_mag_g_mm, 1), 0.13);
    const scale2 = Math.min(130 / Math.max(vectors.u2_mag_g_mm, 1), 0.13);
    drawVector(svg, vectors.u1_angle_deg, vectors.u1_mag_g_mm * scale1, "vec vec1", "url(#arrow1)", 210, 210);
    drawVector(svg, vectors.u2_angle_deg, vectors.u2_mag_g_mm * scale2, "vec vec2", "url(#arrow2)", 210, 210);
  }
}

function drawVector(svg, angleDeg, len, cls, marker, cx, cy) {
  if (len < 2) return;
  const [x, y] = polar(cx, cy, len, angleDeg);
  svg.appendChild(el("line", { x1: cx, y1: cy, x2: x, y2: y, class: cls, "marker-end": marker }));
}

// ---------------------------------------------------------------------------
// 结果渲染
// ---------------------------------------------------------------------------
function renderMetrics(m) {
  $("mU1").innerHTML = `${fmt(m.u1_mag_g_mm)} <span class="u">g·mm @${fmt(m.u1_angle_deg, 1)}°</span>`;
  $("mU2").innerHTML = `${fmt(m.u2_mag_g_mm)} <span class="u">g·mm @${fmt(m.u2_angle_deg, 1)}°</span>`;
  $("mForce").innerHTML = `${fmt(m.centrifugal_force_n, 3)} <span class="u">N</span>`;
  $("mMoment").innerHTML = `${fmt(m.resultant_moment_n_m, 4)} <span class="u">N·m</span>`;
  $("mEcc").innerHTML = `${fmt(m.equivalent_eccentricity_um, 2)} <span class="u">μm</span>
    <span class="grade g${m.grade}">${m.grade}</span>`;
  $("mGrade").innerHTML = `<span class="grade g${m.grade}" style="font-size:15px">${m.grade}</span>
    <span class="u">${{ A: "优", B: "良", C: "合格", D: "偏高" }[m.grade]}</span>`;
}

function renderResult(record) {
  state.record = record;
  state.result = record.result;
  const r = state.result, m = r.metrics;
  renderMetrics(m);
  $("solveInfo").textContent = `求解耗时 ${r.solve_time_ms ?? "–"} ms · ${r.total_tubes} 支试管 · ${r.steps.length} 个步骤`;
  const sum = $("summary");
  sum.style.display = "block";
  sum.innerHTML = r.summary;
  $("btnExport").disabled = false;
  $("btnCopy").disabled = false;
  // 误差校核/振动校准/逐管装载/应急迁移/装管复核只针对已保存方案: 未保存(草稿)时禁用
  $("btnWeighing").disabled = !(record.id !== null && record.id !== undefined);
  $("btnCalib").disabled = !(record.id !== null && record.id !== undefined);
  $("btnLoading").disabled = !(record.id !== null && record.id !== undefined);
  $("btnEvac").disabled = !(record.id !== null && record.id !== undefined);
  $("btnRecheck").disabled = !(record.id !== null && record.id !== undefined);

  drawRotor(r);
  renderPairs(r);
  renderPlacements(r);
  renderMutexInfo(r);
  initReplay(r);
}

function renderMutexInfo(r) {
  const box = $("mutexInfo");
  const ex = r.exact_search;
  if (!ex) { box.style.display = "none"; box.innerHTML = ""; return; }
  const pairsTxt = ex.mutex_pairs.map(([a, b]) => `管 ${a} ↔ 管 ${b}`).join("、");
  const keptTxt = ex.kept_tube_ids.length
    ? ` · 沿用旧槽 ${ex.kept_tube_ids.length} 支(${ex.kept_tube_ids.join(", ")})`
    : "";
  box.style.display = "block";
  box.innerHTML = `🔒 <b>互斥试管隔离 · 精确搜索</b>: ${ex.mutex_pairs.length} 对互斥试管
    (${pairsTxt})均不落在物理相邻槽位(禁用槽仍占相邻关系); 共评估
    <b>${ex.evaluated_arrangements}</b> 种合法摆放, 取得现有评分最小值,
    平分时按管号升序的槽位序列裁决${keptTxt}。`;
}

function renderPairs(r) {
  const box = $("pairList");
  box.innerHTML = "";
  if (!r.pairs.length && !r.singletons.length) {
    box.innerHTML = '<p class="muted">暂无摆放。</p>';
    return;
  }
  r.pairs.forEach((p) => {
    const d = document.createElement("div");
    d.className = "reason";
    d.innerHTML = `<div class="rhead">对径配对 · 槽 ${p.slot_a} ↔ 槽 ${p.slot_b}
      ${p.locked_a || p.locked_b ? '<span class="pill lock">含锁定</span>' : ""}</div>${p.reason}`;
    box.appendChild(d);
  });
  r.singletons.forEach((s) => {
    const d = document.createElement("div");
    d.className = "reason single";
    d.innerHTML = `<div class="rhead">无对径搭档 · 槽 ${s.slot}
      ${s.locked ? '<span class="pill lock">锁定</span>' : ""}</div>${s.reason}`;
    box.appendChild(d);
  });
}

function renderPlacements(r) {
  const tb = $("placementTable").querySelector("tbody");
  tb.innerHTML = "";
  r.placements.forEach((p) => {
    const tr = document.createElement("tr");
    const tags = [
      p.locked ? '<span class="pill lock">锁定</span>' : "",
      p.moved ? '<span class="pill move">已移动</span>' : "",
    ].join(" ");
    const mutexTxt = (p.mutex_with && p.mutex_with.length)
      ? p.mutex_with.map((t) => `<span class="pill mutex">↮${t}</span>`).join(" ")
      : "–";
    tr.innerHTML = `
      <td>${p.tube_id}</td><td>${p.mass}</td>
      <td>${p.slot ?? "–"}</td>
      <td>${p.angle !== null && p.angle !== undefined ? fmt(p.angle, 1) + "°" : "–"}</td>
      <td>${p.previous_slot ?? "–"}</td><td>${mutexTxt}</td><td>${tags}</td>`;
    tb.appendChild(tr);
  });
}

// ---------------------------------------------------------------------------
// 回放
// ---------------------------------------------------------------------------
function initReplay(r) {
  stopPlay();
  state.replay.steps = r.steps;
  state.replay.pos = r.steps.length - 1;
  const range = $("rpRange");
  range.min = 0;
  range.max = Math.max(0, r.steps.length - 1);
  range.value = range.max;
  showReplayStep();
}

function showReplayStep() {
  const { steps, pos } = state.replay;
  if (!steps.length) return;
  const step = steps[pos];
  $("rpPos").textContent = `${pos + 1}/${steps.length}`;
  $("rpRange").value = pos;
  const kindMap = { init: "初始", pair: "配对", single: "补偿", refine: "微调优化", exact: "精确定位" };
  $("stepReason").innerHTML =
    `<span class="badge">${kindMap[step.kind] || step.kind} · 步骤 ${pos + 1}</span> ${step.reason}
     <div class="small" style="margin-top:4px">此步后: |U₁|=${fmt(step.vectors.u1_mag)} g·mm,
      |U₂|=${fmt(step.vectors.u2_mag)} g·mm</div>`;
  drawRotor(state.result, step.placements, {
    u1_mag_g_mm: step.vectors.u1_mag, u1_angle_deg: step.vectors.u1_angle,
    u2_mag_g_mm: step.vectors.u2_mag, u2_angle_deg: step.vectors.u2_angle,
  });
}

function stopPlay() {
  if (state.replay.timer) { clearInterval(state.replay.timer); state.replay.timer = null; $("rpPlay").textContent = "▶ 播放"; }
}
$("rpFirst").onclick = () => { stopPlay(); state.replay.pos = 0; showReplayStep(); };
$("rpPrev").onclick = () => { stopPlay(); state.replay.pos = Math.max(0, state.replay.pos - 1); showReplayStep(); };
$("rpNext").onclick = () => { stopPlay(); state.replay.pos = Math.min(state.replay.steps.length - 1, state.replay.pos + 1); showReplayStep(); };
$("rpLast").onclick = () => { stopPlay(); state.replay.pos = state.replay.steps.length - 1; showReplayStep(); };
$("rpRange").oninput = (e) => { stopPlay(); state.replay.pos = +e.target.value; showReplayStep(); };
$("rpPlay").onclick = () => {
  if (state.replay.timer) { stopPlay(); return; }
  $("rpPlay").textContent = "⏸ 暂停";
  state.replay.timer = setInterval(() => {
    if (state.replay.pos >= state.replay.steps.length - 1) { stopPlay(); state.replay.pos = 0; showReplayStep(); return; }
    state.replay.pos++; showReplayStep();
  }, 900);
};

// ---------------------------------------------------------------------------
// 调用后端
// ---------------------------------------------------------------------------
function buildRequest() {
  const req = {
    name: $("planName").value || "未命名方案",
    slot_count: state.slots.length,
    slots: state.slots,
    tubes: state.tubes.map((t) => ({
      id: t.id, mass: t.mass, locked: t.locked,
      slot: t.locked ? t.slot : null,
    })),
    radius_mm: parseFloat($("radius").value),
    rpm: parseFloat($("rpm").value),
    w1: parseFloat($("w1").value),
    w2: parseFloat($("w2").value),
    move_weight: parseFloat($("moveWeight").value),
    restarts: parseInt($("restarts").value),
    chain_steps: parseInt($("chainSteps").value),
    mutex_pairs: state.mutexPairs
      .filter((mp) => mp.a !== null && mp.b !== null)
      .map((mp) => ({ tube_a: mp.a, tube_b: mp.b })),
  };
  if ($("incremental").checked && state.result) {
    req.previous = state.result.placements
      .filter((p) => p.slot !== null && p.slot !== undefined)
      .map((p) => ({ tube_id: p.tube_id, slot: p.slot }));
    req.parent_id = state.record?.id ?? null;
  }
  return req;
}

async function compute() {
  if (!state.tubes.length) { toast("请先添加试管。", true); return; }
  // 前端预检:锁定试管必须有槽
  for (const t of state.tubes) {
    if (t.locked && (t.slot === null || t.slot === undefined || isNaN(t.slot))) {
      toast(`锁定试管 ${t.id} 必须指定固定槽位。`, true); return;
    }
    if (!(t.mass > 0)) { toast(`试管 ${t.id} 质量必须为正数。`, true); return; }
  }
  const enabled = state.slots.filter((s) => s.enabled).length;
  if (state.tubes.length > enabled) { toast(`试管数(${state.tubes.length})超过可用槽位数(${enabled})。`, true); return; }

  // 互斥对预检
  const pairs = state.mutexPairs.filter((mp) => mp.a !== null && mp.b !== null);
  if (state.mutexPairs.some((mp) => mp.a === null || mp.b === null)) {
    toast("互斥对存在未选择管号的行, 请补全或删除。", true); return;
  }
  if (pairs.length > 12) { toast("互斥试管对最多 12 对。", true); return; }
  const idSet = new Set(state.tubes.map((t) => t.id));
  const pairKeys = new Set();
  for (const mp of pairs) {
    if (!idSet.has(mp.a) || !idSet.has(mp.b)) { toast("互斥对引用了已删除的试管编号。", true); return; }
    if (mp.a === mp.b) { toast(`互斥对 (${mp.a}, ${mp.b}) 无效: 试管不能与自身互斥。`, true); return; }
    const k = mp.a < mp.b ? `${mp.a}-${mp.b}` : `${mp.b}-${mp.a}`;
    if (pairKeys.has(k)) { toast(`互斥试管对 (${k.replace("-", ", ")}) 重复。`, true); return; }
    pairKeys.add(k);
  }
  if (pairs.length) {
    if (state.tubes.length > 7) {
      toast(`含互斥对的请求至多允许 7 支试管, 当前 ${state.tubes.length} 支。`, true); return;
    }
    if (enabled > 10) {
      toast(`含互斥对的请求至多允许 10 个启用槽, 当前 ${enabled} 个; 请禁用部分槽位。`, true); return;
    }
  }

  $("btnPlan").disabled = true;
  $("btnPlan").textContent = "计算中…";
  try {
    const resp = await fetch("/api/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildRequest()),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "计算失败");
    renderResult(data);
    if ($("incremental").checked) $("incremental").checked = false;
    toast(data.id ? `方案已保存 (#${data.id})。` : "计算完成。");
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("btnPlan").disabled = false;
    $("btnPlan").textContent = "计算配平方案";
  }
}
$("btnPlan").onclick = compute;

// ---------------------------------------------------------------------------
// 导出
// ---------------------------------------------------------------------------
$("btnExport").onclick = () => {
  if (!state.record) return;
  const blob = new Blob([JSON.stringify(state.record, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `balance-plan-${state.record.id ?? "draft"}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
};
$("btnCopy").onclick = async () => {
  if (!state.record) return;
  try { await navigator.clipboard.writeText(JSON.stringify(state.record, null, 2)); toast("JSON 已复制到剪贴板。"); }
  catch { toast("复制失败,请使用导出。", true); }
};

// ---------------------------------------------------------------------------
// 历史
// ---------------------------------------------------------------------------
const historySel = new Set();

function syncHistoryButtons() {
  const n = historySel.size;
  $("btnCompare").disabled = n !== 2;
  $("btnBatch").disabled = n < 2 || n > 10;
  $("compareHint").textContent = n === 0
    ? "勾选 2 条可并排比较; 勾选 2~10 条可规划批次换装顺序。"
    : `已选 ${n} 条(比较需恰好 2 条; 批次换装需 2~10 条)。`;
}

$("btnHistory").onclick = async () => {
  historySel.clear();
  syncHistoryButtons();
  $("historyDialog").classList.add("open");
  await renderHistory();
};

async function renderHistory() {
  const resp = await fetch("/api/plans?limit=100");
  const data = await resp.json();
  const box = $("historyList");
  box.innerHTML = "";
  if (!data.items.length) { box.innerHTML = '<p class="muted">暂无保存的方案。</p>'; return; }
  data.items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "history-item" + (historySel.has(it.id) ? " selected" : "");
    const checked = historySel.has(it.id) ? "checked" : "";
    d.innerHTML = `
      <div class="rowline">
        <input type="checkbox" title="加入比较" ${checked}>
        <div>
          <div>#${it.id} ${it.name} ${it.parent_id ? `<span class="small">(派生自 #${it.parent_id})</span>` : ""}</div>
          <div class="small">${new Date(it.created_at).toLocaleString()} ·
            U₁=${fmt(it.u1_mag_g_mm)} g·mm · U₂=${fmt(it.u2_mag_g_mm)} · 评级 ${it.grade} · F=${fmt(it.centrifugal_force_n, 2)}N</div>
        </div>
      </div>
      <div class="rowline">
        <button class="iconbtn" data-a="load">载入</button>
        <button class="iconbtn" data-a="weigh">误差校核</button>
        <button class="iconbtn" data-a="calib">振动校准</button>
        <button class="iconbtn" data-a="loadseq">安全装载</button>
        <button class="iconbtn" data-a="evac">应急迁移</button>
        <button class="iconbtn" data-a="recheck">装管复核</button>
        <button class="iconbtn" data-a="export">导出</button>
        <button class="iconbtn" data-a="del">删除</button>
      </div>`;
    const cb = d.querySelector('input[type="checkbox"]');
    cb.onclick = (e) => e.stopPropagation();
    cb.onchange = () => {
      if (cb.checked) {
        if (historySel.size >= 10) { cb.checked = false; toast("最多选择 10 条方案。", true); return; }
        historySel.add(it.id);
      } else {
        historySel.delete(it.id);
      }
      d.classList.toggle("selected", cb.checked);
      syncHistoryButtons();
    };
    d.querySelector('[data-a="load"]').onclick = () => loadPlan(it.id);
    d.querySelector('[data-a="weigh"]').onclick = () => openWeighingDialog(it.id);
    d.querySelector('[data-a="calib"]').onclick = () => openCalibDialog(it.id);
    d.querySelector('[data-a="loadseq"]').onclick = () => openLoadingDialog(it.id);
    d.querySelector('[data-a="evac"]').onclick = () => openEvacDialog(it.id);
    d.querySelector('[data-a="recheck"]').onclick = () => openRecheckDialog(it.id);
    d.querySelector('[data-a="export"]').onclick = () => { window.location = `/api/plan/${it.id}/export`; };
    d.querySelector('[data-a="del"]').onclick = async () => {
      if (!confirm(`删除方案 #${it.id}?`)) return;
      await fetch(`/api/plan/${it.id}`, { method: "DELETE" });
      historySel.delete(it.id);
      syncHistoryButtons();
      renderHistory();
    };
    box.appendChild(d);
  });
}
$("btnCloseHistory").onclick = () => $("historyDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 方案比较
// ---------------------------------------------------------------------------
function cmpFmt(v, nd) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (typeof v === "string") return v;
  const n = Number(v);
  if (Math.abs(n) >= 1000) return n.toFixed(0);
  return n.toFixed(nd ?? 2);
}

function renderDiffTable(title, rows) {
  if (!rows || !rows.length) return "";
  const body = rows.map((r) => {
    const bTxt = r.change
      ? `${cmpFmt(r.value_b, r.ndigits)}${r.unit ? " " + r.unit : ""} <span class="small">(${r.change})</span>`
      : `${cmpFmt(r.value_b, r.ndigits)}${r.unit ? " " + r.unit : ""}`;
    return `<tr><td>${r.label}</td>
      <td style="text-align:right">${cmpFmt(r.value_a, r.ndigits)}${r.unit ? " " + r.unit : ""}</td>
      <td style="text-align:right">${bTxt}</td></tr>`;
  }).join("");
  return `<div class="diff-group-title">${title}</div>
    <table class="cmp-table"><thead><tr><th>参数</th><th style="text-align:right">方案 A</th><th style="text-align:right">方案 B</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}

function renderCompare(data) {
  const A = data.plan_a, B = data.plan_b, body = $("compareBody");
  const head = `<div class="cmp-grid">
      <div class="cmp-col"><h3>A · #${A.id} ${A.name}</h3>
        <div class="small">${new Date(A.created_at).toLocaleString()} · 评级 <b>${A.grade}</b></div></div>
      <div class="cmp-col"><h3 class="b">B · #${B.id} ${B.name}</h3>
        <div class="small">${new Date(B.created_at).toLocaleString()} · 评级 <b>${B.grade}</b></div></div>
    </div>`;

  const cond = data.same_conditions
    ? '<div class="cmp-ok">两方案的转子几何与求解输入一致, 指标差值可视为<b>同条件</b>下的优化对比。</div>'
    : `<div class="cmp-warn">⚠ 两方案的转子或求解输入不同(<b>${
        data.differing_groups.map((g) => ({ rotor: "转子参数", solver: "求解输入", tubes: "试管清单" }[g])).join("、")
      }</b>), 下列指标差值并非同条件优化结果, 请勿直接据差值评判优劣; 参数差异见下。</div>`;

  const metricRows = data.metrics.map((m) => {
    let dTxt = '<span class="delta-flat">–</span>';
    if (m.delta_b_minus_a !== null && m.delta_b_minus_a !== undefined) {
      const d = m.delta_b_minus_a;
      if (Math.abs(d) < 1e-9) {
        dTxt = '<span class="delta-flat">0</span>';
      } else {
        const cls = d > 0 ? "delta-up" : "delta-down";
        const sign = d > 0 ? "+" : "−";
        dTxt = `<span class="${cls}">${sign}${cmpFmt(Math.abs(d), m.ndigits)}${m.unit ? " " + m.unit : ""}</span>`;
      }
    }
    return `<tr><td>${m.label}</td>
      <td style="text-align:right">${cmpFmt(m.value_a, m.ndigits)}${m.unit ? " " + m.unit : ""}</td>
      <td style="text-align:right">${cmpFmt(m.value_b, m.ndigits)}${m.unit ? " " + m.unit : ""}</td>
      <td style="text-align:right">${dTxt}</td></tr>`;
  }).join("");
  const metrics = `<h2 style="font-size:13px;margin:12px 0 6px">一阶 / 二阶不平衡量与力学指标(B − A)</h2>
    <table class="cmp-table">
      <thead><tr><th>指标</th><th style="text-align:right">方案 A</th><th style="text-align:right">方案 B</th><th style="text-align:right">差值</th></tr></thead>
      <tbody>${metricRows}</tbody></table>
    <div class="small" style="margin-top:4px">方向类差值为按角度环绕归一化到 ±180° 的变化量; 其余指标差值为 B 减 A。</div>`;

  const inputDiff = data.same_conditions ? "" : (
    `<h2 style="font-size:13px;margin:12px 0 4px">参数差异</h2>` +
    renderDiffTable("转子几何(槽位 / 半径)", data.input_diff.rotor) +
    renderDiffTable("求解输入(转速 / 权重 / 搜索参数)", data.input_diff.solver) +
    renderDiffTable("试管清单(增删 / 质量 / 锁定)", data.input_diff.tubes)
  );

  const stMap = {
    added: ['新增', 'st-added'], removed: ['移除', 'st-removed'],
    moved: ['换槽', 'st-moved'], unchanged: ['未变', 'st-unchanged'],
  };
  const c = data.placement_counts;
  const plRows = data.placements.map((r) => {
    const [stTxt, stCls] = stMap[r.status];
    const lock = (la, lb) => (la || lb ? ` <span class="pill lock">锁</span>` : "");
    const slot = (s, ang) => s === null || s === undefined
      ? '<span class="small">未放置</span>'
      : `${s} <span class="small">(${cmpFmt(ang, 1)}°)</span>`;
    return `<tr>
      <td>${r.tube_id}${lock(r.locked_a, r.locked_b)}</td>
      <td>${cmpFmt(r.mass_a ?? r.mass_b, 1)}</td>
      <td>${slot(r.slot_a, r.angle_a)}</td>
      <td>${slot(r.slot_b, r.angle_b)}</td>
      <td><span class="status-tag ${stCls}">${stTxt}</span>${r.status === "moved" ? ` <span class="small">${r.slot_a}→${r.slot_b}</span>` : ""}</td>
    </tr>`;
  }).join("");
  const placements = `<h2 style="font-size:13px;margin:12px 0 6px">摆放明细(按试管编号合并)
      <span class="small">新增 ${c.added} · 移除 ${c.removed} · 换槽 ${c.moved} · 未变 ${c.unchanged}</span></h2>
    <div class="legend" style="justify-content:flex-start">
      <span><i style="background:var(--acc2)"></i>新增(B 有 A 无)</span>
      <span><i style="background:var(--bad)"></i>移除(A 有 B 无)</span>
      <span><i style="background:var(--warn)"></i>换槽(编号相同槽位不同)</span>
      <span><i style="background:var(--mut)"></i>未变</span>
    </div>
    <table class="cmp-table">
      <thead><tr><th>试管</th><th>质量(g)</th><th>A 槽位</th><th>B 槽位</th><th>状态</th></tr></thead>
      <tbody>${plRows}</tbody></table>`;

  body.innerHTML = head + cond + metrics + inputDiff + placements;
}

$("btnCompare").onclick = async () => {
  if (historySel.size !== 2) return;
  const [a, b] = [...historySel];
  try {
    const resp = await fetch(`/api/plans/compare?a=${a}&b=${b}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "比较失败");
    renderCompare(data);
    $("compareDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
};
$("btnCloseCompare").onclick = () => $("compareDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 批次换装顺序(只读, 基于 2~10 条已保存方案)
// ---------------------------------------------------------------------------
$("btnBatch").onclick = async () => {
  const n = historySel.size;
  if (n < 2 || n > 10) return;
  try {
    const resp = await fetch("/api/plans/batch-sequence", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_ids: [...historySel] }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "批次换装规划失败");
    renderBatch(data);
    $("batchDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
};
$("btnCloseBatch").onclick = () => $("batchDialog").classList.remove("open");

function renderBatch(d) {
  const body = $("batchBody");
  const tie = d.optimal_order_count > 1
    ? `达到最小总操作数的执行次序共 <b>${d.optimal_order_count}</b> 条, 已按方案 ID 序列升序裁决。`
    : "达到最小总操作数的执行次序唯一。";
  const head = `<div class="cmp-ok">从<b>空转子</b>开始执行 <b>${d.plan_count}</b> 个批次,
      总操作数 <b>${d.total_operations}</b> 次(每卸下或装入一支试管各计 1 次)。</div>
    <div class="wc-note">${tie} 转子: R=${fmt(d.rotor.radius_mm, 1)} mm · ${d.rotor.slot_count} 槽。</div>`;
  const orderLine = `<div class="rowline" style="flex-wrap:wrap;gap:6px;margin:4px 0 8px">
    <span class="small">执行次序:</span>
    ${d.plans.map((p, i) => `${i ? '<span class="muted">→</span>' : ""}
      <span class="badge">#${p.id} ${p.name}(${p.tube_count} 管)</span>`).join("")}
    </div>`;
  const tubeTable = (rows) => `
      <table class="cmp-table">
        <thead><tr><th>试管</th><th>质量(g)</th><th>槽位</th></tr></thead>
        <tbody>${rows.map((t) => `<tr><td>${t.tube_id}</td><td>${t.mass}</td>
          <td>${t.slot} <span class="small">(${t.angle === null || t.angle === undefined ? "–" : fmt(t.angle, 1) + "°"})</span></td></tr>`).join("")}</tbody>
      </table>`;
  const batches = d.batches.map((b) => {
    const unloadHtml = b.unload.length
      ? tubeTable(b.unload)
      : `<p class="muted">${b.seq === 1 ? "空转子起步, 无需卸下。" : "无(上一批次的试管全部保留)。"}</p>`;
    const loadHtml = b.load.length ? tubeTable(b.load) : '<p class="muted">无需装入。</p>';
    const kept = b.kept_count
      ? `<div class="small" style="margin-top:5px">保留不动 ${b.kept_count} 支(不计操作):
          ${b.kept.map((t) => `试管 ${t.tube_id}@槽 ${t.slot}`).join("、")}</div>`
      : "";
    return `<div class="reason" style="border-left-color:var(--acc2)">
      <div class="rhead">批次 ${b.seq} · 方案 #${b.plan_id} ${b.plan_name}
        <span class="badge">本次 ${b.operations} 次(卸 ${b.unload_count} + 装 ${b.load_count})</span>
        <span class="badge">累计 ${b.cumulative_operations} / ${d.total_operations}</span></div>
      <div class="cmp-grid">
        <div class="cmp-col"><h3>① 卸下清单(${b.unload_count})</h3>${unloadHtml}</div>
        <div class="cmp-col"><h3 class="b">② 装入清单(${b.load_count})</h3>${loadHtml}</div>
      </div>
      ${kept}
    </div>`;
  }).join("");
  body.innerHTML = head + orderLine + batches +
    `<div class="small" style="margin-top:6px">${d.method}。${d.note}</div>`;
}

async function loadPlan(id) {
  const resp = await fetch(`/api/plan/${id}`);
  if (!resp.ok) { toast("载入失败。", true); return; }
  const data = await resp.json();
  const q = data.request;
  $("planName").value = data.name;
  state.slots = q.slots.map((s) => ({ ...s }));
  state.tubes = q.tubes.map((t) => ({ ...t }));
  state.mutexPairs = (q.mutex_pairs || []).map((p) =>
    Array.isArray(p) ? { a: p[0], b: p[1] } : { a: p.tube_a ?? p.a, b: p.tube_b ?? p.b });
  state.nextTubeId = Math.max(0, ...state.tubes.map((t) => t.id)) + 1;
  $("slotCount").value = q.slot_count;
  $("radius").value = q.radius_mm; $("rpm").value = q.rpm;
  $("w1").value = q.w1; $("w2").value = q.w2;
  $("moveWeight").value = q.move_weight;
  $("restarts").value = q.restarts; $("chainSteps").value = q.chain_steps;
  renderSlotEditor(); renderTubes(); renderMutexPairs();
  renderResult({ id: data.id, name: data.name, request: q, result: data.result });
  $("historyDialog").classList.remove("open");
  toast(`已载入方案 #${id}。`);
}

// ---------------------------------------------------------------------------
// 称重误差校核(只读, 基于已保存方案)
// ---------------------------------------------------------------------------
const wcState = { planId: null, plan: null, rows: [] };

async function openWeighingDialog(id) {
  $("btnWeighing").blur();
  try {
    const resp = await fetch(`/api/plan/${id}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "方案读取失败");
    wcState.planId = id;
    wcState.plan = data;
    wcState.rows = data.result.placements
      .filter((p) => p.slot !== null && p.slot !== undefined)
      .map((p) => ({
        tube_id: p.tube_id, slot: p.slot, angle: p.angle,
        mass: p.mass, err: 0,
      }))
      .sort((a, b) => (a.slot - b.slot) || (a.tube_id - b.tube_id));
    $("weighingMeta").innerHTML =
      `方案 <b>#${data.id} ${data.name}</b> · ${new Date(data.created_at).toLocaleString()} ·
       R=${data.request.radius_mm}mm ·
       ${data.result.config.rpm} rpm · ${wcState.rows.length} 支已放置试管 ·
       标称 |U₁|=${fmt(data.result.metrics.u1_mag_g_mm)} g·mm`;
    $("wcLimit").value = "";
    $("wcUniform").value = "";
    $("wcResult").innerHTML = "";
    renderWcTubes();
    $("weighingDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
}

function renderWcTubes() {
  const tb = $("wcTubeBody");
  tb.innerHTML = "";
  wcState.rows.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.tube_id}</td>
      <td>${r.slot} <span class="small">(${fmt(r.angle, 1)}°)</span></td>
      <td>${r.mass}</td>
      <td><input type="number" min="0" max="${r.mass}" step="any" value="${r.err}"
           data-tid="${r.tube_id}" style="width:110px"></td>
      <td class="small" data-range="${r.tube_id}">${fmt(r.mass - r.err, 3)} ~ ${fmt(r.mass + r.err, 3)}</td>`;
    tr.querySelector("input").oninput = (e) => {
      let v = parseFloat(e.target.value);
      if (isNaN(v) || v < 0) v = 0;
      if (v > r.mass) { v = r.mass; e.target.value = v; }
      r.err = v;
      tr.querySelector(`[data-range="${r.tube_id}"]`).textContent =
        `${fmt(r.mass - v, 3)} ~ ${fmt(r.mass + v, 3)}`;
    };
    tb.appendChild(tr);
  });
}

$("btnWcApplyUniform").onclick = () => {
  const v = parseFloat($("wcUniform").value);
  if (isNaN(v) || v < 0) { toast("请输入非负的统一误差上界。", true); return; }
  let clamped = false;
  wcState.rows.forEach((r) => {
    if (v > r.mass) { r.err = r.mass; clamped = true; }
    else r.err = v;
  });
  renderWcTubes();
  if (clamped) toast("部分试管误差上界已截断到其标称质量。");
};

$("btnWcRun").onclick = async () => {
  const btn = $("btnWcRun");
  btn.disabled = true;
  try {
    const limitRaw = $("wcLimit").value;
    const body = {
      tube_errors: wcState.rows
        .filter((r) => r.err > 0)
        .map((r) => ({ tube_id: r.tube_id, mass_error_bound_g: r.err })),
    };
    if (limitRaw !== "") {
      const lim = parseFloat(limitRaw);
      if (isNaN(lim) || lim < 0) { toast("限值必须为非负数, 或留空。", true); return; }
      body.u1_limit_g_mm = lim;
    }
    const resp = await fetch(`/api/plan/${wcState.planId}/weighing-check`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "校核失败");
    renderWcResult(data);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function renderWcResult(d) {
  const box = $("wcResult");
  const n = d.nominal, w = d.worst_case;
  let verdict;
  if (d.within_limit === true) {
    verdict = `<div class="wc-ok">✔ <b>合格</b>: 最不利 |U₁|=${fmt(w.u1_mag_g_mm)} g·mm
      ≤ 限值 ${fmt(d.u1_limit_g_mm)} g·mm, 余量 ${fmt(d.margin_g_mm)} g·mm。
      即使各管称重误差同时取到最不利方向, 一阶不平衡量仍不超限。</div>`;
  } else if (d.within_limit === false) {
    const pct = d.u1_limit_g_mm > 0
      ? ` (约 ${((w.u1_mag_g_mm / d.u1_limit_g_mm - 1) * 100).toFixed(1)}%)`
      : "";
    verdict = `<div class="wc-bad">✘ <b>超限</b>: 最不利 |U₁|=${fmt(w.u1_mag_g_mm)} g·mm
      &gt; 限值 ${fmt(d.u1_limit_g_mm)} g·mm, 超出 ${fmt(d.excess_g_mm)} g·mm${pct}。
      按称重误差区间判定不合格, 需提高称量精度或重新配平。</div>`;
  } else {
    verdict = `<div class="wc-note">未填写限值, 仅给出最不利 |U₁|=${fmt(w.u1_mag_g_mm)}
      g·mm, 不做合格判定。</div>`;
  }

  const naiveRow = d.naive_equals_exact
    ? `<span class="small">本例各误差贡献在最不利方向上同向, 朴素标量估计恰好等于精确值。</span>`
    : `<span class="wc-hi">朴素标量估计 ${fmt(d.naive_abs_worst_g_mm)} g·mm 严格高估</span>
       <span class="small">(|U₁标称| ${fmt(n.u1_mag_g_mm)} + R·Σe ${fmt(d.abs_dev_bound_g_mm)}),
       误差是<b>向量叠加</b>, 不能把各管误差绝对值直接相加。</span>`;

  const rows = d.tubes.map((t) => {
    const signTxt = t.delta_g > 1e-12
      ? `<span class="wc-pos">+${fmt(t.delta_g, 3)}</span>`
      : t.delta_g < -1e-12
        ? `<span class="wc-neg">${fmt(t.delta_g, 3)}</span>`
        : "0";
    return `<tr>
      <td>${t.tube_id}</td>
      <td>${t.slot} <span class="small">(${fmt(t.angle_deg, 1)}°)</span></td>
      <td>${fmt(t.nominal_mass_g, 3)}</td>
      <td>±${fmt(t.mass_error_bound_g, 3)}</td>
      <td>${signTxt}</td>
      <td><b>${fmt(t.actual_mass_g, 3)}</b></td>
      <td>${fmt(t.u1_along_worst_g_mm, 1)}</td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    ${verdict}
    <table class="cmp-table">
      <thead><tr><th>指标</th><th style="text-align:right">标称值</th>
        <th style="text-align:right">最不利值</th><th style="text-align:right">增量</th></tr></thead>
      <tbody>
        <tr><td>一阶不平衡量 |U₁| (g·mm)</td>
          <td style="text-align:right">${fmt(n.u1_mag_g_mm)}</td>
          <td style="text-align:right" class="wc-hi">${fmt(w.u1_mag_g_mm)}</td>
          <td style="text-align:right">+${fmt(d.worst_minus_nominal_g_mm)}</td></tr>
        <tr><td>U₁ 方向 (°)</td>
          <td style="text-align:right">${fmt(n.u1_angle_deg, 1)}</td>
          <td style="text-align:right">${fmt(w.u1_angle_deg, 1)}</td>
          <td style="text-align:right" class="small">可能偏转</td></tr>
        <tr><td>对应离心合力 F (N, ${d.rpm} rpm)</td>
          <td style="text-align:right">${fmt(n.centrifugal_force_n, 3)}</td>
          <td style="text-align:right">${fmt(w.centrifugal_force_n, 3)}</td>
          <td style="text-align:right">+${fmt(w.centrifugal_force_n - n.centrifugal_force_n, 3)}</td></tr>
      </tbody>
    </table>
    <div class="rowline" style="margin:7px 0;gap:10px;flex-wrap:wrap">
      ${naiveRow}
    </div>
    <h2 style="font-size:13px;margin:10px 0 6px">达到最不利 |U₁| 的一组实际质量
      <span class="small">(各管误差独立取 +e/-e; 末列为该管误差沿最不利 U₁ 方向的投影 g·mm)</span></h2>
    <table class="cmp-table wc-table">
      <thead><tr><th>试管</th><th>槽位/角度</th><th>标称(g)</th><th>误差上界</th>
        <th>误差取值 δ</th><th>实际质量(g)</th><th>沿 U₁ 投影</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="small" style="margin-top:6px">算法: ${d.method}; 枚举凸多边形顶点数 ≤ ${d.vertex_count}。
      本次校核为只读结果, 方案 #${d.plan.id} 未被修改。</div>`;
}

$("btnWeighing").onclick = () => {
  if (state.record && state.record.id !== null && state.record.id !== undefined) {
    openWeighingDialog(state.record.id);
  } else {
    toast("请先从历史方案载入一条已保存方案, 或先计算并保存方案。", true);
  }
};
$("btnCloseWeighing").onclick = () => $("weighingDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 实测振动校准(只读, 影响系数法; 试重视为已取下)
// ---------------------------------------------------------------------------
const calibState = { planId: null, freeSlots: [] };

async function openCalibDialog(id) {
  $("btnCalib")?.blur();
  try {
    const resp = await fetch(`/api/plan/${id}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "方案读取失败");
    calibState.planId = id;
    const occupied = new Set(
      data.result.placements
        .filter((p) => p.slot !== null && p.slot !== undefined)
        .map((p) => p.slot));
    calibState.freeSlots = data.result.slot_state
      .filter((s) => s.enabled && !occupied.has(s.slot))
      .map((s) => ({ slot: s.slot, angle: s.angle }))
      .sort((a, b) => a.slot - b.slot);
    if (!calibState.freeSlots.length) {
      toast("该方案没有空闲启用槽位, 无法放置试重/配重。", true);
      return;
    }
    $("calibMeta").innerHTML =
      `方案 <b>#${data.id} ${data.name}</b> · ${new Date(data.created_at).toLocaleString()} ·
       R=${data.request.radius_mm}mm · ${data.result.config.rpm} rpm ·
       空闲启用槽 ${calibState.freeSlots.length} 个:
       ${calibState.freeSlots.map((s) => `#${s.slot}(${fmt(s.angle, 0)}°)`).join("、")}`;
    const sel = $("cbTrialSlot");
    sel.innerHTML = calibState.freeSlots
      .map((s) => `<option value="${s.slot}">#${s.slot} (${fmt(s.angle, 1)}°)</option>`)
      .join("");
    ["cbA0", "cbP0", "cbA1", "cbP1", "cbTrialMass"].forEach((x) => ($(x).value = ""));
    $("cbP0").value = "0";
    $("calibResult").innerHTML = "";
    $("calibDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
}

function readNonNeg(id, label) {
  const v = parseFloat($(id).value);
  if (Number.isNaN(v) || !Number.isFinite(v) || v < 0) {
    throw new Error(`${label}必须为有限的非负数。`);
  }
  return v;
}

$("btnCbRun").onclick = async () => {
  const btn = $("btnCbRun");
  btn.disabled = true;
  try {
    const a0 = readNonNeg("cbA0", "试重前幅值");
    const p0 = readNonNeg("cbP0", "试重前相位");
    const a1 = readNonNeg("cbA1", "试重后幅值");
    const p1 = readNonNeg("cbP1", "试重后相位");
    const mt = parseFloat($("cbTrialMass").value);
    if (!(mt > 0) || !Number.isFinite(mt)) {
      toast("试重质量必须为大于零的有限数。", true); return;
    }
    const masses = $("cbMasses").value.split(/[\s,;，；]+/)
      .map((s) => s.trim()).filter(Boolean).map(Number);
    if (!masses.length) { toast("请至少填写一个可用配重质量。", true); return; }
    if (masses.some((m) => !(m > 0) || !Number.isFinite(m))) {
      toast("可用配重质量都必须为大于零的有限数。", true); return;
    }
    const body = {
      initial_amplitude: a0, initial_phase_deg: p0,
      trial_amplitude: a1, trial_phase_deg: p1,
      trial_mass_g: mt,
      trial_slot: parseInt($("cbTrialSlot").value, 10),
      available_masses: masses,
    };
    const resp = await fetch(`/api/plan/${calibState.planId}/vibration-calibration`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "校准计算失败");
    renderCalibResult(data);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function renderCalibResult(d) {
  const box = $("calibResult");
  const ic = d.influence_coefficient, rd = d.readings, best = d.best;
  const impTxt = best.improvement_pct === null
    ? "原读数为 0, 改善百分比不适用"
    : `较原读数 <b class="${best.improvement >= 0 ? "delta-down" : "delta-up"}">
        ${best.improvement >= 0 ? "下降" : "上升"}
        ${fmt(Math.abs(best.improvement))} (${fmt(Math.abs(best.improvement_pct), 1)}%)</b>`;
  const top = `<div class="wc-ok">✔ 推荐: <b>槽位 #${best.slot}
      (${fmt(best.angle_deg, 1)}°)</b> 放置单枚配重
      <b>${fmt(best.mass_g, 3)} g</b> → 预测残余幅值 <b>${fmt(best.predicted.amplitude)}</b>
      @ ${fmt(best.predicted.phase_deg, 1)}° (原读数 ${fmt(rd.initial.amplitude)}
      @ ${fmt(rd.initial.phase_deg, 1)}°), ${impTxt}。</div>`;

  const icRow = `<table class="cmp-table">
    <thead><tr><th>项目</th><th style="text-align:right">实部 x</th>
      <th style="text-align:right">虚部 y</th><th style="text-align:right">幅值</th>
      <th style="text-align:right">相位/方向</th></tr></thead>
    <tbody>
      <tr><td>试重前读数 V₀</td>
        <td style="text-align:right">${fmt(rd.initial.x)}</td>
        <td style="text-align:right">${fmt(rd.initial.y)}</td>
        <td style="text-align:right">${fmt(rd.initial.amplitude)}</td>
        <td style="text-align:right">${fmt(rd.initial.phase_deg, 1)}°</td></tr>
      <tr><td>装试重后读数 V₁</td>
        <td style="text-align:right">${fmt(rd.with_trial.x)}</td>
        <td style="text-align:right">${fmt(rd.with_trial.y)}</td>
        <td style="text-align:right">${fmt(rd.with_trial.amplitude)}</td>
        <td style="text-align:right">${fmt(rd.with_trial.phase_deg, 1)}°</td></tr>
      <tr><td>振动差 V₁−V₀</td>
        <td style="text-align:right">${fmt(rd.delta.x)}</td>
        <td style="text-align:right">${fmt(rd.delta.y)}</td>
        <td style="text-align:right">${fmt(rd.delta.amplitude)}</td>
        <td style="text-align:right">${fmt(rd.delta.phase_deg, 1)}°</td></tr>
      <tr><td>影响系数 α</td>
        <td style="text-align:right">${ic.x.toExponential(4)}</td>
        <td style="text-align:right">${ic.y.toExponential(4)}</td>
        <td style="text-align:right">${ic.magnitude.toExponential(4)}</td>
        <td style="text-align:right">${fmt(ic.phase_deg, 1)}°</td></tr>
    </tbody></table>
    <div class="small" style="margin:5px 0">
      α=(V₁−V₀)/(m_t·R·e^{iθ_t}), 试重 ${fmt(d.trial.mass_g, 3)} g @ 槽
      #${d.trial.slot} (${fmt(d.trial.angle_deg, 1)}°), 试重质径积
      ${fmt(d.trial.unbalance_g_mm)} g·mm;
      连续意义理想配重: ${fmt(d.ideal_correction.equivalent_mass_g, 3)} g
      @ ${fmt(d.ideal_correction.direction_deg, 1)}°
      (|U*|=${fmt(d.ideal_correction.unbalance_g_mm)} g·mm, 不受槽位/离散质量限制, 仅供对照)。
    </div>`;

  const rows = d.candidates.map((c) => {
    const better = c.improvement >= 0;
    const pct = c.improvement_pct === null ? "–"
      : `${c.improvement_pct >= 0 ? "−" : "+"}${fmt(Math.abs(c.improvement_pct), 1)}%`;
    return `<tr${c.rank === 1 ? ' style="background:#10331f55"' : ""}>
      <td>${c.rank === 1 ? '<b>★ ' : ""}${c.rank}${c.rank === 1 ? "</b>" : ""}</td>
      <td>#${c.slot} <span class="small">(${fmt(c.angle_deg, 1)}°)</span></td>
      <td>${fmt(c.mass_g, 3)}</td>
      <td style="text-align:right"><b>${fmt(c.predicted.amplitude)}</b></td>
      <td style="text-align:right">${fmt(c.predicted.phase_deg, 1)}°</td>
      <td style="text-align:right" class="${better ? "delta-down" : "delta-up"}">
        ${better ? "−" : "+"}${fmt(Math.abs(c.improvement))}</td>
      <td style="text-align:right" class="${better ? "delta-down" : "delta-up"}">${pct}</td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    ${top}
    ${icRow}
    <h2 style="font-size:13px;margin:10px 0 6px">逐空闲启用槽位 × 可用配重: 单枚配重预测
      <span class="small">共 ${d.candidate_count} 种, 按残余幅值升序</span></h2>
    <table class="cmp-table wc-table">
      <thead><tr><th>排序</th><th>槽位 / 角度</th><th>配重(g)</th>
        <th style="text-align:right">预测幅值</th><th style="text-align:right">预测相位</th>
        <th style="text-align:right">幅值改善</th><th style="text-align:right">改善比例</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="small" style="margin-top:6px">${d.note} 方案 #${d.plan.id} 未被修改。</div>`;
}

$("btnCalib").onclick = () => {
  if (state.record && state.record.id !== null && state.record.id !== undefined) {
    openCalibDialog(state.record.id);
  } else {
    toast("请先从历史方案载入一条已保存方案, 或先计算并保存方案。", true);
  }
};
$("btnCloseCalib").onclick = () => $("calibDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 逐管安全装载(只读, 基于已保存方案; 子集 DP 精确求最小峰值次序)
// ---------------------------------------------------------------------------
const ldState = { planId: null, tubeCount: 0 };

async function openLoadingDialog(id) {
  $("btnLoading")?.blur();
  try {
    const resp = await fetch(`/api/plan/${id}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "方案读取失败");
    ldState.planId = id;
    ldState.tubeCount = data.result.placements
      .filter((p) => p.slot !== null && p.slot !== undefined).length;
    const tooMany = ldState.tubeCount > 16;
    $("loadingMeta").innerHTML =
      `方案 <b>#${data.id} ${data.name}</b> · ${new Date(data.created_at).toLocaleString()} ·
       R=${data.request.radius_mm}mm · ${ldState.tubeCount} 支已放置试管 ·
       标称 |U₁|=${fmt(data.result.metrics.u1_mag_g_mm)} g·mm` +
      (tooMany ? ' · <span class="wc-hi">超过 16 支上限, 服务端将拒绝规划</span>' : "");
    $("ldLimit").value = "";
    $("loadingResult").innerHTML = "";
    $("btnLdRun").disabled = tooMany || ldState.tubeCount === 0;
    $("loadingDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
}

$("btnLdRun").onclick = async () => {
  const btn = $("btnLdRun");
  btn.disabled = true;
  try {
    const body = {};
    const limitRaw = $("ldLimit").value;
    if (limitRaw !== "") {
      const lim = parseFloat(limitRaw);
      if (isNaN(lim) || lim < 0) { toast("限值必须为非负数, 或留空。", true); return; }
      body.u1_limit_g_mm = lim;
    }
    const resp = await fetch(`/api/plan/${ldState.planId}/loading-sequence`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "装载次序规划失败");
    renderLoadingResult(data);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function renderLoadingResult(d) {
  const box = $("loadingResult");
  let verdict;
  if (d.within_limit === true) {
    verdict = `<div class="wc-ok">✔ <b>合格</b>: 最优装载次序的全过程峰值 |U₁|=${fmt(d.peak_u1_g_mm)} g·mm
      ≤ 装载阶段限值 ${fmt(d.u1_limit_g_mm)} g·mm, 余量 ${fmt(d.margin_g_mm)} g·mm。</div>`;
  } else if (d.within_limit === false) {
    verdict = `<div class="wc-bad">✘ <b>超限</b>: 即使按最优次序装载, 全过程峰值 |U₁|=${fmt(d.peak_u1_g_mm)} g·mm
      仍超过限值 ${fmt(d.u1_limit_g_mm)} g·mm, 超出 ${fmt(d.excess_g_mm)} g·mm;
      超限步骤: 第 ${d.exceeded_steps.join("、")} 步(共 ${d.exceeded_count} 步)。</div>`;
  } else {
    verdict = `<div class="wc-note">未填写限值, 仅给出最优装载次序
      (全过程峰值 |U₁|=${fmt(d.peak_u1_g_mm)} g·mm), 不做合格判定。</div>`;
  }
  const tie = d.optimal_order_count > 1
    ? `达到最小峰值的装载次序共 <b>${d.optimal_order_count}</b> 条, 已按试管编号序列升序裁决。`
    : "达到最小峰值的装载次序唯一。";
  const orderLine = `<div class="rowline" style="flex-wrap:wrap;gap:6px;margin:6px 0">
    <span class="small">装载次序:</span>
    ${d.order.map((t, i) => `${i ? '<span class="muted">→</span>' : ""}<span class="badge">试管 ${t}</span>`).join("")}
    </div>`;
  const rows = d.steps.map((s) => {
    const over = s.exceeds_limit === true;
    const status = d.u1_limit_g_mm === null
      ? '<span class="small">–</span>'
      : over ? '<span class="delta-up">超限</span>' : '<span class="delta-down">正常</span>';
    return `<tr${over ? ' style="background:#3d141455"' : ""}>
      <td>${s.seq}</td>
      <td><b>${s.tube_id}</b></td>
      <td>${s.slot} <span class="small">(${fmt(s.angle_deg, 1)}°)</span></td>
      <td>${fmt(s.mass_g, 3)}</td>
      <td style="text-align:right">${fmt(s.u1_mag_g_mm)}</td>
      <td style="text-align:right"><b>${fmt(s.peak_so_far_g_mm)}</b></td>
      <td>${status}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    ${verdict}
    <div class="wc-note">${tie} 最终状态 |U₁|=${fmt(d.final_u1_g_mm)} g·mm(与方案标称一致);
      转子半径 R=${fmt(d.radius_mm, 1)} mm。</div>
    ${orderLine}
    <table class="cmp-table wc-table">
      <thead><tr><th>步序</th><th>装入试管</th><th>槽位/角度</th><th>质量(g)</th>
        <th style="text-align:right">当步 |U₁| (g·mm)</th>
        <th style="text-align:right">累计峰值 (g·mm)</th><th>限值判定</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="small" style="margin-top:6px">算法: ${d.method}。${d.note} 方案 #${d.plan.id} 未被修改。</div>`;
}

$("btnLoading").onclick = () => {
  if (state.record && state.record.id !== null && state.record.id !== undefined) {
    openLoadingDialog(state.record.id);
  } else {
    toast("请先从历史方案载入一条已保存方案, 或先计算并保存方案。", true);
  }
};
$("btnCloseLoading").onclick = () => $("loadingDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 失效槽位应急迁移(只读, 基于已保存方案; 按移动数分层精确枚举)
// ---------------------------------------------------------------------------
const evacState = { planId: null, tubeCount: 0, enabledCount: 0 };

async function openEvacDialog(id) {
  $("btnEvac")?.blur();
  try {
    const resp = await fetch(`/api/plan/${id}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "方案读取失败");
    evacState.planId = id;
    evacState.tubeCount = data.result.placements
      .filter((p) => p.slot !== null && p.slot !== undefined).length;
    evacState.enabledCount = data.result.slot_state.filter((s) => s.enabled).length;
    const tooMany = evacState.tubeCount > 10 || evacState.enabledCount > 16;
    $("evacMeta").innerHTML =
      `方案 <b>#${data.id} ${data.name}</b> · ${new Date(data.created_at).toLocaleString()} ·
       R=${data.request.radius_mm}mm · ${data.result.config.rpm} rpm ·
       ${evacState.tubeCount} 支已放置试管 / ${evacState.enabledCount} 个启用槽 ·
       原 |U₁|=${fmt(data.result.metrics.u1_mag_g_mm)} g·mm` +
      (tooMany ? ' · <span class="wc-hi">超过 10 支管或 16 个启用槽上限, 服务端将拒绝</span>' : "");
    $("evFailed").value = "";
    $("evLimit").value = "";
    $("evMaxMoves").value = "3";
    $("evacResult").innerHTML = "";
    $("btnEvRun").disabled = tooMany || evacState.tubeCount === 0;
    $("evacDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
}

$("btnEvRun").onclick = async () => {
  const btn = $("btnEvRun");
  btn.disabled = true;
  try {
    const failed = $("evFailed").value.split(/[\s,;，；]+/)
      .map((s) => s.trim()).filter(Boolean).map((s) => {
        const v = Number(s);
        if (!Number.isInteger(v) || v < 0) throw new Error("失效槽位必须为非负整数槽号。");
        return v;
      });
    if (!failed.length) { toast("请至少填写一个新失效槽位。", true); return; }
    const limit = parseFloat($("evLimit").value);
    if (!(limit >= 0) || !Number.isFinite(limit)) {
      toast("一阶不平衡量限值必须为非负有限数。", true); return;
    }
    const maxMoves = parseInt($("evMaxMoves").value, 10);
    if (![0, 1, 2, 3].includes(maxMoves)) {
      toast("最多可移动管数必须为 0~3 的整数。", true); return;
    }
    const resp = await fetch(`/api/plan/${evacState.planId}/slot-evacuation`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        failed_slots: failed,
        u1_limit_g_mm: limit,
        max_moves: maxMoves,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "迁移规划失败");
    renderEvacResult(data);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function renderEvacResult(d) {
  const box = $("evacResult");
  const failedTxt = d.failed_slots.map((f) =>
    `#${f.slot}(${fmt(f.angle_deg, 0)}°)` +
    (f.occupied_by_tube === null || f.occupied_by_tube === undefined
      ? '<span class="small">空</span>'
      : `<span class="small">${f.locked ? "🔒锁定管" : "管"} ${f.occupied_by_tube}</span>`)
  ).join("、");

  let verdict;
  if (d.feasible) {
    verdict = `<div class="wc-ok">✔ <b>存在可行迁移</b>: 最少移动 <b>${d.moves_required}</b> 支试管即可满足限值 —
      最终 |U₁|=<b>${fmt(d.final_u1.u1_mag_g_mm)}</b> g·mm ≤ 限值 ${fmt(d.u1_limit_g_mm)} g·mm,
      余量 ${fmt(d.margin_g_mm)} g·mm(迁移前 |U₁|=${fmt(d.original_u1.u1_mag_g_mm)} g·mm)。
      ${d.tie_broken ? `达到该最优结果的摆放共 ${d.optimal_arrangement_count} 种, 已按管号升序的目标槽位序列裁决。` : "最优摆放唯一。"}</div>`;
  } else {
    verdict = `<div class="wc-bad">✘ <b>无可行摆放</b>: ${d.reason}</div>`;
  }

  const head = `<div class="wc-note">失效槽位: ${failedTxt} · 必须迁移 ${d.forced_move_count} 支(失效槽上非锁定管) ·
    移动上限 ${d.max_moves} · 精确评估合法摆放 ${d.evaluated_arrangements} 种 ·
    有效目标槽 ${d.usable_target_slots.length ? d.usable_target_slots.map((s) => `#${s}`).join("、") : "无"}。
    ${d.locked_on_failed_slots.length
      ? `<br><span class="wc-hi">锁定试管在失效槽上: ${d.locked_on_failed_slots.map((t) => `管 ${t.tube_id}@槽 ${t.slot}`).join("、")} —— 锁定管不得移动, 结构上不可行。</span>`
      : ""}</div>`;

  // 展示对象: 可行时用最终摆放; 不可行且有对照摆放时用对照摆放; 都没有则展示原槽
  const rows = d.tubes.length ? d.tubes : [];
  const movesSummary = d.moves.length
    ? `<div class="rowline" style="flex-wrap:wrap;gap:6px;margin:6px 0">
         <span class="small">移动清单(${d.moves.length}):</span>
         ${d.moves.map((m) =>
           `<span class="badge">管 ${m.tube_id}: 槽 ${m.from_slot} → 槽 ${m.to_slot}
             <span class="small">(${fmt(m.from_angle_deg, 0)}°→${fmt(m.to_angle_deg, 0)}°)</span></span>`
         ).join("")}
       </div>`
    : "";
  const tableTitle = d.feasible
    ? "逐管原槽 / 目标槽(只读建议, 不覆盖原方案)"
    : d.best_effort
      ? "对照摆放: 移动上限内 |U₁| 最小的摆放(仍不满足限值, 仅供参考)"
      : "原摆放明细(不存在目标摆放)";

  let finalBlock = "";
  if (d.final_u1) {
    finalBlock = `<div class="rowline" style="gap:14px;flex-wrap:wrap;margin:6px 0;font-size:12.5px">
      <span>迁移前 |U₁|=<b>${fmt(d.original_u1.u1_mag_g_mm)}</b> g·mm @ ${fmt(d.original_u1.u1_angle_deg, 1)}°</span>
      <span>迁移后 |U₁|=<b class="${d.feasible ? "delta-down" : "delta-up"}">${fmt(d.final_u1.u1_mag_g_mm)}</b> g·mm @ ${fmt(d.final_u1.u1_angle_deg, 1)}°</span>
      ${d.within_limit === false ? `<span class="delta-up">超出 ${fmt(d.excess_g_mm)} g·mm</span>` : ""}
    </div>`;
  }

  const tubeRows = rows.map((t) => {
    const moved = t.final_slot !== null && t.final_slot !== undefined && t.moved;
    const target = t.final_slot === null || t.final_slot === undefined
      ? '<span class="small">— 无目标摆放 —</span>'
      : moved
        ? `<b>${t.final_slot}</b> <span class="small">(${fmt(t.final_angle_deg, 1)}°)</span>`
        : `${t.final_slot} <span class="small">(${fmt(t.final_angle_deg ?? t.original_angle_deg, 1)}°)</span>`;
    return `<tr${moved ? ' style="background:#3a2a1255"' : ""}>
      <td>${t.tube_id}${t.locked ? ' <span class="pill lock">锁定</span>' : ""}</td>
      <td>${fmt(t.mass_g, 3)}</td>
      <td>${t.original_slot} <span class="small">(${fmt(t.original_angle_deg, 1)}°)</span></td>
      <td>${target}</td>
      <td>${moved ? '<span class="status-tag st-moved">移动</span>' : '<span class="status-tag st-unchanged">不动</span>'}</td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    ${verdict}
    ${head}
    ${movesSummary}
    ${finalBlock}
    <h2 style="font-size:13px;margin:8px 0 6px">${tableTitle}</h2>
    <table class="cmp-table wc-table">
      <thead><tr><th>试管</th><th>质量(g)</th><th>原槽 / 角度</th><th>目标槽 / 角度</th><th>状态</th></tr></thead>
      <tbody>${tubeRows}</tbody>
    </table>
    <div class="small" style="margin-top:6px">算法: ${d.method}。${d.note} 方案 #${d.plan.id} 未被修改。</div>`;
}

$("btnEvac").onclick = () => {
  if (state.record && state.record.id !== null && state.record.id !== undefined) {
    openEvacDialog(state.record.id);
  } else {
    toast("请先从历史方案载入一条已保存方案, 或先计算并保存方案。", true);
  }
};
$("btnCloseEvac").onclick = () => $("evacDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 现场装管复核(只读, 基于已保存方案; 子集 DP 最小总绝对质量差一对一匹配)
// ---------------------------------------------------------------------------
const rcState = { planId: null, rows: [], enabledSlots: [], planBySlot: new Map(), planTubes: 0 };

async function openRecheckDialog(id) {
  $("btnRecheck")?.blur();
  try {
    const resp = await fetch(`/api/plan/${id}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "方案读取失败");
    rcState.planId = id;
    rcState.enabledSlots = data.result.slot_state
      .filter((s) => s.enabled)
      .map((s) => ({ slot: s.slot, angle: s.angle }))
      .sort((a, b) => a.slot - b.slot);
    rcState.planBySlot = new Map(
      data.result.placements
        .filter((p) => p.slot !== null && p.slot !== undefined)
        .map((p) => [p.slot, p]));
    rcState.planTubes = rcState.planBySlot.size;
    // 预填: 按方案摆放给出槽位与标称质量, 操作者再按现场实测修改
    rcState.rows = [...rcState.planBySlot.entries()]
      .map(([slot, p]) => ({ slot, mass: p.mass }))
      .sort((a, b) => a.slot - b.slot);
    $("recheckMeta").innerHTML =
      `方案 <b>#${data.id} ${data.name}</b> · ${new Date(data.created_at).toLocaleString()} ·
       ${rcState.planTubes} 支已放置试管 · 启用槽 ${rcState.enabledSlots.length} 个:
       ${rcState.enabledSlots.map((s) => `#${s.slot}`).join("、")}`;
    $("rcTol").value = "";
    $("recheckResult").innerHTML = "";
    renderRcRows();
    $("recheckDialog").classList.add("open");
  } catch (e) {
    toast(e.message, true);
  }
}

function renderRcRows() {
  const tb = $("rcBody");
  tb.innerHTML = "";
  const optsFor = (sel) => rcState.enabledSlots
    .map((s) => `<option value="${s.slot}" ${s.slot === sel ? "selected" : ""}>#${s.slot} (${fmt(s.angle, 1)}°)</option>`)
    .join("");
  rcState.rows.forEach((r, i) => {
    const tr = document.createElement("tr");
    const planRef = rcState.planBySlot.has(r.slot)
      ? `方案: 试管 ${rcState.planBySlot.get(r.slot).tube_id} · 标称 ${rcState.planBySlot.get(r.slot).mass} g`
      : '<span class="small">方案: 空槽(该槽按方案不应有管)</span>';
    tr.innerHTML = `
      <td><select data-f="slot">${optsFor(r.slot)}</select></td>
      <td><input type="number" min="0.01" step="0.01" data-f="mass" value="${r.mass}" placeholder="如 50.01"></td>
      <td class="small" data-role="planref">${planRef}</td>
      <td><button class="iconbtn" data-act="del">删</button></td>`;
    tr.querySelector('[data-f="slot"]').onchange = (e) => {
      r.slot = parseInt(e.target.value, 10);
      renderRcRows();
    };
    tr.querySelector('[data-f="mass"]').oninput = (e) => { r.mass = e.target.value; };
    tr.querySelector('[data-act="del"]').onclick = () => {
      rcState.rows.splice(i, 1);
      renderRcRows();
    };
    tb.appendChild(tr);
  });
  $("rcCount").textContent =
    `共 ${rcState.rows.length} / 12 份读数 · 方案 ${rcState.planTubes} 支试管`;
}

$("btnRcPrefill").onclick = () => {
  rcState.rows = [...rcState.planBySlot.entries()]
    .map(([slot, p]) => ({ slot, mass: p.mass }))
    .sort((a, b) => a.slot - b.slot);
  renderRcRows();
};

$("btnRcAdd").onclick = () => {
  if (rcState.rows.length >= 12) { toast("实测读数最多 12 份。", true); return; }
  const usedSlots = new Set(rcState.rows.map((r) => r.slot));
  const free = rcState.enabledSlots.find((s) => !usedSlots.has(s.slot));
  rcState.rows.push({ slot: free ? free.slot : rcState.enabledSlots[0].slot, mass: "" });
  renderRcRows();
};

$("btnRcRun").onclick = async () => {
  const btn = $("btnRcRun");
  btn.disabled = true;
  try {
    if (!rcState.rows.length) { toast("请至少录入 1 份实测读数。", true); return; }
    if (rcState.rows.length > 12) { toast("实测读数最多 12 份。", true); return; }
    const seen = new Set();
    const readings = [];
    for (const r of rcState.rows) {
      if (seen.has(r.slot)) { toast(`实测槽位 ${r.slot} 重复录入。`, true); return; }
      seen.add(r.slot);
      const m = parseFloat(r.mass);
      if (!(m > 0) || !Number.isFinite(m)) {
        toast(`槽位 ${r.slot} 的实测质量必须为正的有限数。`, true); return;
      }
      if (Math.abs(m * 100 - Math.round(m * 100)) > 1e-6) {
        toast(`槽位 ${r.slot} 的实测质量须为 0.01 g 的整数倍(读数精度 0.01 g)。`, true); return;
      }
      readings.push({ slot: r.slot, mass: Math.round(m * 100) / 100 });
    }
    const tol = parseFloat($("rcTol").value);
    if (!(tol >= 0) || !Number.isFinite(tol)) {
      toast("统一允许偏差必须为非负有限数。", true); return;
    }
    const resp = await fetch(`/api/plan/${rcState.planId}/tube-recheck`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ readings, tolerance_g: tol }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "装管复核失败");
    renderRecheckResult(data);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function renderRecheckResult(d) {
  const box = $("recheckResult");
  const rcTag = { correct: ["正确", "st-added"], wrong_slot: ["错槽", "st-removed"],
    uncertain: ["身份不确定", "st-moved"] };
  let verdict;
  if (!d.complete) {
    verdict = `<div class="wc-bad">✘ <b>无法完成复核</b>: ${d.reason}</div>`;
  } else if (d.summary.wrong_slot > 0) {
    verdict = `<div class="wc-bad">✘ <b>发现错槽</b>: ${d.reason}</div>`;
  } else if (d.summary.uncertain > 0) {
    verdict = `<div class="cmp-warn">⚠ <b>存在身份不确定</b>: ${d.reason}</div>`;
  } else {
    verdict = `<div class="wc-ok">✔ <b>装管全部正确</b>: ${d.reason}</div>`;
  }

  let head = `<div class="wc-note">统一允许偏差 ±${fmt(d.tolerance_g, 3)} g ·
    方案 ${d.plan_tube_count} 支试管 / 实测 ${d.reading_count} 份。`;
  if (d.complete) {
    head += ` 最小总绝对质量差 <b>${fmt(d.total_abs_deviation_g, 3)} g</b> ·
      最优匹配 ${d.optimal_matching_count} 种(逐槽候选为全部最优匹配的汇总, 非任取一解)。`;
  } else if (d.max_matched_pairs !== null && d.max_matched_pairs !== undefined) {
    head += ` 在偏差内最多可匹配 ${d.max_matched_pairs} 对。`;
    if (d.tubes_without_candidate.length) {
      head += `<br>偏差内无兼容读数的试管: ${d.tubes_without_candidate
        .map((t) => `试管 ${t.tube_id}(标称 ${t.nominal_mass_g} g, 方案槽 #${t.plan_slot})`).join("、")}`;
    }
    if (d.readings_without_candidate.length) {
      head += `<br>偏差内无兼容试管的读数: ${d.readings_without_candidate
        .map((r) => `槽 #${r.slot}(${r.measured_mass_g} g)`).join("、")}`;
    }
  }
  head += "</div>";

  let tables = "";
  if (d.complete) {
    const rRows = d.readings.map((r) => {
      const [txt, cls] = rcTag[r.status];
      const cands = r.candidates.map((c) =>
        `<span class="badge">试管 ${c.tube_id} · 标称 ${fmt(c.nominal_mass_g, 2)} g ·
          方案槽 #${c.plan_slot} · 偏差 ${fmt(c.abs_deviation_g, 3)} g</span>`).join(" ");
      return `<tr>
        <td>#${r.slot} <span class="small">(${fmt(r.angle_deg, 1)}°)</span></td>
        <td><b>${fmt(r.measured_mass_g, 2)}</b></td>
        <td>${cands}</td>
        <td><span class="status-tag ${cls}">${txt}</span></td>
      </tr>
        <tr><td colspan="4" class="small" style="border-top:none;padding-top:0">${r.detail}</td></tr>`;
    }).join("");
    const tRows = d.tubes.map((t) => {
      const [txt, cls] = rcTag[t.status];
      return `<tr>
        <td>${t.tube_id}</td>
        <td>${fmt(t.nominal_mass_g, 2)}</td>
        <td>#${t.plan_slot}</td>
        <td>${t.candidate_slots.map((s) => `#${s}`).join("、")}</td>
        <td><span class="status-tag ${cls}">${txt}</span></td>
      </tr>`;
    }).join("");
    tables = `
      <h2 style="font-size:13px;margin:10px 0 6px">逐实测槽判定
        <span class="small">正确 ${d.summary.correct} · 错槽 ${d.summary.wrong_slot} · 身份不确定 ${d.summary.uncertain}</span></h2>
      <table class="cmp-table wc-table">
        <thead><tr><th>实测槽 / 角度</th><th>实测质量(g)</th><th>最优匹配可能对应的试管</th><th>判定</th></tr></thead>
        <tbody>${rRows}</tbody>
      </table>
      <h2 style="font-size:13px;margin:12px 0 6px">逐方案试管核对
        <span class="small">该管在全部最优匹配中可能落入的实测槽</span></h2>
      <table class="cmp-table wc-table">
        <thead><tr><th>试管</th><th>标称质量(g)</th><th>方案槽</th><th>可能实测槽</th><th>判定</th></tr></thead>
        <tbody>${tRows}</tbody>
      </table>`;
  }
  box.innerHTML = `${verdict}${head}${tables}
    <div class="small" style="margin-top:6px">算法: ${d.method}。${d.note} 方案 #${d.plan.id} 未被修改。</div>`;
}

$("btnRecheck").onclick = () => {
  if (state.record && state.record.id !== null && state.record.id !== undefined) {
    openRecheckDialog(state.record.id);
  } else {
    toast("请先从历史方案载入一条已保存方案, 或先计算并保存方案。", true);
  }
};
$("btnCloseRecheck").onclick = () => $("recheckDialog").classList.remove("open");

// ---------------------------------------------------------------------------
// 标签页 & 初始化
// ---------------------------------------------------------------------------
document.querySelectorAll(".tabs button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    ["replay", "pairs", "placements"].forEach((t) => {
      $("tab-" + t).style.display = t === b.dataset.tab ? "" : "none";
    });
  };
});

$("btnGenSlots").onclick = genSlots;
$("slotCount").onchange = genSlots;
$("btnAddTube").onclick = () => addTube();
$("btnAddMutex").onclick = () => addMutexPair();

function loadSample() {
  $("slotCount").value = 12;
  $("radius").value = 100; $("rpm").value = 4000;
  $("planName").value = "示例: 6 支非等质量试管";
  genSlots();
  state.tubes = [];
  state.mutexPairs = [];
  state.nextTubeId = 1;
  [[50], [50], [51], [49], [30], [30]].forEach(([mass]) => addTube({ mass }));
  renderTubes();
  renderMutexPairs();
  drawRotor(null);
}
$("btnSample").onclick = loadSample;

// 初始
genSlots();
renderMutexPairs();
loadSample();
