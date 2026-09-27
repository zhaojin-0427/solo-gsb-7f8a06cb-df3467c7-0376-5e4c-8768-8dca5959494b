"""FastAPI 端点端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# 健康检查
r = client.get("/api/health")
check("health", r.status_code == 200 and r.json()["status"] == "ok")

# 首页
r = client.get("/")
check("index html", r.status_code == 200 and b"<!DOCTYPE html>" in r.content)

# 静态资源
r = client.get("/static/app.js")
check("static js", r.status_code == 200)

parcel = {"type": "Polygon", "coordinates": [[[0, 0], [120, 0], [120, 80], [0, 80], [0, 0]]]}
forbidden = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "A"}, "geometry":
     {"type": "Polygon", "coordinates": [[[20, 20], [55, 20], [55, 55], [20, 55], [20, 20]]]}},
    {"type": "Feature", "properties": {"name": "B"}, "geometry":
     {"type": "Polygon", "coordinates": [[[45, 30], [80, 30], [80, 60], [45, 60], [45, 30]]]}},
]}

# 正常裁切
r = client.post("/api/clip", json={"parcel": parcel, "forbidden": forbidden})
data = r.json()
check("clip ok", r.status_code == 200 and data["ok"], str(data)[:300])
check("clip parcel area 9600", abs(data["areas"]["parcel_area"] - 9600.0) < 1e-6, str(data["areas"]))
check("clip result FC", data["result"]["type"] == "FeatureCollection")
check("clip features nonempty", len(data["result"]["features"]) >= 1)

# 校验孔洞保留
import json
result_geoms = [f["geometry"] for f in data["result"]["features"]]
holes = sum(len(g.get("coordinates", [])) - 1 for g in result_geoms if g["type"] == "Polygon")
check("clip 孔洞保留", holes >= 1, json.dumps(result_geoms)[:300])

# 无禁建区
r = client.post("/api/clip", json={"parcel": parcel})
d = r.json()
check("no forbidden", d["ok"] and abs(d["areas"]["remaining_area"] - 9600.0) < 1e-6)

# 完全扣空 -> 空 FeatureCollection
cover = {"type": "Polygon", "coordinates": [[[-1, -1], [121, -1], [121, 81], [-1, 81], [-1, -1]]]}
r = client.post("/api/clip", json={"parcel": parcel, "forbidden": cover})
d = r.json()
check("empty: ok", d["ok"])
check("empty: 0 features", d["feature_count"] == 0 and d["result"]["features"] == [])
check("empty: remaining 0", d["areas"]["remaining_area"] == 0.0)

# MultiPolygon 地块
mp = {"type": "MultiPolygon", "coordinates": [
    [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
]}
r = client.post("/api/clip", json={"parcel": mp})
d = r.json()
check("multipolygon parcel", d["ok"] and abs(d["areas"]["parcel_area"] - 200.0) < 1e-6, str(d)[:200])

# 无效输入：未闭合 + 自交 + 零面积
bad = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": "p1", "geometry":
     {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2]]]}},
    {"type": "Feature", "geometry":
     {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]}},
]}
r = client.post("/api/clip", json={"parcel": bad})
d = r.json()
check("invalid: not ok", r.status_code == 200 and not d["ok"])
check("invalid: 多个定位问题", len(d["issues"]) >= 3, str([(i["code"]) for i in d["issues"]]))
codes = {i["code"] for i in d["issues"]}
check("invalid: 覆盖三类", {"RING_NOT_CLOSED", "RING_SELF_INTERSECTION", "ZERO_AREA_RING"} <= codes, str(codes))
unclosed = [i for i in d["issues"] if i["code"] == "RING_NOT_CLOSED"][0]
check("invalid: 坐标定位", unclosed["coordinate"] == [0.0, 2.0], str(unclosed))
check("invalid: feature 归属", unclosed["feature_index"] == 0 and unclosed["feature_id"] == "p1")
si = [i for i in d["issues"] if i["code"] == "RING_SELF_INTERSECTION"][0]
check("invalid: 自交坐标", si["coordinate"] == [1.0, 1.0], str(si))

# 禁建区无效但地块有效
r = client.post("/api/clip", json={"parcel": parcel, "forbidden": bad})
d = r.json()
check("forbidden invalid 也报错", not d["ok"] and all(i["layer"] == "forbidden" for i in d["issues"]))

# 结构错误 JSON
r = client.post("/api/clip", content=b"{not json", headers={"Content-Type": "application/json"})
check("malformed json: 422", r.status_code == 422)

# 缺地块
r = client.post("/api/clip", json={"forbidden": None})
check("missing parcel: 422", r.status_code == 422)

# 顶层类型错误
r = client.post("/api/clip", json={"parcel": {"type": "Point", "coordinates": [1, 2]}})
d = r.json()
check("top-level Point: 结构化错误", d["ok"] is False and "结构错误" in d["error"], str(d["error"]))

# 非面 Feature
r = client.post("/api/clip", json={"parcel": {"type": "Feature",
    "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}})
d = r.json()
check("LineString feature: issue", not d["ok"] and d["issues"][0]["code"] == "UNSUPPORTED_GEOMETRY")

# 孔在环外
hole_out = {"type": "Polygon", "coordinates": [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]],
]}
r = client.post("/api/clip", json={"parcel": hole_out})
d = r.json()
check("hole outside", not d["ok"] and d["issues"][0]["code"] == "INVALID_POLYGON"
      and "孔洞位于外环之外" in d["issues"][0]["message"], str(d["issues"][0]))

# ============================ /api/relations ============================

rel_parcel = {"type": "Polygon",
              "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}

def rel_poly(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}

rel_forbidden = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": "far", "properties": {"name": "相离"},
     "geometry": rel_poly(130, 130, 150, 150)},                 # disjoint
    {"type": "Feature", "properties": {"name": "共点"},
     "geometry": rel_poly(100, 100, 110, 110)},                 # touch
    {"type": "Feature", "properties": {"name": "共边"},
     "geometry": rel_poly(20, -10, 80, 0)},                     # touch
    {"type": "Feature", "properties": {"name": "重叠"},
     "geometry": rel_poly(80, 80, 120, 120)},                   # overlap
    {"type": "Feature", "properties": {"name": "内含"},
     "geometry": rel_poly(10, 10, 40, 40)},                     # within
    {"type": "Feature", "properties": {"name": "包住"},
     "geometry": rel_poly(-10, -10, 110, 110)},                 # contains
    {"type": "Feature", "properties": {"name": "一致"},
     "geometry": rel_poly(0, 0, 100, 100)},                     # equals
]}
r = client.post("/api/relations", json={"parcel": rel_parcel, "forbidden": rel_forbidden})
d = r.json()
check("relations ok", r.status_code == 200 and d["ok"], str(d)[:300])
check("relations 7 项", d["feature_count"] == 7 and len(d["relations"]) == 7)
by_idx = {row["feature_index"]: row for row in d["relations"]}
check("relations 索引连续排序",
      [row["feature_index"] for row in d["relations"]] == list(range(7)))
expected = ["disjoint", "touch", "touch", "overlap", "within", "contains", "equals"]
check("relations 六类判定",
      [row["relation"] for row in d["relations"]] == expected,
      str([(i, row["relation"]) for i, row in by_idx.items()]))

# 数值字段
check("relations disjoint 距离",
      abs(by_idx[0]["boundary_distance_m"] - (30 * 2 ** 0.5)) < 1e-6, str(by_idx[0]))
check("relations touch 零面积零距离",
      by_idx[1]["intersection_area_m2"] == 0.0 and by_idx[1]["boundary_distance_m"] == 0.0)
check("relations overlap 交集400",
      abs(by_idx[3]["intersection_area_m2"] - 400.0) < 1e-6)
check("relations within 边界距离10",
      abs(by_idx[4]["boundary_distance_m"] - 10.0) < 1e-6, str(by_idx[4]))
check("relations contains 交集=地块面积",
      abs(by_idx[5]["intersection_area_m2"] - 10000.0) < 1e-6)
check("relations equals",
      by_idx[6]["relation"] == "equals" and by_idx[6]["boundary_distance_m"] == 0.0)
check("relations 稳定字段",
      all(set(row) == {"feature_index", "feature_id", "relation",
                       "intersection_area_m2", "boundary_distance_m"}
          for row in d["relations"]))
check("relations feature_id 透传", by_idx[0]["feature_id"] == "far")

# 无禁建区 / null
r = client.post("/api/relations", json={"parcel": rel_parcel})
d = r.json()
check("relations 无禁建区", d["ok"] and d["relations"] == [] and d["feature_count"] == 0)
r = client.post("/api/relations", json={"parcel": rel_parcel, "forbidden": None})
d = r.json()
check("relations null 禁建区", d["ok"] and d["relations"] == [])

# 无效几何：与 /api/clip 同一套校验与定位
bad_forbidden = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": "bad-1", "geometry":
     {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]}},
]}
r = client.post("/api/relations", json={"parcel": rel_parcel, "forbidden": bad_forbidden})
d = r.json()
check("relations 无效报错", not d["ok"] and d["issues"][0]["code"] == "ZERO_AREA_RING"
      and d["issues"][0]["layer"] == "forbidden"
      and d["issues"][0]["feature_id"] == "bad-1", str(d)[:300])

# 结构错误 / 缺地块 / 多地块
r = client.post("/api/relations", json={"parcel": {"type": "Point", "coordinates": [1, 2]}})
d = r.json()
check("relations Point 结构错误", not d["ok"] and "结构错误" in d["error"])
r = client.post("/api/relations", json={})
check("relations 缺地块 422", r.status_code == 422)
two_parcels = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry":
     {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
    {"type": "Feature", "geometry":
     {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]}},
]}
r = client.post("/api/relations", json={"parcel": two_parcels})
d = r.json()
check("relations 多地块拒绝", not d["ok"] and "恰好包含一个" in d["error"], str(d))

# MultiPolygon 禁建区当作一个要素（一个索引）
mp_forbidden = {"type": "MultiPolygon", "coordinates": [
    [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]],
    [[[80, 80], [120, 80], [120, 120], [80, 120], [80, 80]]],
]}
r = client.post("/api/relations", json={"parcel": rel_parcel, "forbidden": mp_forbidden})
d = r.json()
check("relations MP 单要素", d["ok"] and d["feature_count"] == 1
      and d["relations"][0]["feature_index"] == 0
      and d["relations"][0]["relation"] == "overlap", str(d)[:300])

# ============================ /api/subdivide ============================

sub_parcel = {"type": "Polygon",
              "coordinates": [[[0, 0], [120, 0], [120, 80], [0, 80], [0, 0]]]}

# 基本分段：120×80，4 段，方向角 0 → 切线 x=30/60/90，每段 2400
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide ok", r.status_code == 200 and d["ok"], str(d)[:300])
check("subdivide 切线投影值", d["cuts"] == [30.0, 60.0, 90.0], str(d["cuts"]))
check("subdivide 目标面积", d["total_area"] == 9600.0 and d["target_area"] == 2400.0)
check("subdivide 容差", abs(d["area_tolerance"] - max(1e-6, 9600e-8)) < 1e-15)
check("subdivide 4 个要素", d["feature_count"] == 4
      and len(d["result"]["features"]) == 4)
check("subdivide 段号递增",
      [f["properties"]["segment"] for f in d["result"]["features"]] == [1, 2, 3, 4])
check("subdivide 各段面积",
      all(abs(s["area_m2"] - 2400.0) <= d["area_tolerance"] for s in d["segments"]),
      str([s["area_m2"] for s in d["segments"]]))
check("subdivide 实际与目标面积字段",
      all(set(s) >= {"segment", "area_m2", "target_area_m2", "area_error_m2",
                     "projection_range", "part_count"} for s in d["segments"]))
check("subdivide 复用裁切统计", d["areas"]["remaining_area"] == 9600.0)
# 分段 GeoJSON 合并覆盖原地块
from shapely.geometry import shape as _shape, mapping as _mapping
from shapely import union_all as _union_all
_seg_geoms = [_shape(f["geometry"]) for f in d["result"]["features"]]
_sym = _union_all(_seg_geoms).symmetric_difference(_shape(sub_parcel)).area
check("subdivide 合并覆盖原范围", _sym < 1e-9, f"symdiff={_sym}")
check("subdivide 两两交集为0", all(
    _seg_geoms[i].intersection(_seg_geoms[j]).area == 0.0
    for i in range(4) for j in range(i + 1, 4)))

# 方向角 90：水平切带，切线投影 = y = 20/40/60
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "count": 4, "angle_deg": 90})
d = r.json()
check("subdivide 90° 切线",
      d["ok"] and all(abs(c - e) < 1e-9 for c, e in zip(d["cuts"], [20, 40, 60])),
      str(d.get("cuts")))

# 带禁建区：先扣 400 的孔，可建 9200，4 段各 2300
sub_hole = {"type": "Polygon",
            "coordinates": [[[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]]]}
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "forbidden": sub_hole,
                      "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide 扣禁建区", d["ok"] and abs(d["total_area"] - 9200.0) < 1e-9)
check("subdivide 扣后各段 2300",
      all(abs(s["area_m2"] - 2300.0) <= d["area_tolerance"] for s in d["segments"]),
      str([s["area_m2"] for s in d["segments"]]))

# 空隙规则：两个分离正方形，目标恰为空隙上的累计面积 → 取较小投影端
two_sq = {"type": "MultiPolygon", "coordinates": [
    [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
]}
r = client.post("/api/subdivide", json={"parcel": two_sq, "count": 2, "angle_deg": 0})
d = r.json()
check("subdivide 空隙取较小投影端", d["ok"] and d["cuts"] == [10.0], str(d.get("cuts")))

# 非法参数：count / angle_deg
for bad_payload, note in [
    ({"parcel": sub_parcel, "count": 1, "angle_deg": 0}, "count=1"),
    ({"parcel": sub_parcel, "count": 13, "angle_deg": 0}, "count=13"),
    ({"parcel": sub_parcel, "count": 2.5, "angle_deg": 0}, "count=2.5"),
    ({"parcel": sub_parcel, "count": "3", "angle_deg": 0}, "count=字符串"),
    ({"parcel": sub_parcel, "count": True, "angle_deg": 0}, "count=布尔"),
    ({"parcel": sub_parcel, "count": 2, "angle_deg": -1}, "angle=-1"),
    ({"parcel": sub_parcel, "count": 2, "angle_deg": 180}, "angle=180"),
    ({"parcel": sub_parcel, "count": 2, "angle_deg": "x"}, "angle=字符串"),
]:
    r = client.post("/api/subdivide", json=bad_payload)
    d = r.json()
    check(f"subdivide 非法参数 {note}",
          not d["ok"] and "result" not in d, str(d)[:200])

# NaN / Infinity（Python json 默认接受非标准常量）
import json as _json
for raw, note in [
    ('{"parcel": %s, "count": 2, "angle_deg": NaN}' % _json.dumps(sub_parcel), "angle=NaN"),
    ('{"parcel": %s, "count": 2, "angle_deg": Infinity}' % _json.dumps(sub_parcel), "angle=Infinity"),
    ('{"parcel": %s, "count": NaN, "angle_deg": 0}' % _json.dumps(sub_parcel), "count=NaN"),
]:
    r = client.post("/api/subdivide", content=raw,
                    headers={"Content-Type": "application/json"})
    d = r.json()
    check(f"subdivide 非有限 {note}", not d["ok"] and "有限" in d["error"], str(d)[:200])

# 缺参数 → 422
r = client.post("/api/subdivide", json={"parcel": sub_parcel, "count": 2})
check("subdivide 缺 angle_deg 422", r.status_code == 422)
r = client.post("/api/subdivide", json={"parcel": sub_parcel, "angle_deg": 0})
check("subdivide 缺 count 422", r.status_code == 422)

# 整数值浮点 count 可接受
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "count": 3.0, "angle_deg": 0})
check("subdivide count=3.0 按 3 处理", r.json()["ok"] and r.json()["count"] == 3)

# 扣空 → 报错且不输出分段
cover_all = {"type": "Polygon",
             "coordinates": [[[-1, -1], [121, -1], [121, 81], [-1, 81], [-1, -1]]]}
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "forbidden": cover_all,
                      "count": 2, "angle_deg": 0})
d = r.json()
check("subdivide 扣空报错", not d["ok"] and "扣除" in d["error"], str(d)[:200])
check("subdivide 扣空无分段", "result" not in d and "segments" not in d)

# 无效几何 → 同一套校验与定位
r = client.post("/api/subdivide",
                json={"parcel": {"type": "Polygon",
                                 "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]},
                      "count": 2, "angle_deg": 0})
d = r.json()
check("subdivide 无效几何报错",
      not d["ok"] and d["issues"][0]["code"] == "ZERO_AREA_RING", str(d)[:200])

# 多地块拒绝
r = client.post("/api/subdivide",
                json={"parcel": two_parcels, "count": 2, "angle_deg": 0})
d = r.json()
check("subdivide 多地块拒绝", not d["ok"] and "恰好包含一个" in d["error"], str(d)[:200])

# 顶层类型错误
r = client.post("/api/subdivide",
                json={"parcel": {"type": "Point", "coordinates": [1, 2]},
                      "count": 2, "angle_deg": 0})
d = r.json()
check("subdivide Point 结构错误", not d["ok"] and "结构错误" in d["error"], str(d)[:200])

print()
if fails:
    print(f"{len(fails)} 个失败: {fails}")
    raise SystemExit(1)
print("全部通过 ✔")
