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
  segments: { show: true }, // 分段按段着色，颜色取自 SEGMENT_COLORS
};

// 等面积分段的调色板（最多 12 段，与分段数量上限一致）
const SEGMENT_COLORS = [
  "#1f6feb", "#1c8a4f", "#d9a83b", "#7e4fd1",
  "#d64545", "#0e8a9e", "#c2571a", "#5b7f2b",
  "#b03a8c", "#4a5bd0", "#8a6d3b", "#2c8c7a",
];

// 当前画布上的数据
const view = {
  parcelPolys: [], // [{outer:[[x,y]...], holes:[[ring],...]}]
  forbiddenPolys: [], // 全部禁建区面（用于范围计算）
  forbiddenGroups: [], // [{polys, name}] 按原始要素分组，组下标即要素索引
  resultPolys: [],
  issues: [],
  activeIssue: -1,
  relations: [],
  activeRelation: -1, // 高亮的禁建区要素索引
  segments: [], // [{polys, info}] 等面积分段结果，按下标即段号-1
  activeSegment: -1, // 高亮的段下标
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

  drawIssueMarkers();
}

/** #rrggbb → 带透明度的 rgba 字符串 */
function hexA(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function polyCentroid(poly) {
  // 用外环顶点均值作为标注位置（对分段结果的展示足够稳定）
  let sx = 0, sy = 0;
  const n = poly.outer.length;
  for (const [x, y] of poly.outer) { sx += x; sy += y; }
  return n ? [sx / n, sy / n] : null;
}

/** 画等面积分段：每段独立着色，段号标注在最大面中心。 */
function drawSegments() {
  const hasActive = view.activeSegment >= 0;
  view.segments.forEach((seg, i) => {
    const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
    const isActive = hasActive && i === view.activeSegment;
    if (hasActive && !isActive) {
      ctx.save();
      ctx.globalAlpha = 0.3;
    }
    for (const poly of seg.polys) {
      const path = polygonPath(poly);
      ctx.fillStyle = hexA(color, isActive ? 0.5 : 0.32);
      ctx.fill(path, "evenodd");
      ctx.strokeStyle = color;
      ctx.lineWidth = isActive ? 3.5 : 2;
      ctx.stroke(path);
    }
    if (hasActive && !isActive) ctx.restore();

    // 段号标注：取该段顶点最多的面
    let main = null;
    for (const poly of seg.polys) {
      if (!main || poly.outer.length > main.outer.length) main = poly;
    }
    const c = main && polyCentroid(main);
    if (c) {
      const [sx, sy] = toScreen(c[0], c[1]);
      ctx.font = "bold 13px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(255,255,255,0.9)";
      ctx.strokeText(`段 ${i + 1}`, sx, sy);
      ctx.fillStyle = color;
      ctx.fillText(`段 ${i + 1}`, sx, sy);
    }
  });
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
  for (const seg of view.segments) b = mergeBounds(b, boundsOf(seg.polys));
  return b;
}

for (const [key, id] of [
  ["parcel", "show-parcel"],
  ["forbidden", "show-forbidden"],
  ["result", "show-result"],
  ["segments", "show-segments"],
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

/** 清除关系审计结果与高亮（重新预览 / 裁切 / 输入变更时调用）。 */
function resetRelations() {
  view.relations = [];
  view.activeRelation = -1;
  $("relations-panel").hidden = true;
  $("relation-list").innerHTML = "";
  $("relation-count").textContent = "0";
}

/** 清除等面积分段结果（输入变更或执行其他动作时调用）。 */
function clearSubdivision() {
  view.segments = [];
  view.activeSegment = -1;
  lastSubdivideGeoJSON = null;
  $("segments-panel").hidden = true;
  $("segment-list").innerHTML = "";
  $("segment-count").textContent = "0";
  draw();
}

// 任一输入（地块 / 禁建区 / 数量 / 角度）改变时清除旧分段
for (const el of [parcelInput, forbiddenInput, $("subdivide-count"), $("subdivide-angle")]) {
  el.addEventListener("input", clearSubdivision);
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
  renderStats(null);
  renderIssues([]);
  resetRelations();
  clearSubdivision();
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
  renderStats(null);
  renderIssues([]);
  resetRelations();
  clearSubdivision();
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
  renderStats(null);
  renderIssues([]);
  resetRelations();
  clearSubdivision();
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

function fmtNum(x, digits = 6) {
  if (x == null || Number.isNaN(x)) return "—";
  return Number(x).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function renderSegments(data) {
  const panel = $("segments-panel");
  const list = $("segment-list");
  list.innerHTML = "";
  panel.hidden = false;
  $("segment-count").textContent = data.count;

  const cuts = data.cuts.map((c) => fmtNum(c)).join("，");
  $("segments-summary").textContent =
    `可建总面积 ${fmtArea(data.total_area)} · 每段目标 ${fmtArea(data.target_area)}` +
    `（容差 ±${fmtNum(data.area_tolerance)} m²）· 方向角 ${fmtNum(data.angle_deg)}°` +
    ` · 切线投影值：${cuts}`;

  data.segments.forEach((info, i) => {
    const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
    const li = document.createElement("li");
    li.style.borderLeftColor = color;
    li.dataset.index = i;

    const title = document.createElement("div");
    title.className = "segment-title";
    const sw = document.createElement("span");
    sw.className = "segment-swatch";
    sw.style.background = color;
    const name = document.createElement("span");
    name.textContent = `段 ${info.segment}`;
    const parts = document.createElement("span");
    parts.className = "relation-name";
    parts.textContent = info.part_count > 1 ? `${info.part_count} 个独立面` : "";
    title.appendChild(sw);
    title.appendChild(name);
    title.appendChild(parts);

    const metrics = document.createElement("div");
    metrics.className = "segment-metrics";
    const err = info.area_error_m2;
    metrics.textContent =
      `面积 ${fmtArea(info.area_m2)} · 目标 ${fmtArea(info.target_area_m2)}` +
      ` · 偏差 ${err >= 0 ? "+" : ""}${fmtNum(err)} m²` +
      ` · 投影 [${fmtNum(info.projection_range[0])}, ${fmtNum(info.projection_range[1])}]`;

    li.appendChild(title);
    li.appendChild(metrics);
    li.addEventListener("click", () => focusSegment(i));
    list.appendChild(li);
  });
  draw();
}

function focusSegment(i) {
  view.activeSegment = i;
  document.querySelectorAll("#segment-list li").forEach((li) => {
    li.classList.toggle("active", Number(li.dataset.index) === i);
  });
  if (!LAYERS.segments.show) {
    LAYERS.segments.show = true;
    $("show-segments").checked = true;
  }
  const seg = view.segments[i];
  const b = seg && boundsOf(seg.polys);
  if (b) {
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

async function runSubdivide() {
  $("json-panel").hidden = true;
  renderStats(null);
  renderIssues([]);
  resetRelations();
  clearSubdivision();
  lastResultGeoJSON = null;
  $("btn-download").disabled = true;
  $("btn-view-json").disabled = true;
  view.resultPolys = [];

  let payload;
  try {
    const { parcel, forbidden } = readInputs();
    const count = Number($("subdivide-count").value);
    const angle = Number($("subdivide-angle").value);
    if (!Number.isInteger(count) || count < 2 || count > 12) {
      throw new Error("分段数量必须是 2~12 的整数");
    }
    if (!Number.isFinite(angle) || angle < 0 || angle >= 180) {
      throw new Error("方向角必须是有限数值，且满足 0 ≤ 角度 < 180");
    }
    payload = { parcel, forbidden, count, angle_deg: angle };
  } catch (e) {
    setStatus(e.message, "error");
    return;
  }

  try {
    setPreviewLayers(payload);
  } catch {
    /* 预览失败不影响请求 */
  }

  setStatus("正在校验并按累计面积等分…");
  try {
    const resp = await fetch("/api/subdivide", {
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

    lastSubdivideGeoJSON = data.result;
    view.segments = (data.result.features || []).map((f) => ({
      polys: extractPolygons([f.geometry]),
      info: f.properties || {},
    }));
    view.activeSegment = -1;
    renderStats(data.areas);
    renderIssues([]);
    renderSegments(data);
    fitTo(currentBounds());
    setStatus(
      `分段完成：可建面积 ${fmtArea(data.total_area)} 等分为 ${data.count} 段，` +
        `每段目标 ${fmtArea(data.target_area)}（容差 ±${fmtNum(data.area_tolerance)} m²），` +
        `点击段列表可高亮`,
      "ok"
    );
  } catch (e) {
    setStatus(`请求失败：${e.message}`, "error");
  }
}

$("btn-subdivide").addEventListener("click", runSubdivide);

$("btn-download-subdivide").addEventListener("click", () => {
  if (!lastSubdivideGeoJSON || lastSubdivideGeoJSON.features.length === 0) return;
  const blob = new Blob([JSON.stringify(lastSubdivideGeoJSON, null, 2)], {
    type: "application/geo+json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.href = url;
  a.download = `subdivide-result-${ts}.geojson`;
  a.click();
  URL.revokeObjectURL(url);
});

$("btn-view-subdivide-json").addEventListener("click", () => {
  if (!lastSubdivideGeoJSON) return;
  const panel = $("json-panel");
  panel.hidden = !panel.hidden;
  $("json-view").textContent = JSON.stringify(lastSubdivideGeoJSON, null, 2);
  if (!panel.hidden) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

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
  const panel = $("json-panel");
  panel.hidden = !panel.hidden;
  $("json-view").textContent = JSON.stringify(lastResultGeoJSON, null, 2);
  if (!panel.hidden) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

$("clear-forbidden").addEventListener("click", () => {
  forbiddenInput.value = "";
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
