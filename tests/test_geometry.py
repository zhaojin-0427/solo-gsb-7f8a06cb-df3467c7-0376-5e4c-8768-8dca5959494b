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

# ---------- 25. 等面积分段：基本切分 ----------
from app.geometry_ops import SubdivideError, subdivide_parcel, subdivide_area_tolerance
from shapely import union_all

sq100 = expect_valid(
    {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]},
    "parcel",
)

# 25.1 正方形 4 段、方向角 0：切线垂直于 +x，即 x = 25/50/75
r = subdivide_parcel(sq100, [], 4, 0.0)
check("sub: 切线 x=25/50/75", r["cuts"] == [25.0, 50.0, 75.0], str(r["cuts"]))
check("sub: 4 段", len(r["segments"]) == 4 and len(r["cuts"]) == 3)
check("sub: 总面积 10000", r["total_area"] == 10000.0 and r["target_area"] == 2500.0)
check("sub: 容差 max(1e-6, 1e-4)", abs(r["area_tolerance"] - 1e-4) < 1e-15)
check(
    "sub: 各段面积恰为目标",
    all(s["area"] == 2500.0 for s in r["segments"]),
    str([s["area"] for s in r["segments"]]),
)
# 段 1 是最左侧（投影最小）的条带
seg1 = r["segments"][0]["geom"]
check("sub: 段1投影递增编号", seg1.bounds[0] == 0.0 and seg1.bounds[2] == 25.0, str(seg1.bounds))

# 25.2 方向角 90：投影 = y，水平切带
r = subdivide_parcel(sq100, [], 4, 90.0)
check("sub: 90° 切线 y=25/50/75",
      all(abs(c - e) < 1e-9 for c, e in zip(r["cuts"], [25.0, 50.0, 75.0])), str(r["cuts"]))
seg1 = r["segments"][0]["geom"]
check("sub: 90° 段1在底部", seg1.bounds[1] < 1e-9 and abs(seg1.bounds[3] - 25.0) < 1e-9)

# 25.3 任意角度（45°）面积也在容差内
r = subdivide_parcel(sq100, [], 3, 45.0)
check("sub: 45° 面积容差",
      all(abs(s["area"] - 10000 / 3) <= r["area_tolerance"] for s in r["segments"]),
      str([s["area"] for s in r["segments"]]))

# 25.4 容差定义
check("sub: 容差函数", subdivide_area_tolerance(50.0) == 1e-6
      and subdivide_area_tolerance(1e8) == 1.0)

# ---------- 26. 等面积分段：合并覆盖与两两不交 ----------
for ang in (0.0, 30.0, 45.0, 90.0, 120.5, 179.9):
    r = subdivide_parcel(sq100, [], 5, ang)
    geoms = [s["geom"] for s in r["segments"]]
    sym = union_all(geoms).symmetric_difference(union_all(sq100)).area
    check(f"sub: {ang}° 合并覆盖原范围", sym < 1e-9, f"symdiff={sym}")
    ok_disjoint = all(
        geoms[i].intersection(geoms[j]).area == 0.0
        for i in range(len(geoms))
        for j in range(i + 1, len(geoms))
    )
    check(f"sub: {ang}° 两两交集面积为0", ok_disjoint)
    check(f"sub: {ang}° 面积容差",
          all(abs(s["area"] - 2000.0) <= r["area_tolerance"] for s in r["segments"]))

# ---------- 27. 等面积分段：空隙取较小投影端 ----------
two_squares = expect_valid(
    {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
    ]},
    "parcel",
)
# 目标 100 = 左正方形面积，累计面积在空隙 [10, 20] 上恒等于目标 → 取 10
r = subdivide_parcel(two_squares, [], 2, 0.0)
check("sub: 空隙切线取较小端 10", r["cuts"] == [10.0], str(r["cuts"]))
check("sub: 空隙两段各 100",
      abs(r["segments"][0]["area"] - 100.0) < 1e-9
      and abs(r["segments"][1]["area"] - 100.0) < 1e-9)
check("sub: 段1是左正方形", r["segments"][0]["geom"].bounds == (0.0, 0.0, 10.0, 10.0))

# ---------- 28. 等面积分段：禁建区扣除 + 孔洞保留 ----------
hole = expect_valid(
    {"type": "Polygon", "coordinates": [[[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]]]},
    "forbidden",
)
r = subdivide_parcel(sq100, hole, 3, 0.0)
check("sub: 先扣禁建区 9600", abs(r["total_area"] - 9600.0) < 1e-9)
check("sub: 扣孔后各段 3200",
      all(abs(s["area"] - 3200.0) <= r["area_tolerance"] for s in r["segments"]),
      str([s["area"] for s in r["segments"]]))
mid = r["segments"][1]["geom"]
mid_polys = [mid] if mid.geom_type == "Polygon" else list(mid.geoms)
check("sub: 中段保留孔洞", sum(len(p.interiors) for p in mid_polys) == 1)

# 禁建区把地块切成两半时，段内保留多个独立面
bar = expect_valid(
    {"type": "Polygon", "coordinates": [[[0, 40], [100, 40], [100, 60], [0, 60], [0, 40]]]},
    "forbidden",
)
r = subdivide_parcel(sq100, bar, 2, 0.0)
check("sub: 可建 8000 两段各 4000",
      all(abs(s["area"] - 4000.0) <= r["area_tolerance"] for s in r["segments"]))
check("sub: 段含多个独立面",
      any(s["part_count"] == 2 for s in r["segments"]),
      str([s["part_count"] for s in r["segments"]]))
# 合并后仍覆盖可建范围
buildable = union_all(sq100).difference(union_all(bar))
sym = union_all([s["geom"] for s in r["segments"]]).symmetric_difference(buildable).area
check("sub: 扣除后合并覆盖可建范围", sym < 1e-9, f"symdiff={sym}")

# ---------- 29. 等面积分段：三角形解析解 ----------
tri = expect_valid(
    {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [0, 100], [0, 0]]]},
    "parcel",
)
r = subdivide_parcel(tri, [], 2, 0.0)
# 理论切线 x* = 100·(1 − 1/√2)
check("sub: 三角形切线解析解",
      abs(r["cuts"][0] - 100 * (1 - 2 ** -0.5)) < 1e-6, str(r["cuts"]))
check("sub: 三角形两段各 2500",
      all(abs(s["area"] - 2500.0) <= r["area_tolerance"] for s in r["segments"]))

# ---------- 30. 等面积分段：count 上限 ----------
r = subdivide_parcel(sq100, [], 12, 60.0)
check("sub: count=12", len(r["segments"]) == 12 and len(r["cuts"]) == 11)
check("sub: count=12 面积容差",
      all(abs(s["area"] - 10000 / 12) <= r["area_tolerance"] for s in r["segments"]))

# ---------- 31. 等面积分段：大面积相对容差 ----------
big = expect_valid(
    {"type": "Polygon", "coordinates": [[[0, 0], [10000, 0], [10000, 10000], [0, 10000], [0, 0]]]},
    "parcel",
)
r = subdivide_parcel(big, [], 7, 45.0)
check("sub: 大面积容差 = 总面积×1e-8", r["area_tolerance"] == 1.0)
check("sub: 大面积各段容差内",
      all(abs(s["area"] - 1e8 / 7) <= r["area_tolerance"] for s in r["segments"]),
      str(max(abs(s["area"] - 1e8 / 7) for s in r["segments"])))

# ---------- 32. 等面积分段：错误情形 ----------
cover_all = expect_valid(
    {"type": "Polygon", "coordinates": [[[-1, -1], [101, -1], [101, 101], [-1, 101], [-1, -1]]]},
    "forbidden",
)
try:
    subdivide_parcel(sq100, cover_all, 2, 0.0)
    check("sub: 扣空报错", False)
except SubdivideError as exc:
    check("sub: 扣空报错", "扣除" in str(exc), str(exc))

for bad_count in (0, 1, 13, 100):
    try:
        subdivide_parcel(sq100, [], bad_count, 0.0)
        check(f"sub: count={bad_count} 拒绝", False)
    except SubdivideError:
        check(f"sub: count={bad_count} 拒绝", True)

for bad_angle in (-0.1, 180.0, 270.0, float("nan"), float("inf")):
    try:
        subdivide_parcel(sq100, [], 2, bad_angle)
        check(f"sub: angle={bad_angle} 拒绝", False)
    except SubdivideError:
        check(f"sub: angle={bad_angle} 拒绝", True)

# 边界角度合法
r = subdivide_parcel(sq100, [], 2, 0.0)
check("sub: angle=0 合法", len(r["segments"]) == 2)
r = subdivide_parcel(sq100, [], 2, 179.999)
check("sub: angle<180 合法", len(r["segments"]) == 2)

print()
if failures:
    print(f"{len(failures)} 个失败: {failures}")
    sys.exit(1)
print("全部通过 ✔")
