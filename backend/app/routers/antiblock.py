"""反爬对抗接口：维护反爬策略，覆盖提交校验、启用配置、停用配置等动作。

统一决策相关接口（版本/快照/历史/入口对比）也挂在本路由下，保持既有接口字段名与
返回结构稳定，仅新增路径，不改动既有契约。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.antiblock import ENTRY_PROXY, VALID_ENTRIES, AntiblockService

router = APIRouter(prefix="/api/antiblock", tags=["反爬对抗"])

service = AntiblockService()

LIST_FIELDS = ["策略编号", "策略名称", "触发条件", "延迟范围", "UA伪装", "Referer生成", "Cookie策略", "熔断阈值"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


class DecisionPayload(BaseModel):
    """统一决策入参：请求指标 + 版本口径（冻结版/最新版）。"""

    metrics: dict[str, Any]
    source: str = ENTRY_PROXY
    version: str | None = None
    prefer: str = "latest"
    request_batch: str | None = None


class ComparePayload(BaseModel):
    metrics: dict[str, Any]
    version: str | None = None
    prefer: str = "latest"


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、策略编号关键字与状态过滤反爬对抗列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="antiblock.csv"'})


# -------- 统一反爬决策的 GET 路径：必须声明在 /{entry_id} 之前，否则会被当成条目编号 --------


@router.get("/versions", response_model=PageResult[dict])
def list_versions(
    strategy_code: str | None = Query(default=None, description="按策略编号过滤版本"),
) -> PageResult[dict]:
    """列出策略版本（含冻结版与当前版），展示阈值调整后的版本演变。"""
    items = service.list_versions(strategy_code)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/snapshots", response_model=PageResult[dict])
def list_snapshots(
    version: str | None = Query(default=None, description="按策略版本过滤快照"),
) -> PageResult[dict]:
    """列出反爬决策数据快照，每份快照都绑定策略版本与触发条件指纹。"""
    items = service.list_snapshots(version=version)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: str) -> dict:
    """沿反爬对抗调用链定位单份决策快照的数据来源。"""
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"决策快照 {snapshot_id} 不存在")
    return snapshot


@router.get("/history", response_model=PageResult[dict])
def list_history() -> PageResult[dict]:
    """历史请求记录：按当时策略版本与旧阈值解释，统一决策不追溯改写。"""
    items = service.list_history()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条反爬策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"反爬策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条反爬策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="反爬策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条反爬策略执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ---------------- 统一反爬决策：决策写入与入口对比 ----------------


@router.post("/decisions", response_model=ActionResult)
def create_decision(payload: DecisionPayload) -> ActionResult:
    """读取反爬策略并统一决策，生成（或幂等复用）反爬决策数据快照。"""
    if payload.source not in VALID_ENTRIES:
        return ActionResult(ok=False, message=f"来源入口只支持 {VALID_ENTRIES}")
    if payload.prefer not in ("latest", "frozen"):
        return ActionResult(ok=False, message="prefer 只支持 latest（最新版）或 frozen（冻结版）")
    snapshot, message = service.decide(
        payload.metrics,
        source=payload.source,
        version=payload.version,
        prefer=payload.prefer,
        request_batch=payload.request_batch,
    )
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.post("/decisions/compare", response_model=ActionResult)
def compare_decisions(payload: ComparePayload) -> ActionResult:
    """同版本下对比采集入口与代理入口的命中结论，并可核对历史请求的原阈值。"""
    result = service.compare_entries(payload.metrics, version=payload.version, prefer=payload.prefer)
    if "error" in result:
        return ActionResult(ok=False, message=result["error"])
    return ActionResult(ok=True, message="两个入口使用同一策略版本，命中结论一致", entry=result)
