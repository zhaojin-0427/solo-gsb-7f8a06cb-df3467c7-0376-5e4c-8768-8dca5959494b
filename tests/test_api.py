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
              "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}

def check_subdivision(d, count, total_area, angle=None):
    ok = d["ok"] and d["count"] == count and d["feature_count"] == count
    check(f"subdivide ok/count={count}", ok, str(d)[:300])
    if not ok:
        return
    check(f"subdivide 段数 FC {count}", len(d["result"]["features"]) == count)
    check(f"subdivide cuts {count-1}", len(d["cuts"]) == count - 1)
    check(f"subdivide segments 表 {count}", len(d["segments"]) == count)
    target = total_area / count
    check(f"subdivide target_area={target}",
          abs(d["target_area_m2"] - target) < 1e-6, str(d["target_area_m2"]))
    # 切线投影严格递增
    cuts = [c["projection"] for c in d["cuts"]]
    check("subdivide 切线递增", all(a < b for a, b in zip(cuts, cuts[1:])), str(cuts))
    check("subdivide 切线编号 1..", [c["index"] for c in d["cuts"]] == list(range(1, count)))
    # 每段实际/目标面积与编号/投影区间
    tol = max(1e-6, total_area * 1e-8)
    bounds = [d["projection"]["min"], *cuts, d["projection"]["max"]]
    for i, s in enumerate(d["segments"]):
        assert s["segment"] == i + 1
        assert abs(s["projection_low"] - bounds[i]) < 1e-9
        assert abs(s["projection_high"] - bounds[i + 1]) < 1e-9
        assert abs(s["actual_area_m2"] - target) <= tol, s
        assert abs(s["target_area_m2"] - target) <= 1e-6
    # Feature 属性齐全
    for i, f in enumerate(d["result"]["features"]):
        p = f["properties"]
        assert p["segment"] == i + 1 and abs(p["area_m2"] - target) <= tol, p
        assert f["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # 分段编号按投影递增
    assert [s["segment"] for s in d["segments"]] == list(range(1, count + 1))
    # 方向向量是单位向量
    ux, uy = d["direction"]["ux"], d["direction"]["uy"]
    assert abs(ux * ux + uy * uy - 1) < 1e-12
    if angle is not None:
        assert abs(d["angle_deg"] - angle) < 1e-9

# 基本分段（无禁建区，正方形 10000）
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "count": 4, "angle_deg": 0})
d = r.json()
check_subdivision(d, 4, 10000.0, angle=0)
check("subdivide 0度切线25/50/75",
      [c["projection"] for c in d["cuts"]] == [25.0, 50.0, 75.0], str(d["cuts"]))

# 90 度
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "count": 3, "angle_deg": 90})
d = r.json()
check_subdivision(d, 3, 10000.0, angle=90)

# 倾斜角 37 度 + count=12（上限）
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "count": 12, "angle_deg": 37})
d = r.json()
check_subdivision(d, 12, 10000.0, angle=37)

# 带禁建区：中心 20×20 孔洞 -> 可建 9600，先扣再分
hole = {"type": "Polygon",
        "coordinates": [[[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]]]}
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "forbidden": hole,
                      "count": 4, "angle_deg": 0})
d = r.json()
check_subdivision(d, 4, 9600.0)
check("subdivide 面积统计",
      abs(d["areas"]["buildable_area"] - 9600.0) < 1e-6
      and abs(d["areas"]["removed_area"] - 400.0) < 1e-6
      and abs(d["areas"]["parcel_area"] - 10000.0) < 1e-6, str(d["areas"]))
# 9600/4=2400 -> 切线 24 / 50（孔的台阶端）/ 76
check("subdivide 孔洞台阶切线24/50/76",
      [round(c["projection"], 6) for c in d["cuts"]] == [24.0, 50.0, 76.0],
      str(d["cuts"]))

# 投影空隙：两个分离方块，count=2，切线必须取空隙较小端 10
gap_parcel = {"type": "MultiPolygon", "coordinates": [
    [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
]}
r = client.post("/api/subdivide",
                json={"parcel": gap_parcel, "count": 2, "angle_deg": 0})
d = r.json()
check_subdivision(d, 2, 200.0)
check("subdivide 空隙取较小端10", d["cuts"][0]["projection"] == 10.0, str(d["cuts"]))

# 非法参数：count 越界 / 非整数 / 布尔 / 字符串
for bad_count in (1, 13, 0, 2.5, True, "3", None):
    r = client.post("/api/subdivide",
                    json={"parcel": sub_parcel, "count": bad_count, "angle_deg": 0})
    d = r.json()
    check(f"subdivide 非法 count={bad_count!r}",
          r.status_code == 422 and d["ok"] is False and "result" not in d,
          f"{r.status_code} {str(d)[:120]}")

# 非法角度：180 / 负数 / 布尔 / 字符串
for bad_angle in (180, -0.001, 180.0, True, "30", None):
    r = client.post("/api/subdivide",
                    json={"parcel": sub_parcel, "count": 4, "angle_deg": bad_angle})
    d = r.json()
    check(f"subdivide 非法 angle={bad_angle!r}",
          r.status_code == 422 and d["ok"] is False and "result" not in d,
          f"{r.status_code} {str(d)[:120]}")

# NaN / Infinity 不是合法 JSON 数字：以原始内容发送，必须被拒绝
for token in ("NaN", "Infinity", "-Infinity"):
    r = client.post(
        "/api/subdivide",
        content=('{"parcel": %s, "count": 4, "angle_deg": %s}'
                 % (json.dumps(sub_parcel), token)).encode(),
        headers={"Content-Type": "application/json"},
    )
    check(f"subdivide 非法 angle token={token}",
          r.status_code == 422 and r.json()["ok"] is False and "result" not in r.json(),
          f"{r.status_code} {r.text[:120]}")

# 边界合法角度 0 与 179.999
for good_angle in (0.0, 179.999):
    r = client.post("/api/subdivide",
                    json={"parcel": sub_parcel, "count": 2, "angle_deg": good_angle})
    d = r.json()
    check(f"subdivide 边界角度 {good_angle}", d["ok"] and d["count"] == 2, str(d)[:200])

# 缺字段
r = client.post("/api/subdivide", json={"parcel": sub_parcel, "count": 4})
check("subdivide 缺 angle 422", r.status_code == 422 and r.json()["ok"] is False)

# 无效几何：与 clip/relations 同一套定位，不输出分段
bad_geom = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": "p1", "geometry":
     {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2]]]}},
]}
r = client.post("/api/subdivide",
                json={"parcel": bad_geom, "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide 无效几何报错", r.status_code == 200 and not d["ok"]
      and len(d["issues"]) >= 1 and "result" not in d, str(d)[:200])

# 禁建区无效
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "forbidden": bad_geom,
                      "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide 禁建区无效报错",
      not d["ok"] and all(i["layer"] == "forbidden" for i in d["issues"])
      and "result" not in d, str(d)[:200])

# 完全扣空：报错且不输出分段
cover = {"type": "Polygon",
         "coordinates": [[[-10, -10], [110, -10], [110, 110], [-10, 110], [-10, -10]]]}
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "forbidden": cover,
                      "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide 扣空报错",
      r.status_code == 200 and not d["ok"] and "可建范围为空" in d["error"]
      and "result" not in d and "cuts" not in d, str(d)[:200])

# 多地块拒绝
r = client.post("/api/subdivide",
                json={"parcel": two_parcels, "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide 多地块拒绝", not d["ok"] and "恰好包含一个" in d["error"] and "result" not in d)

# 结构错误 GeoJSON
r = client.post("/api/subdivide",
                json={"parcel": {"type": "Point", "coordinates": [1, 2]},
                      "count": 4, "angle_deg": 0})
d = r.json()
check("subdivide 顶层 Point 结构错误", not d["ok"] and "结构错误" in d["error"])

# 分段结果几何复核：合并覆盖原范围、两两交集面积 0、孔洞保留
from shapely.geometry import shape as shshape
from shapely.ops import unary_union
r = client.post("/api/subdivide",
                json={"parcel": sub_parcel, "forbidden": hole,
                      "count": 5, "angle_deg": 23})
d = r.json()
buildable = shshape(sub_parcel).difference(shshape(hole))
pieces = [shshape(f["geometry"]) for f in d["result"]["features"]]
u = unary_union(pieces)
check("subdivide HTTP 覆盖", abs(buildable.symmetric_difference(u).area) < 1e-7)
worst = max(pieces[i].intersection(pieces[j]).area
            for i in range(len(pieces)) for j in range(i + 1, len(pieces)))
check("subdivide HTTP 两两零面积交集", worst < 1e-7, str(worst))
check("subdivide HTTP 总面积", abs(sum(p.area for p in pieces) - 9600.0) < 1e-6)

print()
if fails:
    print(f"{len(fails)} 个失败: {fails}")
    raise SystemExit(1)
print("全部通过 ✔")
