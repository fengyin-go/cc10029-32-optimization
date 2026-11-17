"""代理池接口：维护代理节点，覆盖提交校验、启用配置、停用配置等动作。

代理切换清单读取反爬对抗的决策数据快照生成，切换动作必须来自当前一致的策略版本。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.proxy import ProxyService

router = APIRouter(prefix="/api/proxy", tags=["代理池"])

service = ProxyService()

LIST_FIELDS = ["节点编号", "IP地址", "端口号", "协议类型", "匿程度", "响应延迟", "存活检测", "所属区域"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


class SwitchPayload(BaseModel):
    """代理切换清单入参：决策快照编号 + 可选代理节点。"""

    snapshot_id: str
    proxy_node_id: str | None = None


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
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、节点编号关键字与状态过滤代理池列表。"""
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
    output.write("\ufeff")
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
        headers={"Content-Disposition": 'attachment; filename="proxy.csv"'})


# ---------------- 代理切换清单（回写自反爬决策数据快照，GET 须在 /{entry_id} 之前） ----------------


@router.get("/switch-list", response_model=PageResult[dict])
def list_switch_list() -> PageResult[dict]:
    """代理池业务面查看代理切换清单，每条都可追溯到来源快照与策略版本。"""
    items = service.list_switches()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条代理节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"代理节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条代理节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="代理节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条代理节点执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ---------------- 代理切换清单写入 ----------------


@router.post("/switch-list", response_model=ActionResult)
def create_switch(payload: SwitchPayload) -> ActionResult:
    """读取决策快照生成切换清单；版本不一致或快照重复提交时由代理池拒收。"""
    switch, message = service.build_switch_from_snapshot(
        payload.snapshot_id, proxy_node_id=payload.proxy_node_id)
    if switch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=switch)
