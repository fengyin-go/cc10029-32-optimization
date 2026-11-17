"""爬虫引擎 时间线服务：生成作业时间序列、示例数据与采集入口统一决策。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.services.antiblock import ENTRY_CRAWLER, AntiblockService
from app.store import store

MODULE = "crawler"
REQUIRED_FIELDS = ["任务编号", "任务名称", "关联站点"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 采集入口复用反爬对抗的统一决策写法，不再自行维护一份命中判断
antiblock_service = AntiblockService()


class CrawlerService:
    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get(list(row.keys())[1] if len(row) > 1 else "name", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"爬虫任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于爬虫引擎可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"爬虫任务已{action}"

    def collect_decide(
        self,
        metrics: dict[str, Any],
        *,
        version: str | None = None,
        prefer: str = "latest",
        request_batch: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """采集入口的反爬判断：委托反爬对抗统一决策，返回绑定版本的决策快照。"""
        return antiblock_service.decide(
            metrics,
            source=ENTRY_CRAWLER,
            version=version,
            prefer=prefer,
            request_batch=request_batch,
        )
