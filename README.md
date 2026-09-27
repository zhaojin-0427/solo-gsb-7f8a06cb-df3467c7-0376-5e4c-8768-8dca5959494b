# 本地地块裁切工具（Local Parcel Clipper）

一个纯本地运行的网页工具：粘贴**地块**与若干**禁建区**的 GeoJSON
（`Polygon` / `MultiPolygon`），先做严格几何校验与错误定位，再把全部禁建区
**合并（并集）后从地块中扣除**，预览输入与结果、显示剩余面积，并下载结果
GeoJSON。同时提供**禁建区关系审计**：逐要素判定禁建区相对地块的空间关系
（相离 / 共点共边 / 部分重叠 / 完全位于地块内 / 包含地块 / 完全一致），以及
**等面积分段**：先扣禁建区得到可建范围，再按指定方向用平行切线反求切点，
把可建范围分成面积相等的 `count` 段。

- 后端：Python 3.11 + FastAPI + Shapely 2（GEOS）
- 前端：原生 HTML / CSS / JavaScript（Canvas 渲染，**零第三方依赖**）
- 坐标按**平面米制**解释，面积单位为 **平方米（m²）**（同时显示公顷）
- 全程本地运行，数据不离开本机

## 功能与规则

### 裁切语义

1. 所有禁建区先做**并集合并**再差集 → **重叠禁建区只扣一次**。
2. Shapely 差集只扣正面积 → 禁建区与地块**共点、共边（含边完全重合）不扣面积**。
3. 结果保留全部**独立面**与全部**孔洞**（`Polygon` 的洞用 `evenodd` 渲染）。
4. 地块被完全扣空时返回空结果：
   `{"type":"FeatureCollection","features":[]}`，剩余面积为 0。
5. 禁建区可以超出地块范围，只有落在地块内的部分产生扣减。
6. 可只输入地块（无禁建区），此时原样返回。

### 输入校验（精确定位）

以下问题都会被定位到「图层 → 要素序号/id → 第几个面 → 外环/孔洞环（环序）→ 点序号/坐标」，
一次返回全部问题，点击页面上的问题列表可在地图上高亮定位：

- 环未闭合（首点 ≠ 尾点）：`RING_NOT_CLOSED`
- 环自交（边中相交、非相邻边共点、共边重叠等）：`RING_SELF_INTERSECTION`，
  使用 STRtree 空间索引逐对求交并给出交点坐标；同时附带 GEOS 的有效性结论
  （孔洞位于外环之外、孔洞嵌套、多面成员重叠等）：`INVALID_POLYGON`
- 零面积环（全部共线 / 重合，或净面积为零的蝴蝶结等）：`ZERO_AREA_RING`
- 点数不足、坐标非有限数字、几何类型不是 Polygon/MultiPolygon 等结构问题

### 面积统计

接口与页面显示：地块面积、禁建区**并集**面积、**实际扣除**面积、剩余面积（m² / 公顷）。

### 禁建区关系审计（`POST /api/relations`）

沿用地块与禁建区的同一套 GeoJSON 校验。对**每个禁建区要素**（以
FeatureCollection 原始下标作为稳定索引，无效要素跳过、不影响其余要素）相对
唯一地块判定以下六类关系之一：

| 关系 | 含义 |
| --- | --- |
| `disjoint` | 完全相离，无任何公共点 |
| `touch` | 仅共点 / 共边接触（边完全重合也算），交集面积为零 |
| `overlap` | 存在**正面积**交集，且互不为覆盖（零面积交集不会被判为 overlap） |
| `within` | 禁建区完全位于地块内部（允许与地块边界重合） |
| `contains` | 禁建区完全包住地块（允许与地块边界重合） |
| `equals` | 两者几何完全相等 |

每条结果还返回：

- `intersection_area_m2`：两者交集面积（平方米）
- `boundary_distance_m`：两者**边界**之间的最短距离（米）——相离时即净距，
  共点共边 / 部分重叠时为 0，内含时是禁建区边界到地块边界的最近缝隙
- `feature_index` / `feature_id`：稳定的禁建区要素索引与可选 id

结果按 `feature_index` 升序排列。页面「关系审计」列出全部结果并按关系着色，
点击任一条目即在地图上高亮对应禁建区（其余要素变暗、自动缩放定位）。

### 等面积分段（`POST /api/subdivide`）

请求体在 `/api/clip` 的 `parcel` / `forbidden` 之外，另收两个参数：

- `count`：**整数**，分段数量，取值 **2～12**（不接受布尔、浮点、字符串）。
- `angle_deg`：有限数字，分段方向角（度），**0 ≤ 值 < 180**，方向为从
  **+x 轴逆时针**转过该角度的单位向量 `u`。

算法与保证：

1. 先计算**可建范围**（地块扣除禁建区并集，语义与裁切完全一致）。
2. 切割线与 `u` 垂直（方程 `u·p = t`），几何在 `u` 上的投影记为 `s = u·p`。
   按累计面积 `F(t) = area(可建范围 ∩ {u·p ≤ t})` 对每个目标
   `k × 总面积 / count` **二分反求**切线投影 `t`——不是等宽切带，也不做
   栅格近似。
3. 投影方向上若存在**空隙**（如两块分离的面之间没有几何），`F(t)` 会出现
   水平台阶；当目标累计面积恰落在台阶上时，切线取**空隙的较小投影端**。
4. 段 `k = 可建范围 ∩ {tₖ₋₁ < u·p ≤ tₖ}`，按投影递增从 1 编号；每段保留
   实际的**独立面与孔洞**（一段可含多个 Polygon，输出为 `MultiPolygon`）。
5. 每段面积与 `总面积 / count` 的差不超过
   `max(1e-6 平方米, 总面积 × 1e-8)`；各段合并后恰好覆盖原可建范围，
   两两交集面积为 0（仅允许共享切割线，零面积）。
6. 可建范围被完全扣空、参数非法或输入几何无效时一律报错，**不输出分段**。

页面在「等面积分段」区填写**段数**与**角度**后点击「等面积分段」：各段按
12 色调色盘着色，橙色虚线为反求出的平行切线，列表逐段给出实际 / 目标面积
与差值，点击任一段可缩放定位；可「下载分段 GeoJSON」。**修改数量、角度或
任一输入文本都会立即清除旧分段**，避免结果与输入不一致。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI：/api/clip、/api/relations、/api/subdivide、/api/health、静态页面托管
│   └── geometry_ops.py    # GeoJSON 解析、错误定位、并集差集裁切、关系审计
├── static/
│   ├── index.html         # 页面结构
│   ├── style.css          # 样式
│   └── app.js             # 输入解析、Canvas 预览/结果渲染、下载（无依赖）
├── tests/
│   ├── test_geometry.py   # 裁切语义与错误定位单元测试
│   └── test_api.py        # HTTP 端到端测试
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## 一键启动（推荐，Docker Compose）

```bash
docker compose up --build
```

启动后浏览器访问：

> **http://localhost:8080**

停止：`Ctrl+C`（后台运行则 `docker compose down`）。

端口映射在 `docker-compose.yml` 中配置（宿主机 `8080` → 容器 `8080`）；
若本机 8080 被占用，改成 `"8081:8080"` 即可。

## 仅用 Docker（无 Compose）

```bash
docker build -t parcel-clipper .
docker run --rm -p 8080:8080 parcel-clipper
# 访问 http://localhost:8080
```

## 本地开发（不用 Docker）

需 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

访问 http://localhost:8080 。

运行测试：

```bash
python -m tests.test_geometry
python -m tests.test_api
```

## 使用说明

1. 在「地块」框粘贴一个 GeoJSON（`Feature` / `FeatureCollection` /
   `Polygon` / `MultiPolygon` 均可），地块必须恰好包含一个面要素。
2. 在「禁建区」框粘贴任意数量的面要素（可留空）。可点击「载入示例」快速体验。
3. 点击「预览输入」仅画图检查；点击「校验并裁切」执行校验与裁切；
   点击「关系审计」逐要素输出禁建区相对地块的六类关系。
4. 若有错误，按问题列表逐项修正（点击可定位到地图上的红色标记）。
5. 裁切成功后查看剩余面积与结果图层（绿色），点击「下载结果 GeoJSON」保存；
   扣空时返回空要素集合且不提供下载。
6. 关系审计后点击关系列表条目可高亮对应禁建区，再点「校验并裁切」可随时回到裁切。
7. **等面积分段**：在左侧「等面积分段」区填写段数（2～12 整数）与方向角
   （0 ≤ 值 < 180，自 +x 轴逆时针），点击「等面积分段」。成功后各段按不同
   颜色绘制，橙色虚线为反求出的平行切线；下方列表逐段显示实际 / 目标面积
   与偏差，点击某段自动缩放定位，「下载分段 GeoJSON」保存全部段。
   修改段数、角度或地块 / 禁建区输入都会立即清除旧分段。
8. 地图支持滚轮缩放、拖拽平移、图层开关与「适配视图」。

## API

### `POST /api/clip`

请求：

```json
{
  "parcel": { "type": "Polygon", "coordinates": [...] },
  "forbidden": { "type": "FeatureCollection", "features": [ ... ] }
}
```

成功响应（扣空时 `features` 为空数组、`feature_count` 为 0）：

```json
{
  "ok": true,
  "areas": {
    "parcel_area": 10000.0,
    "forbidden_union_area": 400.0,
    "removed_area": 400.0,
    "remaining_area": 9600.0
  },
  "feature_count": 1,
  "result": { "type": "FeatureCollection", "features": [ ... ] },
  "issues": []
}
```

校验失败响应（HTTP 200，`ok=false`，`issues` 含全部定位信息）：

```json
{
  "ok": false,
  "error": "输入几何无效，请按下方定位修正后重试",
  "issues": [
    {
      "layer": "parcel",
      "feature_index": 0,
      "feature_id": "lot-7",
      "polygon_index": 0,
      "ring": "outer",
      "ring_index": 0,
      "point_index": 3,
      "coordinate": [0.0, 10.0],
      "code": "RING_NOT_CLOSED",
      "message": "第 0 个外环未闭合：首点 [0, 0] 与尾点 [0, 10] 不一致"
    }
  ]
}
```

另有 `GET /api/health` 健康检查。

### `POST /api/relations`

请求体与 `/api/clip` 完全相同（`parcel` 必填、`forbidden` 可空），校验规则也
完全一致（无效输入返回同样的 `ok=false` + `issues` 定位结构）。成功响应：

```json
{
  "ok": true,
  "feature_count": 3,
  "relations": [
    {
      "feature_index": 0,
      "feature_id": "zone-a",
      "relation": "overlap",
      "intersection_area_m2": 400.0,
      "boundary_distance_m": 0.0
    },
    {
      "feature_index": 1,
      "feature_id": null,
      "relation": "within",
      "intersection_area_m2": 900.0,
      "boundary_distance_m": 10.0
    },
    {
      "feature_index": 2,
      "feature_id": null,
      "relation": "disjoint",
      "intersection_area_m2": 0.0,
      "boundary_distance_m": 42.426407
    }
  ],
  "issues": []
}
```

`relation` 取值固定为
`disjoint` / `touch` / `overlap` / `within` / `contains` / `equals`，
列表按 `feature_index` 升序。`forbidden` 为空时返回空数组。

### `POST /api/subdivide`

请求（`parcel` / `forbidden` 与 `/api/clip` 完全相同，另加 `count`、
`angle_deg`）：

```json
{
  "parcel": { "type": "Polygon", "coordinates": [...] },
  "forbidden": { "type": "FeatureCollection", "features": [ ... ] },
  "count": 4,
  "angle_deg": 0
}
```

成功响应：

```json
{
  "ok": true,
  "count": 4,
  "angle_deg": 0,
  "direction": { "ux": 1.0, "uy": 0.0 },
  "areas": {
    "parcel_area": 10000.0,
    "forbidden_union_area": 400.0,
    "removed_area": 400.0,
    "buildable_area": 9600.0
  },
  "target_area_m2": 2400.0,
  "area_tolerance_m2": 0.000096,
  "projection": { "min": 0.0, "max": 100.0 },
  "cuts": [
    { "index": 1, "projection": 24.0 },
    { "index": 2, "projection": 50.0 },
    { "index": 3, "projection": 76.0 }
  ],
  "segments": [
    {
      "segment": 1,
      "projection_low": 0.0,
      "projection_high": 24.0,
      "actual_area_m2": 2400.0,
      "target_area_m2": 2400.0,
      "area_error_m2": 0.0
    }
  ],
  "feature_count": 4,
  "result": { "type": "FeatureCollection", "features": [ ... ] },
  "issues": []
}
```

字段说明：

- `cuts`：`count - 1` 条内部切线，`projection` 即切线 `u·p = t` 中的 `t`
  （投影值，单位米），按投影递增给出；`projection.min/max` 是可建范围在
  `u` 上的投影端点，分别作为第 1 段下界与第 `count` 段上界。
- `direction`：由 `angle_deg` 得到的单位方向向量。
- `segments`：逐段给出投影区间、**实际面积** `actual_area_m2` 与
  **目标面积** `target_area_m2` 及二者之差 `area_error_m2`。
- `result`：分段 GeoJSON，每个 Feature 对应一段（`properties.segment` 为
  段号，含实际 / 目标面积），几何在该段包含多个独立面时为 `MultiPolygon`，
  孔洞照常保留。

错误响应：

- 参数非法（`count` 非 2～12 的整数、`angle_deg` 越界 / 非有限数 / 类型错误、
  缺字段等）：HTTP **422**，`ok=false`，无 `result` / `cuts`。
- 几何无效：与其它接口相同的 `ok=false` + `issues` 定位结构（HTTP 200）。
- 可建范围为空（地块被禁建区完全扣除）：`ok=false`，错误信息提示无法分段，
  不返回任何分段。

## 说明与限制

- 不做坐标参考系转换：请确保输入为同一平面米制坐标系（例如投影坐标），
  直接粘贴经纬度会导致面积数值没有平方米意义。
- Z 坐标会被忽略，仅按前两维（x, y）处理。
- 工具不"自动修复"自交面（不调用 `make_valid`），避免在用户不知情时改变地块语义；
  请按定位修正输入后重新裁切。
