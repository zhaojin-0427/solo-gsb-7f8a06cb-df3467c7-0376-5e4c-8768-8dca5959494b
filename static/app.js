/* 本地地块裁切 —— 原生 JS，无第三方依赖 */
"use strict";

const $ = (id) => document.getElementById(id);

const parcelInput = $("parcel-input");
const forbiddenInput = $("forbidden-input");
const statusEl = $("status");
const canvas = $("map");
const ctx = canvas.getContext("2d");

const LAYERS = {
  parcel: {
    show: true,
    fill: "rgba(31,111,235,0.12)",
    stroke: "#1f6feb",
    width: 2,
  },
  forbidden: {
    show: true,
    fill: "rgba(214,69,69,0.18)",
    stroke: "#d64545",
    width: 1.5,
  },
  result: {
    show: true,
    fill: "rgba(34,160,100,0.28)",
    stroke: "#1c8a4f",
    width: 2,
  },
  segments: {
    show: true,
    width: 2,
  },
  cuts: {
    show: true,
    stroke: "#e8830c",
    width: 1.5,
  },
};

// 12 段配色，下标即 (段号 - 1)
const SEGMENT_COLORS = [
  { fill: "rgba(230,87,87,0.30)", stroke: "#d64545" },
  { fill: "rgba(230,138,60,0.30)", stroke: "#d17a2c" },
  { fill: "rgba(224,184,51,0.30)", stroke: "#c39a17" },
  { fill: "rgba(150,201,61,0.30)", stroke: "#7fa323" },
  { fill: "rgba(74,194,107,0.30)", stroke: "#2e9e55" },
  { fill: "rgba(51,196,168,0.30)", stroke: "#1f9a82" },
  { fill: "rgba(63,178,214,0.30)", stroke: "#2389ab" },
  { fill: "rgba(71,135,224,0.30)", stroke: "#2f6fd6" },
  { fill: "rgba(96,92,222,0.30)", stroke: "#4c48d1" },
  { fill: "rgba(151,84,217,0.30)", stroke: "#7e44c9" },
  { fill: "rgba(209,82,193,0.30)", stroke: "#b43ca3" },
  { fill: "rgba(214,88,143,0.30)", stroke: "#c0457e" },
];

// 当前画布上的数据
const view = {
  parcelPolys: [], // [{outer:[[x,y]...], holes:[[ring],...]}]
  forbiddenPolys: [], // 全部禁建区面（用于范围计算）
  forbiddenGroups: [], // [{polys, name}] 按原始要素分组，组下标即要素索引
  resultPolys: [],
  // 等面积分段：每段 {polys:[...]}，按下标对应段号 1..count
  segmentGroups: [],
  segmentCuts: [], // [{projection}]
  segmentAxis: null, // {ux, uy} 分段方向
  activeSegment: -1,
  issues: [],
  activeIssue: -1,
  relations: [],
  activeRelation: -1, // 高亮的禁建区要素索引
};

// 六类关系的展示文案与描边色（与列表徽章配色一致）
const RELATION_META = {
  disjoint: { label: "disjoint", cn: "相离", color: "#9aa5b1" },
  touch: { label: "touch", cn: "共点/共边", color: "#d9a83b" },
  overlap: { label: "overlap", cn: "部分重叠", color: "#d64545" },
  within: { label: "within", cn: "完全位于地块内", color: "#1c8a4f" },
  contains: { label: "contains", cn: "包含地块", color: "#7e4fd1" },
  equals: { label: "equals", cn: "完全一致", color: "#1f6feb" },
};

// 世界坐标 -> 屏幕坐标的变换
const transform = { scale: 1, tx: 0, ty: 0 };
let worldBounds = null; // [minX, minY, maxX, maxY]

// ---------------------------------------------------------------------------
// GeoJSON 解析
// ---------------------------------------------------------------------------

function parseGeoJSONText(text, label) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    throw new Error(`${label}不是合法 JSON：${e.message}`);
  }
  if (!obj || typeof obj !== "object") {
    throw new Error(`${label}必须是 GeoJSON 对象`);
  }
  return obj;
}

/** 把任意受支持的 GeoJSON 输入展开为 {geoms, features} 。 */
function flattenInput(obj) {
  const type = obj.type;
  if (type === "FeatureCollection") {
    const features = Array.isArray(obj.features) ? obj.features : [];
    return {
      features,
      geoms: features.map((f) => f && f.geometry).filter(Boolean),
    };
  }
  if (type === "Feature") return { features: [obj], geoms: [obj.geometry] };
  if (type === "Polygon" || type === "MultiPolygon") {
    return { features: [null], geoms: [obj] };
  }
  throw new Error(
    `类型 ${JSON.stringify(type)} 不受支持，仅支持 Polygon / MultiPolygon`
  );
}

/** 从 geometry 列表提取面环（用于画布渲染，不做严格校验）。 */
function extractPolygons(geoms) {
  const polys = [];
  for (const geom of geoms || []) {
    if (!geom || typeof geom !== "object") continue;
    if (geom.type === "Polygon") {
      polys.push(polygonFromCoords(geom.coordinates));
    } else if (geom.type === "MultiPolygon") {
      for (const coords of geom.coordinates || []) {
        polys.push(polygonFromCoords(coords));
      }
    }
  }
  return polys.filter((p) => p.outer.length > 0);
}

function polygonFromCoords(coordinates) {
  const rings = Array.isArray(coordinates) ? coordinates : [];
  const toXY = (ring) =>
    (Array.isArray(ring) ? ring : [])
      .filter((c) => Array.isArray(c) && c.length >= 2 &&
        Number.isFinite(c[0]) && Number.isFinite(c[1]))
      .map((c) => [c[0], c[1]]);
  return {
    outer: rings.length > 0 ? toXY(rings[0]) : [],
    holes: rings.slice(1).map(toXY).filter((r) => r.length > 0),
  };
}

/** 要素展示名：优先取 properties.name / properties.id，其次 Feature.id。 */
function featureName(feature, index) {
  if (feature && feature.properties) {
    const p = feature.properties;
    if (p.name != null && p.name !== "") return String(p.name);
    if (p.id != null && p.id !== "") return String(p.id);
  }
  if (feature && feature.id != null) return String(feature.id);
  return `#${index}`;
}

/** 按原始要素索引把禁建区组织成组，每组保留该要素的全部面（供高亮）。 */
function buildForbiddenGroups(forbiddenObj) {
  if (!forbiddenObj) return [];
  let features;
  if (forbiddenObj.type === "FeatureCollection") {
    features = Array.isArray(forbiddenObj.features) ? forbiddenObj.features : [];
  } else if (forbiddenObj.type === "Feature") {
    features = [forbiddenObj];
  } else if (forbiddenObj.type === "Polygon" || forbiddenObj.type === "MultiPolygon") {
    features = [{ type: "Feature", geometry: forbiddenObj }];
  } else {
    return [];
  }
  return features.map((f, i) => ({
    name: featureName(f, i),
    polys: extractPolygons([f && f.geometry]),
  }));
}

function flattenForbiddenPolys(groups) {
  return groups.reduce((acc, g) => acc.concat(g.polys), []);
}

// ---------------------------------------------------------------------------
// 画布渲染
// ---------------------------------------------------------------------------

function resizeCanvas() {
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.width = w + "px";
  canvas.style.height = h + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function boundsOf(polys) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  let found = false;
  for (const p of polys) {
    for (const ring of [p.outer, ...p.holes]) {
      for (const [x, y] of ring) {
        found = true;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  return found ? [minX, minY, maxX, maxY] : null;
}

function issueBounds(issues) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  let found = false;
  for (const it of issues) {
    const c = it.coordinate;
    if (!c) continue;
    found = true;
    if (c[0] < minX) minX = c[0];
    if (c[0] > maxX) maxX = c[0];
    if (c[1] < minY) minY = c[1];
    if (c[1] > maxY) maxY = c[1];
  }
  if (!found) return null;
  if (maxX - minX < 1e-6 && maxY - minY < 1e-6) {
    return [minX - 5, minY - 5, maxX + 5, maxY + 5];
  }
  return [minX, minY, maxX, maxY];
}

function mergeBounds(a, b) {
  if (!a) return b;
  if (!b) return a;
  return [
    Math.min(a[0], b[0]),
    Math.min(a[1], b[1]),
    Math.max(a[2], b[2]),
    Math.max(a[3], b[3]),
  ];
}

function fitTo(bounds) {
  if (!bounds) {
    worldBounds = null;
    draw();
    return;
  }
  worldBounds = bounds;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const pad = 40;
  const [minX, minY, maxX, maxY] = bounds;
  const bw = Math.max(maxX - minX, 1e-9);
  const bh = Math.max(maxY - minY, 1e-9);
  const scale = Math.min((w - pad * 2) / bw, (h - pad * 2) / bh);
  transform.scale = scale;
  // 世界坐标 y 轴向上，屏幕 y 轴向下，先把范围中心移到原点再翻转
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  transform.tx = w / 2 - cx * scale;
  transform.ty = h / 2 + cy * scale;
  draw();
}

function toScreen(x, y) {
  return [x * transform.scale + transform.tx, -y * transform.scale + transform.ty];
}

function toWorld(sx, sy) {
  return [
    (sx - transform.tx) / transform.scale,
    -(sy - transform.ty) / transform.scale,
  ];
}

function polygonPath(poly) {
  const path = new Path2D();
  const addRing = (ring) => {
    if (ring.length === 0) return;
    const [sx, sy] = toScreen(ring[0][0], ring[0][1]);
    path.moveTo(sx, sy);
    for (let i = 1; i < ring.length; i++) {
      const [x, y] = toScreen(ring[i][0], ring[i][1]);
      path.lineTo(x, y);
    }
    path.closePath();
  };
  addRing(poly.outer);
  for (const hole of poly.holes) addRing(hole);
  return path;
}

function drawLayer(polys, layerKey) {
  const style = LAYERS[layerKey];
  if (!style.show) return;
  for (const poly of polys) {
    const path = polygonPath(poly);
    // 外环与孔洞环合并在同一路径，evenodd 填充使孔洞透明
    ctx.fillStyle = style.fill;
    ctx.fill(path, "evenodd");
    ctx.strokeStyle = style.stroke;
    ctx.lineWidth = style.width;
    ctx.stroke(path);
  }
}

/** 画禁建区：未审计时用统一色；审计后按关系着色，高亮组加粗描边。 */
function drawForbidden() {
  if (!LAYERS.forbidden.show) return;
  const style = LAYERS.forbidden;
  const hasActive = view.activeRelation >= 0;
  const relByIndex = new Map(view.relations.map((r) => [r.feature_index, r.relation]));

  view.forbiddenGroups.forEach((group, i) => {
    const relation = relByIndex.get(i);
    const isActive = hasActive && i === view.activeRelation;
    let stroke = style.stroke;
    let fill = style.fill;
    let width = style.width;
    if (relation && RELATION_META[relation]) stroke = RELATION_META[relation].color;
    if (isActive) {
      width = 3.5;
      fill = "rgba(255,140,0,0.28)";
      stroke = "#ff8c00";
    } else if (hasActive) {
      ctx.save();
      ctx.globalAlpha = 0.35;
    }
    for (const poly of group.polys) {
      const path = polygonPath(poly);
      ctx.fillStyle = fill;
      ctx.fill(path, "evenodd");
      ctx.strokeStyle = stroke;
      ctx.lineWidth = width;
      ctx.stroke(path);
    }
    if (!isActive && hasActive) ctx.restore();
  });
}

function draw() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);

  if (LAYERS.parcel.show) drawLayer(view.parcelPolys, "parcel");
  if (LAYERS.forbidden.show) drawForbidden();
  if (LAYERS.result.show) drawLayer(view.resultPolys, "result");
  if (LAYERS.segments.show) drawSegments();
  if (LAYERS.cuts.show) drawCutLines();

  drawIssueMarkers();
}

/** 按段着色绘制等面积分段；被选中段加粗、其余段变淡。 */
function drawSegments() {
  const hasActive = view.activeSegment >= 0;
  view.segmentGroups.forEach((group, i) => {
    const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
    const active = hasActive && i === view.activeSegment;
    if (!active && hasActive) {
      ctx.save();
      ctx.globalAlpha = 0.3;
    }
    for (const poly of group.polys) {
      const path = polygonPath(poly);
      ctx.fillStyle = color.fill;
      ctx.fill(path, "evenodd");
      ctx.strokeStyle = color.stroke;
      ctx.lineWidth = active ? 3.5 : LAYERS.segments.width;
      ctx.stroke(path);
    }
    if (!active && hasActive) ctx.restore();
  });
}

/** 画分段切线（与分段方向垂直、贯穿画布的虚线）。 */
function drawCutLines() {
  if (!view.segmentAxis || view.segmentCuts.length === 0) return;
  const { ux, uy } = view.segmentAxis;
  // 切割线方向 v = u 顺时针转 90°
  const vx = -uy;
  const vy = ux;

  ctx.save();
  ctx.strokeStyle = LAYERS.cuts.stroke;
  ctx.lineWidth = LAYERS.cuts.width;
  ctx.setLineDash([7, 5]);
  ctx.beginPath();
  for (const cut of view.segmentCuts) {
    const t = cut.projection;
    // 沿 v 方向取足够远的两点（用世界坐标的对角线尺度做延伸）
    let reach = 1;
    if (worldBounds) {
      reach = Math.max(worldBounds[2] - worldBounds[0], worldBounds[3] - worldBounds[1]) * 2;
    }
    const p1 = toScreen(t * ux - reach * vx, t * uy - reach * vy);
    const p2 = toScreen(t * ux + reach * vx, t * uy + reach * vy);
    ctx.moveTo(p1[0], p1[1]);
    ctx.lineTo(p2[0], p2[1]);
  }
  ctx.stroke();
  ctx.restore();
}

function drawIssueMarkers() {
  view.issues.forEach((issue, i) => {
    const c = issue.coordinate;
    if (!c) return;
    const [sx, sy] = toScreen(c[0], c[1]);
    if (sx < -20 || sy < -20 || sx > canvas.clientWidth + 20 || sy > canvas.clientHeight + 20)
      return;
    const active = i === view.activeIssue;
    const r = active ? 8 : 5;
    ctx.beginPath();
    ctx.arc(sx, sy, r, 0, Math.PI * 2);
    ctx.fillStyle = active ? "#ff8c00" : "#d64545";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
  });
}

// ---------------------------------------------------------------------------
// 交互：缩放 / 平移
// ---------------------------------------------------------------------------

function zoomAt(sx, sy, factor) {
  const [wx, wy] = toWorld(sx, sy);
  transform.scale *= factor;
  transform.tx = sx - wx * transform.scale;
  transform.ty = sy + wy * transform.scale;
  draw();
}

let dragging = false;
let dragStart = null;

canvas.addEventListener("mousedown", (e) => {
  dragging = true;
  dragStart = { x: e.clientX, y: e.clientY, tx: transform.tx, ty: transform.ty };
  canvas.classList.add("dragging");
});
window.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  transform.tx = dragStart.tx + (e.clientX - dragStart.x);
  transform.ty = dragStart.ty + (e.clientY - dragStart.y);
  draw();
});
window.addEventListener("mouseup", () => {
  dragging = false;
  canvas.classList.remove("dragging");
});
canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    zoomAt(e.clientX - rect.left, e.clientY - rect.top, factor);
  },
  { passive: false }
);

$("btn-zoom-in").addEventListener("click", () =>
  zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1.2)
);
$("btn-zoom-out").addEventListener("click", () =>
  zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1 / 1.2)
);
$("btn-fit").addEventListener("click", () => fitTo(currentBounds()));

function currentBounds() {
  let b = boundsOf(view.parcelPolys);
  b = mergeBounds(b, boundsOf(view.forbiddenPolys));
  b = mergeBounds(b, boundsOf(view.resultPolys));
  for (const g of view.segmentGroups) b = mergeBounds(b, boundsOf(g.polys));
  return b;
}

for (const [key, id] of [
  ["parcel", "show-parcel"],
  ["forbidden", "show-forbidden"],
  ["result", "show-result"],
  ["segments", "show-segments"],
  ["cuts", "show-cuts"],
]) {
  $(id).addEventListener("change", (e) => {
    LAYERS[key].show = e.target.checked;
    draw();
  });
}

// ---------------------------------------------------------------------------
// 状态 / 统计 / 问题列表
// ---------------------------------------------------------------------------

function setStatus(text, kind) {
  statusEl.textContent = text || "";
  statusEl.className = "status" + (kind ? " " + kind : "");
}

function fmtArea(m2) {
  if (m2 == null || Number.isNaN(m2)) return "—";
  const ha = m2 / 10000;
  if (m2 >= 10000) {
    return `${m2.toLocaleString("zh-CN", { maximumFractionDigits: 3 })} m²` +
      `（${ha.toLocaleString("zh-CN", { maximumFractionDigits: 4 })} 公顷）`;
  }
  return `${m2.toLocaleString("zh-CN", { maximumFractionDigits: 3 })} m²`;
}

function renderStats(areas) {
  if (!areas) {
    for (const id of ["stat-parcel", "stat-forbidden", "stat-removed", "stat-remaining"])
      $(id).textContent = "—";
    return;
  }
  $("stat-parcel").textContent = fmtArea(areas.parcel_area);
  $("stat-forbidden").textContent = fmtArea(areas.forbidden_union_area);
  $("stat-removed").textContent = fmtArea(areas.removed_area);
  $("stat-remaining").textContent = fmtArea(areas.remaining_area);
}

const LAYER_NAME = { parcel: "地块", forbidden: "禁建区" };

function renderIssues(issues) {
  view.issues = issues || [];
  view.activeIssue = -1;
  const panel = $("issues-panel");
  const list = $("issue-list");
  list.innerHTML = "";
  $("issue-count").textContent = view.issues.length;

  if (view.issues.length === 0) {
    panel.hidden = true;
    draw();
    return;
  }
  panel.hidden = false;

  view.issues.forEach((issue, i) => {
    const li = document.createElement("li");
    const locParts = [LAYER_NAME[issue.layer] || issue.layer];
    locParts.push(`要素 #${issue.feature_index}`);
    if (issue.feature_id != null) locParts.push(`id=${issue.feature_id}`);
    if (issue.polygon_index != null) locParts.push(`面 ${issue.polygon_index}`);
    if (issue.ring)
      locParts.push(`${issue.ring === "outer" ? "外环" : "孔洞环"} ${issue.ring_index}`);
    if (issue.coordinate)
      locParts.push(`坐标 [${issue.coordinate[0]}, ${issue.coordinate[1]}]`);

    const msg = document.createElement("div");
    msg.textContent = issue.message;
    const loc = document.createElement("span");
    loc.className = "issue-loc";
    loc.textContent = locParts.join(" · ");
    li.appendChild(msg);
    li.appendChild(loc);
    li.addEventListener("click", () => focusIssue(i));
    li.dataset.index = i;
    list.appendChild(li);
  });
  draw();
}

function focusIssue(i) {
  view.activeIssue = i;
  const issue = view.issues[i];
  document.querySelectorAll("#issue-list li").forEach((li) => {
    li.classList.toggle("active", Number(li.dataset.index) === i);
  });
  if (issue && issue.coordinate) {
    fitTo(issueBounds(view.issues));
  }
  draw();
}

// ---------------------------------------------------------------------------
// 业务动作
// ---------------------------------------------------------------------------

let lastResultGeoJSON = null;
let lastSubdivideGeoJSON = null;
// JSON 查看面板当前展示的内容来源：null | "clip" | "segments"
let jsonViewMode = null;

/** 清除关系审计结果与高亮（重新预览 / 裁切 / 输入变更时调用）。 */
function resetRelations() {
  view.relations = [];
  view.activeRelation = -1;
  $("relations-panel").hidden = true;
  $("relation-list").innerHTML = "";
  $("relation-count").textContent = "0";
}

/** 清除旧分段结果（输入改变 / 执行其它动作时调用）。 */
function resetSegments() {
  view.segmentGroups = [];
  view.segmentCuts = [];
  view.segmentAxis = null;
  view.activeSegment = -1;
  lastSubdivideGeoJSON = null;
  $("segments-box").hidden = true;
  $("segment-list").innerHTML = "";
  $("segments-summary").textContent = "";
  $("btn-download-segments").disabled = true;
  $("btn-view-segments-json").disabled = true;
  // JSON 面板若正展示分段内容则一并收起
  if (jsonViewMode === "segments") {
    $("json-panel").hidden = true;
    $("json-view").textContent = "";
    jsonViewMode = null;
  }
}

function readInputs() {
  const parcelText = parcelInput.value.trim();
  const forbiddenText = forbiddenInput.value.trim();
  if (!parcelText) throw new Error("请输入地块 GeoJSON");
  const parcel = parseGeoJSONText(parcelText, "地块");
  // 确保结构可被后端理解（提前给出友好提示）
  flattenInput(parcel);
  let forbidden = null;
  if (forbiddenText) {
    forbidden = parseGeoJSONText(forbiddenText, "禁建区");
    flattenInput(forbidden);
  }
  return { parcel, forbidden };
}

function setPreviewLayers(payload) {
  view.parcelPolys = extractPolygons(flattenInput(payload.parcel).geoms);
  view.forbiddenGroups = payload.forbidden
    ? buildForbiddenGroups(payload.forbidden)
    : [];
  view.forbiddenPolys = flattenForbiddenPolys(view.forbiddenGroups);
}

function previewInputs() {
  $("json-panel").hidden = true;
  jsonViewMode = null;
  renderStats(null);
  renderIssues([]);
  resetRelations();
  resetSegments();
  lastResultGeoJSON = null;
  $("btn-download").disabled = true;
  $("btn-view-json").disabled = true;
  view.resultPolys = [];

  try {
    const { parcel, forbidden } = readInputs();
    setPreviewLayers({ parcel, forbidden });
    if (view.parcelPolys.length === 0) {
      throw new Error("地块中没有可绘制的 Polygon / MultiPolygon");
    }
    let b = boundsOf(view.parcelPolys);
    b = mergeBounds(b, boundsOf(view.forbiddenPolys));
    fitTo(b);
    setStatus(
      `预览：地块 ${view.parcelPolys.length} 个面` +
        (view.forbiddenPolys.length
          ? `，禁建区 ${view.forbiddenGroups.length} 个要素`
          : "（无禁建区）"),
      "ok"
    );
  } catch (e) {
    view.parcelPolys = [];
    view.forbiddenPolys = [];
    view.forbiddenGroups = [];
    fitTo(null);
    setStatus(e.message, "error");
  }
}

async function runClip() {
  $("json-panel").hidden = true;
  jsonViewMode = null;
  renderStats(null);
  renderIssues([]);
  resetRelations();
  resetSegments();
  lastResultGeoJSON = null;
  $("btn-download").disabled = true;
  $("btn-view-json").disabled = true;
  view.resultPolys = [];

  let payload;
  try {
    payload = readInputs();
  } catch (e) {
    setStatus(e.message, "error");
    return;
  }

  // 先把输入画出来，即使后端报错也能看到问题位置
  try {
    setPreviewLayers(payload);
  } catch {
    /* 预览失败不影响请求 */
  }

  setStatus("正在校验并裁切…");
  try {
    const resp = await fetch("/api/clip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();

    if (!data.ok) {
      renderIssues(data.issues);
      fitTo(mergeBounds(currentBounds(), issueBounds(view.issues)) || currentBounds());
      setStatus(
        data.error + `（共 ${data.issues ? data.issues.length : 0} 个问题，点击列表可定位）`,
        "error"
      );
      return;
    }

    lastResultGeoJSON = data.result;
    view.resultPolys = extractPolygons(
      (lastResultGeoJSON.features || []).map((f) => f.geometry)
    );
    renderStats(data.areas);
    renderIssues([]);
    fitTo(currentBounds());
    $("btn-download").disabled = data.feature_count === 0;
    $("btn-view-json").disabled = false;
    if (data.feature_count === 0) {
      setStatus("裁切完成：地块被完全扣除，剩余面积为 0，结果为空 GeoJSON", "ok");
    } else {
      setStatus(
        `裁切完成：剩余 ${data.feature_count} 个独立面，剩余面积 ${fmtArea(
          data.areas.remaining_area
        )}`,
        "ok"
      );
    }
  } catch (e) {
    setStatus(`请求失败：${e.message}`, "error");
  }
}

$("btn-preview").addEventListener("click", previewInputs);
$("btn-clip").addEventListener("click", runClip);

// ---------------------------------------------------------------------------
// 禁建区关系审计
// ---------------------------------------------------------------------------

function fmtDistance(m) {
  if (m == null || Number.isNaN(m)) return "—";
  return `${m.toLocaleString("zh-CN", { maximumFractionDigits: 3 })} m`;
}

function groupBounds(groupIndex) {
  const group = view.forbiddenGroups[groupIndex];
  return group ? boundsOf(group.polys) : null;
}

function renderRelations(rows) {
  view.relations = rows || [];
  view.activeRelation = -1;
  const panel = $("relations-panel");
  const list = $("relation-list");
  list.innerHTML = "";
  $("relation-count").textContent = view.relations.length;
  panel.hidden = false;

  view.relations.forEach((r) => {
    const meta = RELATION_META[r.relation] || { label: r.relation, cn: "" };
    const li = document.createElement("li");
    li.className = `row-${r.relation}`;
    li.dataset.index = r.feature_index;

    const title = document.createElement("div");
    title.className = "relation-title";
    const badge = document.createElement("span");
    badge.className = `rel-badge rel-${r.relation}`;
    badge.textContent = `${meta.label} ${meta.cn}`;
    const idx = document.createElement("span");
    idx.textContent = `要素 #${r.feature_index}`;
    const name = document.createElement("span");
    name.className = "relation-name";
    name.textContent =
      view.forbiddenGroups[r.feature_index] &&
      view.forbiddenGroups[r.feature_index].name;
    title.appendChild(badge);
    title.appendChild(idx);
    title.appendChild(name);

    const metrics = document.createElement("div");
    metrics.className = "relation-metrics";
    metrics.textContent =
      `交集面积 ${fmtArea(r.intersection_area_m2)} · ` +
      `边界最短距离 ${fmtDistance(r.boundary_distance_m)}`;

    li.appendChild(title);
    li.appendChild(metrics);
    li.addEventListener("click", () => focusRelation(r.feature_index));
    list.appendChild(li);
  });
  draw();
}

function focusRelation(featureIndex) {
  view.activeRelation = featureIndex;
  document.querySelectorAll("#relation-list li").forEach((li) => {
    li.classList.toggle("active", Number(li.dataset.index) === featureIndex);
  });
  // 高亮依赖禁建区图层，被关掉时临时打开
  if (!LAYERS.forbidden.show) {
    LAYERS.forbidden.show = true;
    $("show-forbidden").checked = true;
  }
  const b = groupBounds(featureIndex);
  if (b) {
    // 小要素周围留白，避免高亮边框贴到画布边缘
    const pad = Math.max(
      8,
      (worldBounds
        ? Math.max(worldBounds[2] - worldBounds[0], worldBounds[3] - worldBounds[1]) * 0.04
        : 20)
    );
    fitTo([b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]);
  } else {
    draw();
  }
}

async function runRelations() {
  $("json-panel").hidden = true;
  jsonViewMode = null;
  renderStats(null);
  renderIssues([]);
  resetRelations();
  resetSegments();
  lastResultGeoJSON = null;
  $("btn-download").disabled = true;
  $("btn-view-json").disabled = true;
  view.resultPolys = [];

  let payload;
  try {
    payload = readInputs();
  } catch (e) {
    setStatus(e.message, "error");
    return;
  }

  try {
    setPreviewLayers(payload);
  } catch {
    /* 预览失败不影响请求 */
  }

  setStatus("正在校验并审计空间关系…");
  try {
    const resp = await fetch("/api/relations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();

    if (!data.ok) {
      renderIssues(data.issues);
      fitTo(mergeBounds(currentBounds(), issueBounds(view.issues)) || currentBounds());
      setStatus(
        data.error + `（共 ${data.issues ? data.issues.length : 0} 个问题，点击列表可定位）`,
        "error"
      );
      return;
    }

    renderRelations(data.relations);
    fitTo(currentBounds());
    if (data.feature_count === 0) {
      setStatus("关系审计完成：没有禁建区要素", "ok");
    } else {
      const counts = {};
      for (const r of data.relations) counts[r.relation] = (counts[r.relation] || 0) + 1;
      const summary = ["overlap", "within", "contains", "equals", "touch", "disjoint"]
        .filter((k) => counts[k])
        .map((k) => `${RELATION_META[k].label} ${counts[k]}`)
        .join("，");
      setStatus(`关系审计完成：共 ${data.feature_count} 个禁建区（${summary}），点击条目高亮`, "ok");
    }
  } catch (e) {
    setStatus(`请求失败：${e.message}`, "error");
  }
}

$("btn-relations").addEventListener("click", runRelations);

// ---------------------------------------------------------------------------
// 等面积分段
// ---------------------------------------------------------------------------

const subCountInput = $("sub-count");
const subAngleInput = $("sub-angle");

function readSubdivideParams() {
  const countRaw = subCountInput.value.trim();
  const angleRaw = subAngleInput.value.trim();
  // 前端只接受真正的整数写法（"3" 可以，3.5 / true / 空串不行）
  if (!/^[+-]?\d+$/.test(countRaw)) {
    throw new Error("分段数量 count 必须是 2～12 的整数");
  }
  const count = Number(countRaw);
  if (count < 2 || count > 12) {
    throw new Error("分段数量 count 必须在 2～12 之间");
  }
  if (angleRaw === "" || !Number.isFinite(Number(angleRaw))) {
    throw new Error("方向角 angle_deg 必须是有限数字");
  }
  const angle = Number(angleRaw);
  if (!(angle >= 0 && angle < 180)) {
    throw new Error("方向角 angle_deg 必须满足 0 ≤ 值 < 180");
  }
  return { count, angle_deg: angle };
}

function renderSegments(data) {
  const features = data.result.features || [];
  view.segmentGroups = features.map((f) => ({
    polys: extractPolygons([f.geometry]),
  }));
  view.segmentCuts = data.cuts || [];
  view.segmentAxis = data.direction || null;
  view.activeSegment = -1;

  const list = $("segment-list");
  list.innerHTML = "";
  features.forEach((f, i) => {
    const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
    const li = document.createElement("li");
    li.style.borderLeftColor = color.stroke;
    li.dataset.index = i;

    const title = document.createElement("div");
    title.className = "segment-title";
    const dot = document.createElement("span");
    dot.className = "swatch";
    dot.style.width = "11px";
    dot.style.height = "11px";
    dot.style.background = color.fill;
    dot.style.borderColor = color.stroke;
    const name = document.createElement("span");
    name.textContent = `第 ${f.properties.segment} 段`;
    title.appendChild(dot);
    title.appendChild(name);

    const metrics = document.createElement("div");
    metrics.className = "segment-metrics";
    const err = Math.abs(f.properties.area_error_m2 || 0);
    metrics.textContent =
      `实际 ${fmtArea(f.properties.area_m2)} · ` +
      `目标 ${fmtArea(f.properties.target_area_m2)} · 差 ${err.toExponential(1)} m²`;

    li.appendChild(title);
    li.appendChild(metrics);
    li.addEventListener("click", () => focusSegment(i));
    list.appendChild(li);
  });

  $("segments-summary").textContent =
    `共 ${data.count} 段 · 方向 ${Number(data.angle_deg).toLocaleString("zh-CN")}° · ` +
    `可建面积 ${fmtArea(data.areas.buildable_area)} · ` +
    `目标每段 ${fmtArea(data.target_area_m2)} · 容差 ±${data.area_tolerance_m2.toExponential(1)} m²`;
  $("segments-box").hidden = false;
  $("btn-download-segments").disabled = false;
  $("btn-view-segments-json").disabled = false;
}

function focusSegment(i) {
  view.activeSegment = i;
  document.querySelectorAll("#segment-list li").forEach((li) => {
    li.classList.toggle("active", Number(li.dataset.index) === i);
  });
  const group = view.segmentGroups[i];
  if (group) fitTo(boundsOf(group.polys));
  else draw();
}

async function runSubdivide() {
  $("json-panel").hidden = true;
  jsonViewMode = null;
  renderStats(null);
  renderIssues([]);
  resetRelations();
  resetSegments();
  lastResultGeoJSON = null;
  $("btn-download").disabled = true;
  $("btn-view-json").disabled = true;
  view.resultPolys = [];

  let payload;
  try {
    const inputs = readInputs();
    const params = readSubdivideParams();
    payload = { ...inputs, ...params };
  } catch (e) {
    setStatus(e.message, "error");
    return;
  }

  try {
    setPreviewLayers(payload);
  } catch {
    /* 预览失败不影响请求 */
  }

  setStatus("正在计算等面积分段…");
  try {
    const resp = await fetch("/api/subdivide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();

    if (!data.ok) {
      if (data.issues && data.issues.length) {
        renderIssues(data.issues);
        fitTo(mergeBounds(currentBounds(), issueBounds(view.issues)) || currentBounds());
        setStatus(
          data.error + `（共 ${data.issues.length} 个问题，点击列表可定位）`,
          "error"
        );
      } else {
        setStatus(data.error, "error");
      }
      return;
    }

    lastSubdivideGeoJSON = data.result;
    renderSegments(data);
    const clipAreas = {
      parcel_area: data.areas.parcel_area,
      forbidden_union_area: data.areas.forbidden_union_area,
      removed_area: data.areas.removed_area,
      remaining_area: data.areas.buildable_area,
    };
    renderStats(clipAreas);
    renderIssues([]);
    fitTo(currentBounds());
    const maxErr = Math.max(...data.segments.map((s) => Math.abs(s.area_error_m2)));
    setStatus(
      `等面积分段完成：${data.count} 段，方向 ${data.angle_deg}°，` +
        `最大面积偏差 ${maxErr.toExponential(2)} m²（容差 ${data.area_tolerance_m2.toExponential(1)}）`,
      "ok"
    );
  } catch (e) {
    setStatus(`请求失败：${e.message}`, "error");
  }
}

$("btn-subdivide").addEventListener("click", runSubdivide);

$("btn-download-segments").addEventListener("click", () => {
  if (!lastSubdivideGeoJSON || lastSubdivideGeoJSON.features.length === 0) return;
  const blob = new Blob([JSON.stringify(lastSubdivideGeoJSON, null, 2)], {
    type: "application/geo+json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.href = url;
  a.download = `subdivide-${ts}.geojson`;
  a.click();
  URL.revokeObjectURL(url);
});

$("btn-view-segments-json").addEventListener("click", () => {
  if (!lastSubdivideGeoJSON) return;
  const panel = $("json-panel");
  const show = panel.hidden || jsonViewMode !== "segments";
  panel.hidden = !show;
  $("json-view").textContent = JSON.stringify(lastSubdivideGeoJSON, null, 2);
  jsonViewMode = show ? "segments" : null;
  if (show) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

// 数量 / 角度或任何输入改变时，立即清除旧分段，避免页面显示与输入不一致
[subCountInput, subAngleInput].forEach((el) =>
  el.addEventListener("input", () => {
    resetSegments();
    draw();
  })
);

$("btn-download").addEventListener("click", () => {
  if (!lastResultGeoJSON || lastResultGeoJSON.features.length === 0) return;
  const blob = new Blob([JSON.stringify(lastResultGeoJSON, null, 2)], {
    type: "application/geo+json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.href = url;
  a.download = `clip-result-${ts}.geojson`;
  a.click();
  URL.revokeObjectURL(url);
});

$("btn-view-json").addEventListener("click", () => {
  if (!lastResultGeoJSON) return;
  const panel = $("json-panel");
  const show = panel.hidden || jsonViewMode !== "clip";
  panel.hidden = !show;
  $("json-view").textContent = JSON.stringify(lastResultGeoJSON, null, 2);
  jsonViewMode = show ? "clip" : null;
  if (show) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

$("clear-forbidden").addEventListener("click", () => {
  forbiddenInput.value = "";
  resetSegments();
  draw();
});

// 地块 / 禁建区文本改变即清除旧分段，避免分段与最新输入不一致
parcelInput.addEventListener("input", () => {
  resetSegments();
  draw();
});
forbiddenInput.addEventListener("input", () => {
  resetSegments();
  draw();
});

// ---------------------------------------------------------------------------
// 示例数据
// ---------------------------------------------------------------------------

const SAMPLE_PARCEL = {
  type: "Polygon",
  coordinates: [
    [
      [0, 0],
      [120, 0],
      [120, 80],
      [0, 80],
      [0, 0],
    ],
  ],
};

const SAMPLE_FORBIDDEN = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      properties: { name: "禁建区A（越过地块东界，与B重叠）" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [20, 20],
            [130, 20],
            [130, 55],
            [20, 55],
            [20, 20],
          ],
        ],
      },
    },
    {
      type: "Feature",
      properties: { name: "禁建区B（越过地块东界，与A重叠）" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [45, 30],
            [125, 30],
            [125, 60],
            [45, 60],
            [45, 30],
          ],
        ],
      },
    },
    {
      type: "Feature",
      properties: { name: "禁建区C（与地块共边，不扣面积）" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [90, 80],
            [110, 80],
            [110, 100],
            [90, 100],
            [90, 80],
          ],
        ],
      },
    },
    {
      type: "Feature",
      properties: { name: "禁建区D（仅与地块共点，不扣面积）" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [120, 80],
            [135, 80],
            [120, 95],
            [120, 80],
          ],
        ],
      },
    },
    {
      type: "Feature",
      properties: { name: "禁建区E（完全位于地块内）" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [85, 10],
            [105, 10],
            [105, 30],
            [85, 30],
            [85, 10],
          ],
        ],
      },
    },
    {
      type: "Feature",
      properties: { name: "禁建区F（与地块相离）" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [135, 10],
            [155, 10],
            [155, 30],
            [135, 30],
            [135, 10],
          ],
        ],
      },
    },
  ],
};

$("load-sample").addEventListener("click", () => {
  parcelInput.value = JSON.stringify(SAMPLE_PARCEL, null, 2);
  forbiddenInput.value = JSON.stringify(SAMPLE_FORBIDDEN, null, 2);
  previewInputs();
});

// 初始化
window.addEventListener("resize", resizeCanvas);
resizeCanvas();
