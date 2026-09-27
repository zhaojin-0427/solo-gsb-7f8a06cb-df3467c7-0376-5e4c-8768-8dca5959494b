"""GeoJSON 输入校验、地块裁切与等面积分段核心逻辑（平面米制坐标）。

设计要点：
- 坐标一律按平面米制解释，面积单位为平方米。
- 未闭合环 / 自交 / 零面积等问题必须精确定位（环序、坐标点、坐标值）。
- 禁建区先做并集再裁切：重叠部分只算一次；共点、共边不产生扣减
  （Shapely 的差集本身只扣正面积，天然满足该要求）。
- 等面积分段：先求可建范围，再按方向角用垂直平行切线等分，
  切线位置由累计面积反求（顶点投影事件 + 二分），不做等宽切带或栅格近似。
"""

from __future__ import annotations

import math
import re
from typing import Any

from shapely import is_valid_reason, union_all
from shapely.geometry import (
    LineString,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

# 小于该面积（平方米）视为零面积退化环
ZERO_AREA_EPS = 1e-9
# 关系审计中，交集面积不大于该阈值（平方米）视为零面积接触（共点 / 共边），
# 不得判为 overlap
RELATION_AREA_EPS = ZERO_AREA_EPS
# 坐标点去重 / 近点归属的容差
COORD_EPS = 1e-9

# 禁建区相对地块的六类空间关系（DE-9IM 归类，顺序即页面展示顺序）
RELATIONS = ("disjoint", "touch", "overlap", "within", "contains", "equals")


class GeoJSONError(Exception):
    """输入 GeoJSON 的结构性错误（非几何有效性问题）。"""


def _issue(
    layer: str,
    feature_index: int,
    feature_id: Any,
    code: str,
    message: str,
    *,
    polygon_index: int | None = None,
    ring: str | None = None,
    ring_index: int | None = None,
    point_index: int | None = None,
    coordinate: list[float] | None = None,
) -> dict:
    return {
        "layer": layer,
        "feature_index": feature_index,
        "feature_id": feature_id,
        "polygon_index": polygon_index,
        "ring": ring,  # "outer" | "inner"
        "ring_index": ring_index,
        "point_index": point_index,
        "coordinate": coordinate,
        "code": code,
        "message": message,
    }


# ---------------------------------------------------------------------------
# GeoJSON 解析
# ---------------------------------------------------------------------------

def _coords_to_ring(
    coords: Any,
    layer: str,
    feature_index: int,
    feature_id: Any,
    polygon_index: int,
    ring: str,
    ring_index: int,
    issues: list[dict],
) -> list[tuple[float, float]] | None:
    """校验单个坐标环的结构与闭合性，返回 (自动闭合后的) 2D 点串。"""
    if not isinstance(coords, list) or len(coords) < 4:
        issues.append(
            _issue(
                layer,
                feature_index,
                feature_id,
                "RING_TOO_FEW_POINTS",
                (
                    f"坐标环至少需要 4 个点（含闭合点），当前只有 "
                    f"{len(coords) if isinstance(coords, list) else 0} 个点"
                ),
                polygon_index=polygon_index,
                ring=ring,
                ring_index=ring_index,
            )
        )
        return None

    pts: list[tuple[float, float]] = []
    bad_coord = False
    for i, c in enumerate(coords):
        if (
            not isinstance(c, list)
            or len(c) < 2
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in c[:2])
        ):
            issues.append(
                _issue(
                    layer,
                    feature_index,
                    feature_id,
                    "INVALID_COORDINATE",
                    "坐标点必须是包含两个有限数字的数组 [x, y]",
                    polygon_index=polygon_index,
                    ring=ring,
                    ring_index=ring_index,
                    point_index=i,
                )
            )
            bad_coord = True
            continue
        pts.append((float(c[0]), float(c[1])))

    if bad_coord or len(pts) < 4:
        return None

    unclosed = not _same_point(pts[0], pts[-1])
    if unclosed:
        issues.append(
            _issue(
                layer,
                feature_index,
                feature_id,
                "RING_NOT_CLOSED",
                (
                    f"第 {ring_index} 个{('外环' if ring == 'outer' else '孔洞环')}"
                    f"未闭合：首点 {_fmt_pt(pts[0])} 与尾点 {_fmt_pt(pts[-1])} 不一致"
                ),
                polygon_index=polygon_index,
                ring=ring,
                ring_index=ring_index,
                point_index=len(coords) - 1,
                coordinate=list(pts[-1]),
            )
        )
        pts = [*pts, pts[0]]  # 复制闭合后继续后续检查，保证一次能报出更多问题

    return pts


def _same_point(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= COORD_EPS and abs(a[1] - b[1]) <= COORD_EPS


def _fmt_pt(p: tuple[float, float]) -> str:
    return f"[{p[0]:g}, {p[1]:g}]"


def _signed_area2(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        s += x1 * y2 - x2 * y1
    return s


def _all_collinear(pts: list[tuple[float, float]]) -> bool:
    """顶点是否全部落在同一条直线上。"""
    anchor = pts[0]
    direction = None
    for p in pts[1:]:
        if _same_point(p, anchor):
            continue
        if direction is None:
            direction = (p[0] - anchor[0], p[1] - anchor[1])
            continue
        cross = direction[0] * (p[1] - anchor[1]) - direction[1] * (p[0] - anchor[0])
        if abs(cross) > 1e-7:
            return False
    return True


def _scan_ring_self_intersections(
    pts: list[tuple[float, float]],
    layer: str,
    feature_index: int,
    feature_id: Any,
    polygon_index: int,
    ring: str,
    ring_index: int,
    issues: list[dict],
) -> None:
    """定位环上的自交点。

    用 STRtree 对线段做空间查询，逐对求交：
    - 两条边只在其公共顶点（同位置端点）相接时合法（含闭合环首尾相接）。
    - 其余任何接触都算自交：边中相交、非相邻边共点、共边重叠等。
    """
    # 去掉连续重复点（它们会产生退化线段）
    dedup: list[tuple[float, float]] = []
    for p in pts:
        if not dedup or not _same_point(dedup[-1], p):
            dedup.append(p)
    if len(dedup) < 4:
        return
    if not _same_point(dedup[0], dedup[-1]):
        dedup.append(dedup[0])

    # 去除闭合点后的唯一顶点少于 3 个时必为零面积退化环，
    # 其回边与原边"重叠"属于退化现象而非自交，交由零面积检查报错。
    unique_vertices = {(round(p[0], 12), round(p[1], 12)) for p in dedup[:-1]}
    if len(unique_vertices) < 3:
        return

    # 全部顶点共线也是退化环（回边必然与原边重叠），不报自交。
    if _all_collinear(dedup[:-1]):
        return

    n = len(dedup) - 1  # 线段数
    # 记录每条边的两个端点坐标。两条边若在同一个顶点（同位置端点）相接，
    # 则该接触点合法 —— 不能只按线段编号判断：STRtree 自查询会返回共享
    # 多边形正常顶点的非相邻线段，且闭合点在环中同时是首点与尾点。
    segments: list[LineString] = []
    seg_endpoints: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for i in range(n):
        p1, p2 = dedup[i], dedup[i + 1]
        if not _same_point(p1, p2):
            segments.append(LineString([p1, p2]))
            seg_endpoints.append((p1, p2))
    if len(segments) < 2:
        return

    tree = STRtree(segments)
    hits: dict[tuple[float, float], int] = {}

    def add_hit(p: tuple[float, float]) -> None:
        key = (round(p[0], 9), round(p[1], 9))
        hits[key] = hits.get(key, 0) + 1

    for i, seg in enumerate(segments):
        for j in tree.query(seg):
            j = int(j)
            if j <= i:
                continue
            other = segments[j]
            if not seg.intersects(other):
                continue
            (a0, a1), (b0, b1) = seg_endpoints[i], seg_endpoints[j]
            # 找出两边是否在公共端点相接（覆盖首尾闭合、连续重复点后的编号错位）
            shared = None
            if _same_point(a1, b0):
                shared = a1
            elif _same_point(a0, b1):
                shared = a0
            elif _same_point(a0, b0):
                shared = a0
            elif _same_point(a1, b1):
                shared = a1
            inter = seg.intersection(other)
            for p in _contact_points(inter):
                if shared is not None and _same_point(p, shared):
                    continue
                add_hit(p)

    for (qx, qy), _ in hits.items():
        idx = _nearest_vertex_index(dedup, (qx, qy))
        issues.append(
            _issue(
                layer,
                feature_index,
                feature_id,
                "RING_SELF_INTERSECTION",
                (
                    f"第 {ring_index} 个{('外环' if ring == 'outer' else '孔洞环')}"
                    f"在坐标 [{qx:g}, {qy:g}] 附近自交"
                ),
                polygon_index=polygon_index,
                ring=ring,
                ring_index=ring_index,
                point_index=idx,
                coordinate=[qx, qy],
            )
        )


def _contact_points(geom: BaseGeometry) -> list[tuple[float, float]]:
    """从两线段的相交结果中提取接触点（支持共边重叠的端点）。"""
    raw: list[tuple[float, float]] = []
    gtype = geom.geom_type
    if gtype == "Point":
        raw = [(geom.x, geom.y)]
    elif gtype == "MultiPoint":
        raw = [(g.x, g.y) for g in geom.geoms]
    elif gtype == "LineString":
        coords = list(geom.coords)
        raw = [(coords[0][0], coords[0][1]), (coords[-1][0], coords[-1][1])]
    elif gtype == "MultiLineString":
        for line in geom.geoms:
            coords = list(line.coords)
            raw.append((coords[0][0], coords[0][1]))
            raw.append((coords[-1][0], coords[-1][1]))
    return raw


def _nearest_vertex_index(
    pts: list[tuple[float, float]], target: tuple[float, float]
) -> int:
    best_i, best_d = 0, math.inf
    tx, ty = target
    for i, (x, y) in enumerate(pts):
        d = (x - tx) ** 2 + (y - ty) ** 2
        if d < best_d:
            best_d, best_i = d, i
    return best_i


_REASON_RE = re.compile(r"^(.*?)\s*\[(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\]\s*$")

_REASON_ZH = {
    "Self-intersection": "自相交",
    "Ring Self-intersection": "坐标环自相交",
    "Nested shells": "外环相互嵌套",
    "Duplicate Rings": "存在重复坐标环",
    "Too few points in geometry component": "几何顶点过少",
    "Invalid Coordinate": "非法坐标",
    "Ring is not closed": "坐标环未闭合",
    "Hole lies outside shell": "孔洞位于外环之外",
    "Holes are nested": "孔洞相互嵌套",
    "Interior is disconnected": "内部区域不连通",
    "Hole overlaps shell": "孔洞与外环重叠",
    "Multiple duplicate shells": "存在重复外环",
    "Invalid number of points in LinearRing": "LinearRing 点数非法",
}


def _parse_reason(reason: str) -> tuple[str, tuple[float, float] | None]:
    m = _REASON_RE.match(reason.strip())
    if not m:
        return reason.strip(), None
    head = m.group(1).strip()
    return head, (float(m.group(2)), float(m.group(3)))


def _check_polygon_rings(
    polygon_coords: Any,
    layer: str,
    feature_index: int,
    feature_id: Any,
    polygon_index: int,
    issues: list[dict],
) -> Polygon | None:
    """校验单个 Polygon 的全部坐标环并构造 Shapely Polygon。"""
    if not isinstance(polygon_coords, list) or not polygon_coords:
        issues.append(
            _issue(
                layer,
                feature_index,
                feature_id,
                "INVALID_POLYGON",
                "Polygon 的 coordinates 必须是非空数组（至少包含一个外环）",
                polygon_index=polygon_index,
            )
        )
        return None

    rings: list[list[tuple[float, float]]] = []
    for r_idx, raw_ring in enumerate(polygon_coords):
        ring = "outer" if r_idx == 0 else "inner"
        pts = _coords_to_ring(
            raw_ring,
            layer,
            feature_index,
            feature_id,
            polygon_index,
            ring,
            r_idx,
            issues,
        )
        if pts is None:
            continue

        area2 = abs(_signed_area2(pts))
        if area2 <= ZERO_AREA_EPS:
            issues.append(
                _issue(
                    layer,
                    feature_index,
                    feature_id,
                    "ZERO_AREA_RING",
                    (
                        f"第 {r_idx} 个{('外环' if ring == 'outer' else '孔洞环')}"
                        "面积为零（所有点共线或重合），无法构成有效面"
                    ),
                    polygon_index=polygon_index,
                    ring=ring,
                    ring_index=r_idx,
                    point_index=0,
                    coordinate=list(pts[0]),
                )
            )
            # 零面积环（如鞋带面积恰好抵消的蝴蝶结）仍可能存在自交，
            # 先完成自交扫描再跳过。
        _scan_ring_self_intersections(
            pts,
            layer,
            feature_index,
            feature_id,
            polygon_index,
            ring,
            r_idx,
            issues,
        )
        if area2 <= ZERO_AREA_EPS:
            continue

        rings.append(pts)

    if not rings:
        return None

    outer = rings[0]
    holes = rings[1:]
    try:
        return Polygon(outer, holes)
    except Exception as exc:  # pragma: no cover - 兜底
        issues.append(
            _issue(
                layer,
                feature_index,
                feature_id,
                "INVALID_POLYGON",
                f"无法构造面：{exc}",
                polygon_index=polygon_index,
                ring="outer",
            )
        )
        return None


def _check_geometry(
    geometry: Any,
    layer: str,
    feature_index: int,
    feature_id: Any,
    issues: list[dict],
) -> Polygon | MultiPolygon | None:
    gtype = geometry.get("type") if isinstance(geometry, dict) else None

    if gtype == "Polygon":
        return _check_polygon_rings(
            geometry.get("coordinates"),
            layer,
            feature_index,
            feature_id,
            0,
            issues,
        )

    if gtype == "MultiPolygon":
        raw_polys = geometry.get("coordinates")
        if not isinstance(raw_polys, list) or not raw_polys:
            issues.append(
                _issue(
                    layer,
                    feature_index,
                    feature_id,
                    "INVALID_POLYGON",
                    "MultiPolygon 的 coordinates 必须是非空数组",
                )
            )
            return None
        polys: list[Polygon] = []
        for p_idx, poly_coords in enumerate(raw_polys):
            poly = _check_polygon_rings(
                poly_coords,
                layer,
                feature_index,
                feature_id,
                p_idx,
                issues,
            )
            if poly is not None:
                polys.append(poly)
        if not polys:
            return None
        return MultiPolygon(polys)

    issues.append(
        _issue(
            layer,
            feature_index,
            feature_id,
            "UNSUPPORTED_GEOMETRY",
            f"仅支持 Polygon / MultiPolygon，收到的是 {gtype!r}",
        )
    )
    return None


def _iter_features(obj: dict, layer: str) -> list[tuple[int, Any, dict]]:
    """把 FeatureCollection / Feature / 裸 Geometry 统一成 (序号, id, geometry)。"""
    gtype = obj.get("type")
    if gtype == "FeatureCollection":
        feats = obj.get("features")
        if not isinstance(feats, list):
            raise GeoJSONError("FeatureCollection 的 features 必须是数组")
        out = []
        for i, f in enumerate(feats):
            if not isinstance(f, dict) or f.get("type") != "Feature":
                raise GeoJSONError(f"features[{i}] 不是合法的 Feature")
            geom = f.get("geometry")
            if not isinstance(geom, dict):
                raise GeoJSONError(f"features[{i}] 缺少 geometry")
            out.append((i, f.get("id"), geom))
        return out

    if gtype == "Feature":
        geom = obj.get("geometry")
        if not isinstance(geom, dict):
            raise GeoJSONError("Feature 缺少 geometry")
        return [(0, obj.get("id"), geom)]

    if gtype in ("Polygon", "MultiPolygon"):
        return [(0, None, obj)]

    raise GeoJSONError(
        f"顶层类型必须是 FeatureCollection / Feature / Polygon / MultiPolygon，"
        f"收到 {gtype!r}"
    )


def validate_layer_indexed(
    obj: dict, layer: str
) -> tuple[list[tuple[int, Any, Polygon | MultiPolygon]], list[dict]]:
    """校验一个输入图层，返回 ((要素序号, 要素 id, 几何), ..., 问题列表)。

    要素序号沿用 GeoJSON 中 FeatureCollection 的原始下标（裸 Feature /
    裸几何为 0），被判定无效的要素不进入结果列表，因此审计结果可以稳定地
    引用回原始要素。
    """
    features = _iter_features(obj, layer)
    records: list[tuple[int, Any, Polygon | MultiPolygon]] = []
    issues: list[dict] = []

    for feat_index, feat_id, geom in features:
        built = _check_geometry(geom, layer, feat_index, feat_id, issues)
        if built is None:
            continue

        # 坐标环级别的检查之外，再用 GEOS 做面级 / 多面级有效性校验
        # （孔洞在外环外、孔洞嵌套、外环重叠等）。
        reason = is_valid_reason(built)
        if reason and reason != "Valid Geometry":
            head, coord = _parse_reason(reason)
            polygon_index = None
            point_index = None
            if coord is not None:
                polygon_index, point_index = _locate(built, coord)
            zh = _REASON_ZH.get(head, head)
            issues.append(
                _issue(
                    layer,
                    feat_index,
                    feat_id,
                    "INVALID_POLYGON",
                    f"{zh}（GEOS：{head}）"
                    + (f"，发生在 [{coord[0]:g}, {coord[1]:g}]" if coord else ""),
                    polygon_index=polygon_index,
                    point_index=point_index,
                    coordinate=list(coord) if coord else None,
                )
            )
            continue

        records.append((feat_index, feat_id, built))

    return records, issues


def validate_layer(
    obj: dict, layer: str
) -> tuple[list[Polygon | MultiPolygon], list[dict]]:
    """校验一个输入图层，返回 (几何列表, 问题列表)。"""
    records, issues = validate_layer_indexed(obj, layer)
    return [geom for _, _, geom in records], issues


def _locate(
    geom: Polygon | MultiPolygon, coord: tuple[float, float]
) -> tuple[int | None, int | None]:
    """把 GEOS 报错坐标归属到最近的 polygon 成员与顶点。"""
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    best_poly, best_idx, best_d = None, None, math.inf
    for p_idx, poly in enumerate(polys):
        ring = poly.exterior
        for i, c in enumerate(ring.coords):
            d = (c[0] - coord[0]) ** 2 + (c[1] - coord[1]) ** 2
            if d < best_d:
                best_d, best_poly, best_idx = d, p_idx, i
    return best_poly, best_idx


# ---------------------------------------------------------------------------
# 裁切
# ---------------------------------------------------------------------------

def _as_polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_as_polygons(g))
        return out
    return []


def _linear_length(geom: BaseGeometry) -> float:
    """几何中全部线状成分的总长度（点 / 空 / 面状成分计 0）。"""
    if geom.is_empty:
        return 0.0
    gtype = geom.geom_type
    if gtype in ("LineString", "LinearRing"):
        return float(geom.length)
    if gtype == "MultiLineString":
        return float(geom.length)
    if gtype == "GeometryCollection":
        return sum(_linear_length(g) for g in geom.geoms)
    return 0.0


def clip_parcel(
    parcel_geoms: list[Polygon | MultiPolygon],
    forbidden_geoms: list[Polygon | MultiPolygon],
) -> dict:
    """合并全部禁建区后从地块中扣除，返回结果几何与面积统计。"""
    if len(parcel_geoms) != 1:  # validate 层已保证地块恰好一个要素
        raise GeoJSONError("地块必须恰好包含一个 Polygon / MultiPolygon 要素")
    parcel = parcel_geoms[0]
    parcel_area = parcel.area

    if forbidden_geoms:
        # union_all 自动去重重叠：任意区域在并集中只出现一次
        forbidden_union = union_all(forbidden_geoms)
        result = parcel.difference(forbidden_union)
        forbidden_area = float(forbidden_union.area)
    else:
        result = parcel
        forbidden_area = 0.0

    # 差集对共点 / 共边不扣任何正面积；这里只保留真正的面。
    parts = [p for p in _as_polygons(result) if p.area > 0.0]
    remaining_area = sum(p.area for p in parts)

    return {
        "parts": parts,
        "areas": {
            "parcel_area": float(parcel_area),
            "forbidden_union_area": forbidden_area,
            "removed_area": float(parcel_area - remaining_area),
            "remaining_area": float(remaining_area),
        },
    }


# ---------------------------------------------------------------------------
# 禁建区关系审计
# ---------------------------------------------------------------------------

def _classify_relation(
    parcel: Polygon | MultiPolygon,
    forbidden: Polygon | MultiPolygon,
    intersection: BaseGeometry | None,
) -> str:
    """判定禁建区相对地块的六类关系之一。

    判定顺序（互斥归类，不使用 Shapely 的 overlaps —— 它不覆盖仅一方落在
    对方内部的情形，也无法表达零面积接触）：

    - equals：两者几何完全相等（面积相等）
    - within：禁建区完全位于地块内部（允许触边，且不是 equals）
    - contains：地块完全位于禁建区内部（允许触边，且不是 equals）
    - disjoint：两者完全不接触
    - touch：仅共点 / 共边接触，交集面积为零（零面积交集不算 overlap）
    - overlap：存在正面积交集且互不为覆盖
    """
    if parcel.equals(forbidden):
        return "equals"
    if forbidden.within(parcel):
        return "within"
    if forbidden.contains(parcel):
        return "contains"
    if not parcel.intersects(forbidden):
        return "disjoint"

    if intersection is None:
        intersection = parcel.intersection(forbidden)
    # 共点（Point/MultiPoint）、共边（LineString/MultiLineString）的交集
    # 本身没有面积；边内穿入再穿出的退化情形也以正面积阈值兜底，
    # 保证零面积交集不会被判成 overlap。
    if float(intersection.area) <= RELATION_AREA_EPS:
        return "touch"
    return "overlap"


def audit_relations(
    parcel_records: list[tuple[int, Any, Polygon | MultiPolygon]],
    forbidden_records: list[tuple[int, Any, Polygon | MultiPolygon]],
) -> list[dict]:
    """逐个禁建区判定相对地块的空间关系。

    返回按原始要素索引稳定排序的结果列表，每项包含：
    feature_index / feature_id / relation / intersection_area_m2 /
    boundary_distance_m。
    """
    if len(parcel_records) != 1:  # validate 层已保证地块恰好一个要素
        raise GeoJSONError("地块必须恰好包含一个 Polygon / MultiPolygon 要素")
    parcel = parcel_records[0][2]

    rows: list[dict] = []
    for feat_index, feat_id, forbidden in forbidden_records:
        # 只做一次 intersects/交集计算，distance 作用在两边的边界上：
        # 内含 / 包含时内部与边界的间距为 0 之外的真实缝隙，脱离（disjoint）
        # 时给出两个面之间的净距，共点 / 共边时为 0。
        if parcel.intersects(forbidden):
            intersection = parcel.intersection(forbidden)
            intersection_area = max(0.0, float(intersection.area))
        else:
            intersection = None
            intersection_area = 0.0

        relation = _classify_relation(parcel, forbidden, intersection)
        boundary_distance = float(parcel.boundary.distance(forbidden.boundary))

        rows.append(
            {
                "feature_index": feat_index,
                "feature_id": feat_id,
                "relation": relation,
                "intersection_area_m2": round(intersection_area, 6),
                "boundary_distance_m": round(max(0.0, boundary_distance), 6),
            }
        )

    rows.sort(key=lambda r: r["feature_index"])
    return rows


# ---------------------------------------------------------------------------
# 等面积分段
# ---------------------------------------------------------------------------

# 分段数量允许范围
SUBDIVIDE_MIN_COUNT = 2
SUBDIVIDE_MAX_COUNT = 12
# 每段面积与目标面积（可建总面积 / count）允许的最大偏差：
# max(1e-6 平方米, 总面积 × 1e-8)
SUBDIVIDE_ABS_TOL = 1e-6
SUBDIVIDE_REL_TOL = 1e-8


class SubdivideError(Exception):
    """等面积分段的前置条件不满足（如可建范围为空、参数越界）。"""


def subdivide_area_tolerance(total_area: float) -> float:
    """每段面积允许的最大偏差：max(1e-6 平方米, 总面积 × 1e-8)。"""
    return max(SUBDIVIDE_ABS_TOL, total_area * SUBDIVIDE_REL_TOL)


def _iter_rings(geom: BaseGeometry):
    """遍历 Polygon / MultiPolygon 的全部坐标环（外环与孔洞环）。"""
    if isinstance(geom, Polygon):
        yield geom.exterior
        yield from geom.interiors
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            yield from _iter_rings(poly)


def subdivide_parcel(
    parcel_geoms: list[Polygon | MultiPolygon],
    forbidden_geoms: list[Polygon | MultiPolygon],
    count: int,
    angle_deg: float,
) -> dict:
    """先扣除禁建区得到可建范围，再按方向角做等面积分段。

    - 方向：从 +x 轴逆时针旋转 angle_deg 度的单位向量 u；
    - 切线：垂直于 u 的平行线，即投影 p = x·ux + y·uy 的等值线；
    - 分段：按投影递增编号，第 k 条切线由累计面积 k·总面积/count 反求
      （顶点投影事件区间内累计面积是二次函数，解析求根，异常时二分兜底；
      禁止等宽切带 / 栅格近似）；
    - 累计面积在空隙上恰等于目标时，取空隙的较小投影端；
    - 每段面积与目标面积的偏差 ≤ max(1e-6 平方米, 总面积 × 1e-8)。
    """
    if not (SUBDIVIDE_MIN_COUNT <= count <= SUBDIVIDE_MAX_COUNT):
        raise SubdivideError(
            f"分段数量必须是 {SUBDIVIDE_MIN_COUNT}~{SUBDIVIDE_MAX_COUNT} 的整数"
        )
    if not math.isfinite(angle_deg) or not (0.0 <= angle_deg < 180.0):
        raise SubdivideError("方向角必须是有限数值，且满足 0 ≤ angle_deg < 180")

    # 先计算可建范围（复用裁切：禁建区并集差集）
    clip = clip_parcel(parcel_geoms, forbidden_geoms)
    parts = clip["parts"]
    if not parts:
        raise SubdivideError("地块被禁建区完全扣除，可建范围为 0，无法进行等面积分段")
    region = parts[0] if len(parts) == 1 else union_all(parts)
    total = float(region.area)
    if total <= 0.0:
        raise SubdivideError("可建范围面积为 0，无法进行等面积分段")

    theta = math.radians(angle_deg)
    ux, uy = math.cos(theta), math.sin(theta)
    vx, vy = -uy, ux  # u 逆时针 90°，与 u 构成 (投影, 垂直) 正交坐标系

    # 全部顶点在 u 方向上的投影：累计面积函数 A(t) 在每个相邻投影对之间
    # 是二次光滑段，顶点投影即反求切线所需的全部“事件点”。
    projections: list[float] = []
    perpendiculars: list[float] = []
    for ring in _iter_rings(region):
        for x, y in ring.coords:
            projections.append(x * ux + y * uy)
            perpendiculars.append(x * vx + y * vy)
    events = sorted(set(projections))
    pmin, pmax = events[0], events[-1]
    qmin, qmax = min(perpendiculars), max(perpendiculars)

    span_p = pmax - pmin
    span_q = qmax - qmin
    pad = max(span_p, span_q, 1.0)  # 切带矩形外扩量，保证完整覆盖可建范围
    q_lo, q_hi = qmin - pad, qmax + pad

    def slab(t_lo: float, t_hi: float) -> Polygon:
        """(投影, 垂直) 坐标系下的矩形 [t_lo, t_hi] × [q_lo, q_hi]。"""
        return Polygon(
            [
                (t_lo * ux + q_lo * vx, t_lo * uy + q_lo * vy),
                (t_lo * ux + q_hi * vx, t_lo * uy + q_hi * vy),
                (t_hi * ux + q_hi * vx, t_hi * uy + q_hi * vy),
                (t_hi * ux + q_lo * vx, t_hi * uy + q_lo * vy),
            ]
        )

    def area_upto(t: float) -> float:
        """累计面积 A(t)：可建范围中投影 ≤ t 部分的面积。"""
        return float(region.intersection(slab(pmin - pad, t)).area)

    def section_width(t: float) -> float:
        """截面宽度 w(t)：可建范围与投影等值线 p = t 的交集总长度。"""
        line = LineString(
            [
                (t * ux + q_lo * vx, t * uy + q_lo * vy),
                (t * ux + q_hi * vx, t * uy + q_hi * vy),
            ]
        )
        cross = region.intersection(line)
        return float(_linear_length(cross))

    tol = subdivide_area_tolerance(total)
    # “恰等于”判定容差：远小于分段容差，只用于识别落在空隙上的目标
    eq_eps = max(1e-9, total * 1e-12)

    # 每个事件点的累计面积，用于定位目标所在区间并识别空隙平台
    event_areas = [area_upto(e) for e in events]
    for i in range(1, len(event_areas)):  # 数值上强制非递减
        if event_areas[i] < event_areas[i - 1]:
            event_areas[i] = event_areas[i - 1]

    width_bound = span_q  # 截面宽度上界：dA/dt ≤ 垂直方向跨度

    def invert_bisect(lo: float, hi: float, target: float) -> float:
        """二分反求（兜底）：保证 |A(t) - target| ≤ tol / 4。"""
        for _ in range(200):
            if (hi - lo) * width_bound <= tol * 0.25:
                break
            mid = 0.5 * (lo + hi)
            if area_upto(mid) >= target:
                hi = mid
            else:
                lo = mid
        return 0.5 * (lo + hi)

    def invert(lo: float, hi: float, target: float) -> float:
        """在 (lo, hi) 内按累计面积反求切线投影（禁止等宽切带 / 栅格近似）。

        相邻事件点之间没有顶点投影，截面宽度 w 是 t 的线性函数，
        累计面积 A 是 t 的二次函数：在区间内部取两个截面宽度样本即可
        精确还原该二次函数并解析求根；数值异常时退化为二分。
        （端点处 w 可能因与边界共线而跳变，故样本必须取在开区间内部。）
        """
        h = hi - lo
        area_lo = area_upto(lo)
        delta = target - area_lo
        t1, t2 = lo + h / 3.0, lo + 2.0 * h / 3.0
        w1, w2 = section_width(t1), section_width(t2)
        slope = (w2 - w1) / (t2 - t1)  # w(t) = w0 + slope·(t - lo)
        w0 = w1 - slope * (t1 - lo)

        cut = None
        if abs(slope) <= 1e-15 * max(1.0, abs(w0)):
            if w0 > 0.0:
                cut = lo + delta / w0
        else:
            # (slope/2)·s² + w0·s - delta = 0，取落在 (lo, hi) 内的根
            disc = w0 * w0 + 2.0 * slope * delta
            if disc >= 0.0:
                root = math.sqrt(disc)
                for cand in ((-w0 + root) / slope, (-w0 - root) / slope):
                    if 0.0 < cand < h:
                        cut = lo + cand
                        break
        if cut is not None and abs(area_upto(cut) - target) <= tol * 0.125:
            return cut
        return invert_bisect(lo, hi, target)

    cuts: list[float] = []
    for k in range(1, count):
        target = total * k / count
        cut = None
        for i, a in enumerate(event_areas):
            if abs(a - target) <= eq_eps:
                # 累计面积在空隙上恰等于目标：取空隙的较小投影端。
                # 事件升序遍历，首次命中即累计面积首次达到目标的位置。
                cut = events[i]
                break
            if a > target:
                cut = invert(events[i - 1], events[i], target)
                break
        if cut is None:  # pragma: no cover - target < total 时必然命中
            cut = events[-1]
        cuts.append(cut)

    # 用相邻切线围成的切带裁出各段：相邻段只共享切线（零面积），
    # 全部切带铺满投影范围，合并后恰好覆盖原可建范围。
    bounds = [pmin - pad, *cuts, pmax + pad]
    segments: list[dict] = []
    for k in range(count):
        seg = region.intersection(slab(bounds[k], bounds[k + 1]))
        seg_polys = [p for p in _as_polygons(seg) if p.area > 0.0]
        if not seg_polys:  # pragma: no cover - 每段面积 ≈ 总面积/count > 0
            raise SubdivideError(f"第 {k + 1} 段为空，无法完成等面积分段")
        geom_out = seg_polys[0] if len(seg_polys) == 1 else union_all(seg_polys)
        area = float(sum(p.area for p in seg_polys))
        segments.append(
            {
                "segment": k + 1,
                "geom": geom_out,
                "area": area,
                "target": total / count,
                "part_count": len(seg_polys),
                "projection_range": [
                    cuts[k - 1] if k > 0 else pmin,
                    cuts[k] if k < count - 1 else pmax,
                ],
            }
        )

    return {
        "segments": segments,
        "cuts": cuts,
        "count": count,
        "angle_deg": angle_deg,
        "direction": [ux, uy],
        "total_area": total,
        "target_area": total / count,
        "area_tolerance": tol,
        "areas": clip["areas"],
    }
