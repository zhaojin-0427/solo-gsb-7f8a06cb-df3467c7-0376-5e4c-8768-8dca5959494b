"""FastAPI 后端：校验 GeoJSON、执行地块裁切、托管前端静态页面。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import MultiPolygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry_ops import (
    GeoJSONError,
    audit_relations,
    clip_parcel,
    subdivide_buildable,
    validate_layer_indexed,
)

# (要素索引, 要素 id, 几何) —— validate_layer_indexed 的记录结构
LayerRecord = tuple[int, Any, BaseGeometry]

app = FastAPI(title="本地地块裁切", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ClipRequest(BaseModel):
    # 地块：GeoJSON 对象（Feature / FeatureCollection / Polygon / MultiPolygon）
    parcel: dict[str, Any] = Field(..., description="地块 GeoJSON")
    # 禁建区：GeoJSON 对象，可为空要素集合
    forbidden: dict[str, Any] | None = Field(None, description="禁建区 GeoJSON")


class SubdivideRequest(ClipRequest):
    # 分段数量：2～12 的整数（不接受布尔、浮点或字符串）
    count: int = Field(..., description="分段数量（2～12 的整数）")
    # 分段方向角：相对 +x 轴逆时针的角度，0 ≤ 值 < 180，必须是有限数字
    angle_deg: float = Field(..., description="分段方向角（度，0 ≤ 值 < 180）")

    @field_validator("count", mode="before")
    @classmethod
    def _check_count(cls, v: Any) -> int:
        # before 模式检查原始入参：拒绝字符串数字、浮点与布尔（Pydantic 默认会
        # 把它们强转成 int），只接受真正的 int。
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("count 必须是 2～12 的整数")
        if not 2 <= v <= 12:
            raise ValueError("count 必须在 2～12 之间")
        return v

    @field_validator("angle_deg", mode="before")
    @classmethod
    def _check_angle(cls, v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("angle_deg 必须是数字（度）")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("angle_deg 必须是有限数字")
        if not 0.0 <= f < 180.0:
            raise ValueError("angle_deg 必须满足 0 ≤ 值 < 180")
        return f


def _error_response(message: str, issues: list[dict] | None = None) -> dict:
    return {"ok": False, "error": message, "issues": issues or []}


@app.exception_handler(RequestValidationError)
async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """参数 / 请求体校验失败：统一成 ok=false，不输出任何分段结果。"""
    locs = [".".join(str(p) for p in e.get("loc", []) if p != "body") for e in exc.errors()]
    msgs = "；".join(
        f"{loc + '：' if loc else ''}{e.get('msg', '参数不合法')}"
        for loc, e in zip(locs, exc.errors())
    )
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": f"非法参数：{msgs or '请求参数不合法'}", "issues": []},
    )


def _validate_payload(
    req: ClipRequest,
) -> tuple[list[LayerRecord], list[LayerRecord], dict | None]:
    """校验请求中的两个图层，返回 (地块记录, 禁建区记录, 错误响应)。

    错误响应为 None 时表示通过几何有效性检查（要素数量是否恰好一个由调用方判断）。
    """
    try:
        parcel_records, parcel_issues = validate_layer_indexed(req.parcel, "parcel")
        if req.forbidden is not None:
            forbidden_records, forbidden_issues = validate_layer_indexed(
                req.forbidden, "forbidden"
            )
        else:
            forbidden_records, forbidden_issues = [], []
    except GeoJSONError as exc:
        return [], [], _error_response(f"GeoJSON 结构错误：{exc}")

    issues = parcel_issues + forbidden_issues
    if issues:
        return (
            [],
            [],
            _error_response("输入几何无效，请按下方定位修正后重试", issues),
        )
    return parcel_records, forbidden_records, None


@app.post("/api/clip")
def clip(req: ClipRequest) -> dict:
    parcel_records, forbidden_records, err = _validate_payload(req)
    if err is not None:
        return err

    if len(parcel_records) != 1:
        return _error_response("地块必须恰好包含一个 Polygon / MultiPolygon 要素")

    parcel_geoms = [geom for _, _, geom in parcel_records]
    forbidden_geoms = [geom for _, _, geom in forbidden_records]

    result = clip_parcel(parcel_geoms, forbidden_geoms)
    parts = result["parts"]

    features = [
        {
            "type": "Feature",
            "properties": {"part": i + 1, "area_m2": round(p.area, 6)},
            "geometry": mapping(p),
        }
        for i, p in enumerate(parts)
    ]
    result_geojson: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": features,
    }

    return {
        "ok": True,
        "areas": result["areas"],
        "feature_count": len(features),
        "result": result_geojson,
        "issues": [],
    }


@app.post("/api/relations")
def relations(req: ClipRequest) -> dict:
    """禁建区关系审计：逐要素判定相对地块的六类空间关系。"""
    parcel_records, forbidden_records, err = _validate_payload(req)
    if err is not None:
        return err

    if len(parcel_records) != 1:
        return _error_response("地块必须恰好包含一个 Polygon / MultiPolygon 要素")

    rows = audit_relations(parcel_records, forbidden_records)

    return {
        "ok": True,
        "relations": rows,
        "feature_count": len(rows),
        "issues": [],
    }


@app.post("/api/subdivide")
def subdivide(req: SubdivideRequest) -> dict:
    """等面积分段：先扣禁建区得到可建范围，再按方向 angle_deg 反求 count 条
    平行切线，把可建范围按投影递增分成 count 个等面积段。"""
    parcel_records, forbidden_records, err = _validate_payload(req)
    if err is not None:
        return err

    if len(parcel_records) != 1:
        return _error_response("地块必须恰好包含一个 Polygon / MultiPolygon 要素")

    parcel_geoms = [geom for _, _, geom in parcel_records]
    forbidden_geoms = [geom for _, _, geom in forbidden_records]

    clip = clip_parcel(parcel_geoms, forbidden_geoms)
    if not clip["parts"] or clip["areas"]["remaining_area"] <= 0.0:
        return _error_response("可建范围为空（地块被禁建区完全扣除），无法分段")

    buildable = clip["parts"][0] if len(clip["parts"]) == 1 else unary_union(clip["parts"])

    try:
        sub = subdivide_buildable(buildable, req.count, req.angle_deg)
    except GeoJSONError as exc:
        return _error_response(f"分段失败：{exc}")

    features: list[dict[str, Any]] = []
    for i, polys in enumerate(sub["segments"]):
        geometry: dict[str, Any]
        if len(polys) == 1:
            geometry = mapping(polys[0])
        else:
            geometry = mapping(MultiPolygon(polys))
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "segment": i + 1,
                    "area_m2": round(sub["actual_areas"][i], 6),
                    "target_area_m2": round(sub["target_area"], 6),
                    "area_error_m2": round(
                        sub["actual_areas"][i] - sub["target_area"], 9
                    ),
                },
                "geometry": geometry,
            }
        )

    result_geojson: dict[str, Any] = {"type": "FeatureCollection", "features": features}

    boundaries = [sub["projection_min"], *sub["cuts"], sub["projection_max"]]
    segment_rows = []
    for i, actual in enumerate(sub["actual_areas"]):
        segment_rows.append(
            {
                "segment": i + 1,
                "projection_low": boundaries[i],
                "projection_high": boundaries[i + 1],
                "actual_area_m2": round(actual, 6),
                "target_area_m2": round(sub["target_area"], 6),
                "area_error_m2": round(actual - sub["target_area"], 9),
            }
        )

    return {
        "ok": True,
        "count": sub["count"],
        "angle_deg": req.angle_deg,
        "direction": {"ux": sub["direction"][0], "uy": sub["direction"][1]},
        "areas": {
            "parcel_area": clip["areas"]["parcel_area"],
            "forbidden_union_area": clip["areas"]["forbidden_union_area"],
            "removed_area": clip["areas"]["removed_area"],
            "buildable_area": sub["buildable_area"],
        },
        "target_area_m2": sub["target_area"],
        "area_tolerance_m2": sub["area_tolerance"],
        "projection": {
            "min": sub["projection_min"],
            "max": sub["projection_max"],
        },
        "cuts": [
            {"index": i + 1, "projection": t}
            for i, t in enumerate(sub["cuts"])
        ],
        "segments": segment_rows,
        "feature_count": len(features),
        "result": result_geojson,
        "issues": [],
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
