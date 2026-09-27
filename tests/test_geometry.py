"""geometry_ops 的行为测试（不依赖 pytest，直接断言）。"""

import sys

from app.geometry_ops import clip_parcel, validate_layer, validate_layer_indexed

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def expect_valid(obj, layer="forbidden"):
    geoms, issues = validate_layer(obj, layer)
    assert not issues, f"expected valid, got {issues}"
    return geoms


def expect_codes(obj, expected_codes, layer="parcel"):
    _, issues = validate_layer(obj, layer)
    codes = sorted(i["code"] for i in issues)
    assert codes == sorted(expected_codes), (
        f"codes mismatch\n got: {[(i['code'], i['message']) for i in issues]}\n exp: {expected_codes}"
    )
    return issues


# ---------- 1. 基础裁切：孔洞保留 ----------
parcel = {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}
hole = {"type": "Polygon", "coordinates": [[[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]]]}
pg = expect_valid(parcel, "parcel")
fg = expect_valid(hole)
r = clip_parcel(pg, fg)
check("hole: 单面带孔", len(r["parts"]) == 1 and len(list(r["parts"][0].interiors)) == 1)
check("hole: 剩余 9600", abs(r["areas"]["remaining_area"] - 9600.0) < 1e-6, r["areas"])
check("hole: 扣除 400", abs(r["areas"]["removed_area"] - 400.0) < 1e-6)

# ---------- 2. 拆成两个独立面 ----------
strip = {"type": "Polygon", "coordinates": [[[0, 40], [100, 40], [100, 60], [0, 60], [0, 40]]]}
r = clip_parcel(pg, expect_valid(strip))
check("split: MultiPolygon 两个面", len(r["parts"]) == 2)
check("split: 剩余 8000", abs(r["areas"]["remaining_area"] - 8000.0) < 1e-6)

# ---------- 3. 重叠禁建区只算一次 ----------
overlap1 = {"type": "Polygon", "coordinates": [[[50, 50], [150, 50], [150, 150], [50, 150], [50, 50]]]}
overlap2 = {"type": "Polygon", "coordinates": [[[60, 60], [70, 60], [70, 70], [60, 70], [60, 60]]]}
fg = expect_valid(
    {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": overlap1},
        {"type": "Feature", "properties": {}, "geometry": overlap2},
    ]}
)
r = clip_parcel(pg, fg)
check("dedup: 重叠只扣一次 7500", abs(r["areas"]["remaining_area"] - 7500.0) < 1e-6, r["areas"])
check("dedup: 禁建并集面积", abs(r["areas"]["forbidden_union_area"] - 10000.0) < 1e-6)

# ---------- 4. 共点、共边不扣面积 ----------
corner = {"type": "Polygon", "coordinates": [[[100, 100], [110, 100], [110, 110], [100, 110], [100, 100]]]}
edge = {"type": "Polygon", "coordinates": [[[20, -10], [80, -10], [80, 0], [20, 0], [20, -10]]]}
fg = expect_valid({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {}, "geometry": corner},
    {"type": "Feature", "properties": {}, "geometry": edge},
]})
r = clip_parcel(pg, fg)
check("touch: 共点共边扣 0", abs(r["areas"]["removed_area"]) < 1e-9, r["areas"])
check("touch: 剩余 10000", abs(r["areas"]["remaining_area"] - 10000.0) < 1e-9)

# ---------- 5. 完全扣空 ----------
full = {"type": "Polygon", "coordinates": [[[-10, -10], [110, -10], [110, 110], [-10, 110], [-10, -10]]]}
r = clip_parcel(pg, expect_valid(full))
check("empty: 无面", r["parts"] == [])
check("empty: 剩余 0", r["areas"]["remaining_area"] == 0.0)

# ---------- 6. 无禁建区 ----------
r = clip_parcel(pg, [])
check("noforb: 原样返回", len(r["parts"]) == 1 and abs(r["areas"]["remaining_area"] - 10000.0) < 1e-9)

# ---------- 7. 未闭合环：定位 ----------
issues = expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10]]]},
    ["RING_NOT_CLOSED"],
)
i = issues[0]
check("unclosed: 环序0", i["ring_index"] == 0 and i["ring"] == "outer")
check("unclosed: 尾点坐标", i["coordinate"] == [0.0, 10.0], str(i))

issues = expect_codes(
    {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[2, 2], [8, 2], [8, 8], [2, 8]],  # 孔未闭合
    ]},
    ["RING_NOT_CLOSED"],
)
check("unclosed: 孔洞环 ring=inner index=1",
      issues[0]["ring"] == "inner" and issues[0]["ring_index"] == 1, str(issues[0]))

# ---------- 8. 自交（蝴蝶结）：定位交点 ----------
# 蝴蝶结净鞋带面积恰好为 0，因此同时报零面积与自交
issues = expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]},
    ["RING_SELF_INTERSECTION", "ZERO_AREA_RING"],
)
si = [x for x in issues if x["code"] == "RING_SELF_INTERSECTION"][0]
check("selfinter: 坐标 1,1", si["coordinate"] == [1.0, 1.0], str(si))
check("selfinter: 给出点序号", isinstance(si["point_index"], int), str(si))

# 非零面积的自交（顶点 (5,-5) 使边穿过底边）
issues = expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [5, -5], [0, 0]]]},
    ["INVALID_POLYGON", "RING_SELF_INTERSECTION"],
)
si = [x for x in issues if x["code"] == "RING_SELF_INTERSECTION"][0]
check("selfinter nonzero: 交点 (3.333,0)",
      abs(si["coordinate"][0] - 10 / 3) < 1e-6 and si["coordinate"][1] == 0.0, str(si))

# ---------- 9. 共边重叠的自交 ----------
# 环在一条边上折返，产生重叠线段
issues = expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [4, 2], [2, 2], [2, 4], [0, 4], [0, 0]]]},
    ["INVALID_POLYGON", "RING_SELF_INTERSECTION"],
)
check("selfinter: 共边重叠被发现", len(issues) >= 1, str(issues))

# ---------- 10. 零面积 ----------
expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [5, 0], [10, 0], [0, 0]]]},
    ["ZERO_AREA_RING"],
)
expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]},
    ["ZERO_AREA_RING"],
)
expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0]]]},
    ["RING_TOO_FEW_POINTS"],
)
# 有效外环 + 零面积孔
issues = expect_codes(
    {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[2, 2], [5, 2], [8, 2], [2, 2]],
    ]},
    ["ZERO_AREA_RING"],
)
check("zeroarea: 孔环定位 inner/1", issues[0]["ring"] == "inner" and issues[0]["ring_index"] == 1)

# ---------- 11. 孔在环外 / 孔嵌套 ----------
expect_codes(
    {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]],
    ]},
    ["INVALID_POLYGON"],
)
issues = expect_codes(
    {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]],
        [[3, 3], [4, 3], [4, 4], [3, 4], [3, 3]],
    ]},
    ["INVALID_POLYGON"],
)
check("nested holes: 中文原因", "嵌套" in issues[0]["message"], issues[0]["message"])
check("nested holes: 有坐标", issues[0]["coordinate"] is not None, str(issues[0]))

# ---------- 12. MultiPolygon 成员重叠 ----------
issues = expect_codes(
    {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]],
    ]},
    ["INVALID_POLYGON"],
)
check("mp overlap: 定位 polygon_index", issues[0]["polygon_index"] in (0, 1), str(issues[0]))

# MultiPolygon 正常（相邻共边合法？GEOS 默认不允许共享边——验证一下）
geoms, issues = validate_layer(
    {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[10, 0], [20, 0], [20, 10], [10, 10], [10, 0]]],
    ]},
    "forbidden",
)
print(f"[INFO] MultiPolygon 共边相邻 validity issues = {[i['code'] for i in issues]}")

# ---------- 13. MultiPolygon 合法（分开两块）----------
geoms, issues = validate_layer(
    {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
    ]},
    "parcel",
)
check("mp valid: 无问题", not issues, str(issues))

# ---------- 14. 多个问题一次性报出 ----------
issues = expect_codes(
    {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Polygon",
         "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2]]]}},  # 未闭合+自交+净零面积
        {"type": "Feature", "geometry": {"type": "Polygon",
         "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]}},  # 零面积
    ]},
    ["RING_NOT_CLOSED", "RING_SELF_INTERSECTION", "ZERO_AREA_RING", "ZERO_AREA_RING"],
)
locs = {(i["feature_index"], i["code"]) for i in issues}
check("multi-issue: 归属正确要素",
      (0, "RING_NOT_CLOSED") in locs and (1, "ZERO_AREA_RING") in locs, str(locs))

# ---------- 15. 不支持的类型 / 坏坐标 ----------
expect_codes(
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}},
    ["UNSUPPORTED_GEOMETRY"],
)
issues = expect_codes(
    {"type": "Polygon", "coordinates": [[[0, 0], ["x", 0], [10, 10], [0, 0]]]},
    ["INVALID_COORDINATE"],
)
check("bad coord: 点序号1", issues[0]["point_index"] == 1, str(issues[0]))

# ---------- 16. 带 Z 坐标可工作（按 2D 米制） ----------
geoms, issues = validate_layer(
    {"type": "Polygon", "coordinates": [[[0, 0, 5], [10, 0, 5], [10, 10, 5], [0, 10, 5], [0, 0, 5]]]},
    "parcel",
)
check("3d coords: 接受", not issues, str(issues))

# ---------- 17. 顶层结构错误 ----------
from app.geometry_ops import GeoJSONError
for bad in [{"type": "LineString", "coordinates": []}, {"type": "Bogus"}]:
    try:
        validate_layer(bad, "parcel")
        check(f"structure reject {bad['type']}", False)
    except GeoJSONError:
        check(f"structure reject {bad['type']}", True)

# ---------- 18. Feature id 透传 ----------
issues = expect_codes(
    {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "lot-7", "geometry":
         {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]}},
    ]},
    ["ZERO_AREA_RING"],
)
check("feature id 透传", issues[0]["feature_id"] == "lot-7", str(issues[0]))

# ---------- 19. 禁建区超出地块 + 孔洞联合 ----------
# 左半：40×100 在地块内（超出 10）；小孔 20×40 完全在内
big_hole_left = {"type": "Polygon", "coordinates": [[[-10, -10], [40, -10], [40, 90], [-10, 90], [-10, -10]]]}
small_hole = {"type": "Polygon", "coordinates": [[[60, 30], [80, 30], [80, 70], [60, 70], [60, 30]]]}
fg = expect_valid({"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": big_hole_left},
    {"type": "Feature", "geometry": small_hole},
]})
r = clip_parcel(pg, fg)
# 扣 4000 + 800 = 4800；剩 5200... 注意左禁建 y 只到 90，顶部 x<40 留 40×10，
# 因此左侧实扣 40×90=3600，总扣 4400，剩 5600
check("mixed: 面积 5600", abs(r["areas"]["remaining_area"] - 5600.0) < 1e-6, str(r["areas"]))
# 左侧顶部仍与右侧连通，小孔在剩余面内部 → 单面带孔
check("mixed: 单面带孔", len(r["parts"]) == 1 and len(list(r["parts"][0].interiors)) == 1)

# ---------- 20. 裁切结果保持在地块内 ----------
from shapely.geometry import shape as shshape
parcel_shape = shshape(parcel)
r = clip_parcel(pg, fg)
for p in r["parts"]:
    assert p.difference(parcel_shape).area < 1e-9
check("result: 结果完全位于地块内", True)

# ---------- 21. 关系审计：六类关系 + 零面积接触 ----------
from app.geometry_ops import RELATIONS, audit_relations

parcel_obj = {"type": "Polygon",
              "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}
precs, pissues = validate_layer_indexed(parcel_obj, "parcel")
assert len(precs) == 1 and not pissues


def audit_feature(geom_obj):
    recs, issues = validate_layer_indexed(geom_obj, "forbidden")
    assert not issues, issues
    return audit_relations(precs, recs)[0]


# 21.1 disjoint：相离，距离为最近点净距
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[130, 0], [150, 0], [150, 20], [130, 20], [130, 0]]]})
check("rel: disjoint", row["relation"] == "disjoint", str(row))
check("rel: disjoint 交集0", row["intersection_area_m2"] == 0.0)
check("rel: disjoint 距离30", abs(row["boundary_distance_m"] - 30.0) < 1e-6, str(row))

# 21.2 touch：仅共点
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[100, 100], [110, 100], [110, 110], [100, 110], [100, 100]]]})
check("rel: 共点 touch", row["relation"] == "touch"
      and row["intersection_area_m2"] == 0.0 and row["boundary_distance_m"] == 0.0, str(row))

# 21.3 touch：共边（边部分重合）
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[20, -10], [80, -10], [80, 0], [20, 0], [20, -10]]]})
check("rel: 共边 touch", row["relation"] == "touch" and row["intersection_area_m2"] == 0.0, str(row))

# 21.4 touch：边完全重合也只算 touch
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[0, -10], [100, -10], [100, 0], [0, 0], [0, -10]]]})
check("rel: 整条共边 touch", row["relation"] == "touch" and row["intersection_area_m2"] == 0.0, str(row))

# 21.5 overlap：部分重叠，交集 20×20=400
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[80, 80], [120, 80], [120, 120], [80, 120], [80, 80]]]})
check("rel: overlap", row["relation"] == "overlap", str(row))
check("rel: overlap 交集400", abs(row["intersection_area_m2"] - 400.0) < 1e-6, str(row))
check("rel: overlap 距离0", row["boundary_distance_m"] == 0.0)

# 21.6 within：禁建区完全位于地块内，交集=自身面积，边界距离=10
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[10, 10], [40, 10], [40, 40], [10, 40], [10, 10]]]})
check("rel: within", row["relation"] == "within", str(row))
check("rel: within 交集900", abs(row["intersection_area_m2"] - 900.0) < 1e-6)
check("rel: within 边界距离10", abs(row["boundary_distance_m"] - 10.0) < 1e-6, str(row))

# 21.7 within：贴边的内嵌（与地块共边）仍是 within 而非 touch
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[10, 0], [40, 0], [40, 40], [10, 40], [10, 0]]]})
check("rel: 贴边 within", row["relation"] == "within", str(row))
check("rel: 贴边 within 交集1200", abs(row["intersection_area_m2"] - 1200.0) < 1e-6)
check("rel: 贴边 within 距离0", row["boundary_distance_m"] == 0.0)

# 21.8 contains：禁建区包住地块
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[-10, -10], [110, -10], [110, 110], [-10, 110], [-10, -10]]]})
check("rel: contains", row["relation"] == "contains", str(row))
check("rel: contains 交集10000", abs(row["intersection_area_m2"] - 10000.0) < 1e-6)
check("rel: contains 距离10", abs(row["boundary_distance_m"] - 10.0) < 1e-6)

# 21.9 equals：完全相等
row = audit_feature({"type": "Polygon",
                     "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]})
check("rel: equals", row["relation"] == "equals"
      and abs(row["intersection_area_m2"] - 10000.0) < 1e-6
      and row["boundary_distance_m"] == 0.0, str(row))

# 21.10 六类常量齐全
check("rel: 六类常量", RELATIONS == ("disjoint", "touch", "overlap", "within", "contains", "equals"))

# ---------- 22. 审计索引稳定性 / 排序 / 无效要素跳过 ----------
# 第 2 个要素无效；其余要素保留原始 FeatureCollection 下标，且按索引升序
mixed = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": "f0", "geometry":
     {"type": "Polygon", "coordinates": [[[130, 130], [140, 130], [140, 140], [130, 140], [130, 130]]]}},
    {"type": "Feature", "id": "f1-bad", "geometry":
     {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]}},
    {"type": "Feature", "id": "f2", "geometry":
     {"type": "Polygon", "coordinates": [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]]}},
]}
recs, issues = validate_layer_indexed(mixed, "forbidden")
check("rel: 无效要素进 issues", len(issues) == 1 and issues[0]["feature_index"] == 1)
rows = audit_relations(precs, recs)
check("rel: 跳过无效后剩2", [r["feature_index"] for r in rows] == [0, 2], str(rows))
check("rel: 索引0 disjoint", rows[0]["relation"] == "disjoint" and rows[0]["feature_id"] == "f0")
check("rel: 索引2 within", rows[1]["relation"] == "within" and rows[1]["feature_id"] == "f2")

# 倒序输入也必须按索引排序返回
rev = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry":
     {"type": "Polygon", "coordinates": [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]]}},
    {"type": "Feature", "geometry":
     {"type": "Polygon", "coordinates": [[[130, 130], [140, 130], [140, 140], [130, 140], [130, 130]]]}},
    {"type": "Feature", "geometry":
     {"type": "Polygon", "coordinates": [[[100, 100], [110, 100], [110, 110], [100, 110], [100, 100]]]}},
]}
recs, issues = validate_layer_indexed(rev, "forbidden")
rows = audit_relations(precs, recs)
check("rel: 按索引排序", [r["feature_index"] for r in rows] == [0, 1, 2])
check("rel: 排序后关系正确",
      [r["relation"] for r in rows] == ["within", "disjoint", "touch"], str(rows))

# ---------- 23. MultiPolygon 禁建区 ----------
mp = {"type": "MultiPolygon", "coordinates": [
    [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]],
    [[[120, 120], [130, 120], [130, 130], [120, 130], [120, 120]]],
]}
row = audit_feature(mp)
check("rel: MP 一块内一块外 overlap", row["relation"] == "overlap", str(row))
check("rel: MP 交集100", abs(row["intersection_area_m2"] - 100.0) < 1e-6)
check("rel: MP 边界距离10", abs(row["boundary_distance_m"] - 10.0) < 1e-6, str(row))

# 两块都在内部但与地块边界间距不同 -> within，距离取最近
mp_in = {"type": "MultiPolygon", "coordinates": [
    [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]],
    [[[50, 50], [60, 50], [60, 60], [50, 60], [50, 50]]],
]}
row = audit_feature(mp_in)
check("rel: MP 双内含 within", row["relation"] == "within"
      and abs(row["boundary_distance_m"] - 10.0) < 1e-6, str(row))

# ---------- 24. 无禁建区：空审计 ----------
check("rel: 空列表", audit_relations(precs, []) == [])

# ---------- 25. 等面积分段 ----------
from shapely.geometry import box as _box, shape as _shape
from shapely.ops import unary_union as _unary_union
from app.geometry_ops import subdivide_buildable

SUB_FAILS = []


def sub_check(name, cond, detail=""):
    check(name, cond, detail)
    if not cond:
        SUB_FAILS.append(name)


def _flatten(geom):
    return [geom] if geom.geom_type == "Polygon" else list(geom.geoms)


def _verify_subdivision(buildable, count, angle, tol_mult=1.0):
    """通用验证：面积等权、覆盖、两两零面积交集、孔洞/独立面保留。"""
    res = subdivide_buildable(buildable, count, angle)
    target = buildable.area / count
    tol = max(1e-6, buildable.area * 1e-8) * tol_mult

    sub_check(f"sub[{angle}°/{count}]: 返回段数", len(res["segments"]) == count)
    # 切线按投影递增
    sub_check(
        f"sub[{angle}°/{count}]: 切线递增",
        all(a < b for a, b in zip(res["cuts"], res["cuts"][1:])),
        str(res["cuts"]),
    )

    all_pieces = []
    for k, polys in enumerate(res["segments"]):
        sub_check(f"sub[{angle}°/{count}]: 第{k+1}段非空", len(polys) >= 1)
        a = sum(p.area for p in polys)
        sub_check(
            f"sub[{angle}°/{count}]: 第{k+1}段等面积",
            abs(a - target) <= tol,
            f"{a} vs {target}",
        )
        all_pieces.extend(polys)

    union_pieces = _unary_union(all_pieces)
    symdiff = float(buildable.symmetric_difference(union_pieces).area)
    # 覆盖差只来自 GEOS 浮点噪声（典型 1e-12～1e-28），用独立的几何容差判断
    cover_tol = max(1e-7, buildable.area * 1e-11)
    sub_check(
        f"sub[{angle}°/{count}]: 覆盖原范围",
        symdiff <= cover_tol,
        f"symdiff={symdiff}",
    )
    worst = 0.0
    for i in range(len(all_pieces)):
        for j in range(i + 1, len(all_pieces)):
            worst = max(worst, all_pieces[i].intersection(all_pieces[j]).area)
    sub_check(f"sub[{angle}°/{count}]: 两两交集面积0", worst <= tol, f"worst={worst}")
    return res


# 25.1 矩形 + 各种角度与段数
square = _box(0, 0, 100, 100)
for angle in [0, 15, 44.999, 45, 90, 135, 179.999]:
    _verify_subdivision(square, 5, angle)
for n in (2, 3, 7, 12):
    _verify_subdivision(square, n, 30)

# 25.2 投影空隙：两个分离方块 [0,10]² 与 [20,30]×[0,10]，x 方向
disconnected = _unary_union([_box(0, 0, 10, 10), _box(20, 0, 30, 10)])
res = _verify_subdivision(disconnected, 2, 0)
# 目标累计 100 恰落在空隙台阶段 [10,20] -> 取较小投影端 10
sub_check("sub 空隙: 取较小投影端10", res["cuts"] == [10.0], str(res["cuts"]))
# count=4：每段 50，切线投影 5 / 10 / 25；
# 目标累计 100 落空隙台阶 [10,20] 取较小端 10；目标 150 落在第二块内为 25
res = _verify_subdivision(disconnected, 4, 0)
sub_check("sub 空隙: 多切线取台阶端", res["cuts"] == [5.0, 10.0, 25.0], str(res["cuts"]))
sub_check("sub 空隙: 投影范围", res["projection_min"] == 0.0 and res["projection_max"] == 30.0)
# 各段实际面积全部 50
sub_check("sub 空隙: 四段等面积",
          all(abs(a - 50.0) < 1e-7 for a in res["actual_areas"]), str(res["actual_areas"]))

# 25.3 孔洞保留：切割线穿过孔洞时，孔洞被分到相邻段中
holy = _box(0, 0, 100, 100).difference(_box(40, 40, 60, 60))
res = _verify_subdivision(holy, 4, 0)
# 面积上每段 2400（含孔洞扣减），且扣减总面积仍为 400
sub_check("sub 孔洞: 各段2400",
          all(abs(a - 2400.0) < 1e-6 for a in res["actual_areas"]), str(res["actual_areas"]))
total_seg_area = sum(res["actual_areas"])
sub_check("sub 孔洞: 总面积9600", abs(total_seg_area - 9600.0) < 1e-6)
# 纵向切（90°）count=3：切线 33.33/66.67，孔洞 y∈[40,60] 完整落在中间段内
res = _verify_subdivision(holy, 3, 90)
total_holes = sum(len(list(p.interiors)) for polys in res["segments"] for p in polys)
sub_check("sub 孔洞: 完整孔洞保留在段内", total_holes >= 1, f"holes={total_holes}")
# 该中间段面积为 3200（含 400 的孔洞扣减）
mid_area = res["actual_areas"][1]
sub_check("sub 孔洞: 中间段面积3200", abs(mid_area - 3200.0) < 1e-6, str(mid_area))
# 切在孔洞边（x=40 与 x=60）：count=12 的切线含 40/60
res = _verify_subdivision(holy, 12, 0)
sub_check("sub 孔洞: 切线贴孔边40/60",
          any(abs(c - 40.0) < 1e-7 for c in res["cuts"])
          and any(abs(c - 60.0) < 1e-7 for c in res["cuts"]),
          str(res["cuts"]))

# 25.4 多个独立面（横条被挖掉）切割后段内保留多个独立面
striped = _box(0, 0, 100, 100).difference(_box(0, 40, 100, 60))
res = _verify_subdivision(striped, 4, 90)
# y 方向 4 段等面积（每段 2000）：投影区间 0~25 / 25~50 / 50~75 / 75~100，
# 仅第二段（25～50）同时穿过上下两块
multi_segments = [polys for polys in res["segments"] if len(polys) >= 2]
sub_check("sub 独立面: 跨空隙段含两个独立面", len(multi_segments) == 1,
          str([len(polys) for polys in res["segments"]]))

# 25.5 非轴对齐多边形（平行四边形），方向 0 等分
para = _shape({"type": "Polygon", "coordinates": [
    [[0, 0], [100, 0], [140, 60], [40, 60], [0, 0]]]})
_ = _verify_subdivision(para, 6, 0)
# 该形状带三角形端部，截面宽度随 x 非线性，等面积切线不应等距（证明不是等宽切带）
res = subdivide_buildable(para, 6, 0)
gaps = [b - a for a, b in zip([res["projection_min"], *res["cuts"]],
                              [*res["cuts"], res["projection_max"]])]
sub_check("sub 平行四边形: 非等宽切带", max(gaps) - min(gaps) > 1e-4, str(gaps))

# 25.6 分段方向向量随角度变化
res = subdivide_buildable(square, 3, 90)
sub_check("sub 方向: 90度", abs(res["direction"][0]) < 1e-12 and abs(res["direction"][1] - 1) < 1e-12)

print()
if failures:
    print(f"{len(failures)} 个失败: {failures}")
    sys.exit(1)
print("全部通过 ✔")
