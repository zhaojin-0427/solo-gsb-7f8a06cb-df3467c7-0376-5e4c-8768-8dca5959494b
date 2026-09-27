"""FastAPI 后端：校验 GeoJSON、执行地块裁切 / 等面积分段、托管前端静态页面。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from .geometry_ops import (
    SUBDIVIDE_MAX_COUNT,
    SUBDIVIDE_MIN_COUNT,
    GeoJSONError,
    SubdivideError,
    audit_relations,
    clip_parcel,
    subdivide_parcel,
    validate_layer_indexed,
)

# (要素索引, 要素 id, 几何) —— validate_layer_indexed 的记录结构
LayerRecord = tuple[int, Any, BaseGeometry]

app = FastAPI(title="本地地块裁切", version="1.1.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ClipRequest(BaseModel):
    # 地块：GeoJSON 对象（Feature / FeatureCollection / Polygon / MultiPolygon）
    parcel: dict[str, Any] = Field(..., description="地块 GeoJSON")
    # 禁建区：GeoJSON 对象，可为空要素集合
    forbidden: dict[str, Any] | None = Field(None, description="禁建区 GeoJSON")


class SubdivideRequest(BaseModel):
    # 地块与禁建区：与 /api/clip 相同
    parcel: dict[str, Any] = Field(..., description="地块 GeoJSON")
    forbidden: dict[str, Any] | None = Field(None, description="禁建区 GeoJSON")
    # 分段数量与方向角：手动校验以给出中文定位信息（故类型为 Any）
    count: Any = Field(..., description="分段数量（整数 2~12）")
    angle_deg: Any = Field(..., description="方向角（度，0 ≤ 值 < 180）")


def _error_response(message: str, issues: list[dict] | None = None) -> dict:
    return {"ok": False, "error": message, "issues": issues or []}


def _validate_payload(
    req: ClipRequest | SubdivideRequest,
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


def _validate_subdivide_params(req: SubdivideRequest) -> tuple[int, float, dict | None]:
    """校验分段数量与方向角，返回 (count, angle_deg, 错误响应)。"""
    count = req.count
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return 0, 0.0, _error_response(
            f"分段数量 count 必须是 {SUBDIVIDE_MIN_COUNT}~{SUBDIVIDE_MAX_COUNT} 的整数"
        )
    if isinstance(count, float):
        if not math.isfinite(count):
            return 0, 0.0, _error_response("分段数量 count 必须是有限的整数值")
        if not count.is_integer():
            return 0, 0.0, _error_response(
                f"分段数量 count 必须是 {SUBDIVIDE_MIN_COUNT}~{SUBDIVIDE_MAX_COUNT} 的整数"
            )
        count = int(count)
    if not (SUBDIVIDE_MIN_COUNT <= count <= SUBDIVIDE_MAX_COUNT):
        return 0, 0.0, _error_response(
            f"分段数量 count 超出范围：必须在 {SUBDIVIDE_MIN_COUNT}~{SUBDIVIDE_MAX_COUNT} 之间"
        )

    angle = req.angle_deg
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        return 0, 0.0, _error_response("方向角 angle_deg 必须是数字（度）")
    angle = float(angle)
    if not math.isfinite(angle):
        return 0, 0.0, _error_response("方向角 angle_deg 必须是有限数值")
    if not (0.0 <= angle < 180.0):
        return 0, 0.0, _error_response("方向角 angle_deg 必须满足 0 ≤ 值 < 180")

    return count, angle, None


@app.post("/api/subdivide")
def subdivide(req: SubdivideRequest) -> dict:
    """等面积分段：先求可建范围，再按方向角用垂直平行切线等分为 count 段。"""
    count, angle_deg, err = _validate_subdivide_params(req)
    if err is not None:
        return err

    parcel_records, forbidden_records, err = _validate_payload(req)
    if err is not None:
        return err

    if len(parcel_records) != 1:
        return _error_response("地块必须恰好包含一个 Polygon / MultiPolygon 要素")

    parcel_geoms = [geom for _, _, geom in parcel_records]
    forbidden_geoms = [geom for _, _, geom in forbidden_records]

    try:
        result = subdivide_parcel(parcel_geoms, forbidden_geoms, count, angle_deg)
    except SubdivideError as exc:
        return _error_response(str(exc))

    features = []
    for seg in result["segments"]:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "segment": seg["segment"],
                    "area_m2": seg["area"],
                    "target_area_m2": seg["target"],
                    "area_error_m2": seg["area"] - seg["target"],
                    "projection_range": seg["projection_range"],
                },
                "geometry": mapping(seg["geom"]),
            }
        )

    segments_meta = [
        {
            "segment": seg["segment"],
            "area_m2": seg["area"],
            "target_area_m2": seg["target"],
            "area_error_m2": seg["area"] - seg["target"],
            "projection_range": seg["projection_range"],
            "part_count": seg["part_count"],
        }
        for seg in result["segments"]
    ]

    return {
        "ok": True,
        "count": result["count"],
        "angle_deg": result["angle_deg"],
        "direction": result["direction"],
        "areas": result["areas"],
        "total_area": result["total_area"],
        "target_area": result["target_area"],
        "area_tolerance": result["area_tolerance"],
        "cuts": result["cuts"],
        "segments": segments_meta,
        "feature_count": len(features),
        "result": {"type": "FeatureCollection", "features": features},
        "issues": [],
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
