"""代理池业务规则：状态流转、字段校验与代理切换清单都收在这里。

代理切换清单的数据来源是反爬对抗生成的「反爬决策数据快照」：
- 切换动作必须来自当前一致的策略版本，版本不匹配直接拒收；
- 同一快照只允许写入一次，代理池拒绝第二份结果；
- 代理池自身不再实现命中判断，统一结论以反爬对抗的快照为准。
"""
from __future__ import annotations

from typing import Any

from app.services.antiblock_store import decision_store, now_text
from app.store import store

MODULE = "proxy"
LIST_FIELDS = ["节点编号", "IP地址", "端口号", "协议类型", "匿程度", "响应延迟", "存活检测", "所属区域"]
REQUIRED_FIELDS = ["节点编号", "IP地址", "端口号"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ProxyService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("节点编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"代理节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于代理池可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"代理节点已{action}"

    # ---------------- 代理切换清单（回写自反爬决策快照） ----------------

    def build_switch_from_snapshot(
        self, snapshot_id: str, *, proxy_node_id: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """读取一份反爬决策数据快照，生成代理切换清单条目。

        校验顺序：快照存在 -> 版本当前一致 -> 快照未重复写入。任一不满足都拒收。
        """
        snapshot = decision_store.find_snapshot(snapshot_id)
        if snapshot is None:
            return None, f"反爬决策快照 {snapshot_id} 不存在，无法生成代理切换清单"

        version = decision_store.get_version(str(snapshot["策略版本"]))
        if version is None or version.get("版本状态") != "current":
            return None, (
                f"快照绑定版本 {snapshot.get('策略版本')} 不是当前一致策略版本，"
                "代理池拒绝该切换动作"
            )

        if decision_store.proxy_switch_exists(snapshot_id):
            return None, f"快照 {snapshot_id} 的切换清单已存在，代理池拒绝第二份结果"

        conclusion = snapshot.get("命中结论", {})
        switch = {
            "切换编号": f"SWITCH-{snapshot_id}",
            "来源快照编号": snapshot_id,
            "策略编号": snapshot.get("策略编号"),
            "策略版本": snapshot.get("策略版本"),
            "规则指纹": snapshot.get("规则指纹"),
            "代理节点": proxy_node_id or "AUTO-PICK",
            "触发条件": snapshot.get("触发条件"),
            "命中": bool(conclusion.get("hit")),
            "建议延迟ms": conclusion.get("delay_ms"),
            "熔断开启": bool(conclusion.get("circuit_open")),
            "来源入口": snapshot.get("来源入口"),
            "写入时间": now_text(),
        }
        saved, created = decision_store.append_proxy_switch(switch)
        if not created:
            return None, f"快照 {snapshot_id} 并发重复提交，代理池只保留第一份结果"
        return saved, "代理切换清单已按统一决策快照生成"

    def list_switches(self) -> list[dict[str, Any]]:
        """代理池业务面读取代理切换清单（结果由反爬决策快照回写）。"""
        return decision_store.list_proxy_switches()
