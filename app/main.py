"""FastAPI 后端：校验 GeoJSON、执行地块裁切、托管前端静态页面。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from .geometry_ops import (
    GeoJSONError,
    audit_relations,
    clip_parcel,
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


def _error_response(message: str, issues: list[dict] | None = None) -> dict:
    return {"ok": False, "error": message, "issues": issues or []}


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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
