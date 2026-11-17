"""反爬对抗业务规则：状态流转、字段校验与统一反爬决策都收在这里。

统一决策改造要点：
- 命中条件/延迟范围/熔断阈值的判定只有 antiblock_rules.evaluate 一份写法，
  采集入口（crawler）与代理切换入口（proxy）都经 decide() 调用，不再各写一遍；
- decide() 生成绑定「策略版本 + 触发条件指纹」的决策数据快照，两个入口读到的
  必然是同一版本的完整快照；
- 快照并发写入由 decision_store 的锁保证只成功一个，重复提交幂等；
- 历史请求按当时版本固化，decide() 只追加新记录，不追溯改写旧命中结果。
"""
from __future__ import annotations

from typing import Any

from app.services import antiblock_rules
from app.services.antiblock_store import decision_store, now_text
from app.store import store

MODULE = "antiblock"
LIST_FIELDS = ["策略编号", "策略名称", "触发条件", "延迟范围", "UA伪装", "Referer生成", "Cookie策略", "熔断阈值"]
REQUIRED_FIELDS = ["策略编号", "策略名称", "触发条件"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 两个统一入口的标识，写入快照的「来源入口」字段
ENTRY_CRAWLER = "crawler"
ENTRY_PROXY = "proxy"
VALID_ENTRIES = (ENTRY_CRAWLER, ENTRY_PROXY)


class AntiblockService:
    # ---------------- 既有 CRUD（字段名与返回结构保持稳定） ----------------

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
            rows = [row for row in rows if keyword.casefold() in str(row.get("策略编号", "")).casefold()]
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
            return None, f"反爬策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于反爬对抗可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 启用配置即发布策略版本：阈值/做法改变会产生新版本，旧版本自动冻结
        if action == "启用配置":
            published = decision_store.publish_version(entry, change_note=f"动作 {action} 触发发布")
            entry["策略版本"] = published["策略版本"]
        return entry, f"反爬策略已{action}"

    # ---------------- 策略版本（冻结版 / 最新版） ----------------

    def list_versions(self, strategy_code: str | None = None) -> list[dict[str, Any]]:
        return decision_store.list_versions(strategy_code)

    def resolve_strategy(
        self, *, version: str | None = None, prefer: str = "latest"
    ) -> dict[str, Any] | None:
        """两个入口读取策略的唯一口径：显式版本优先，否则在冻结版/最新版间取舍。"""
        return decision_store.resolve_version(version, prefer=prefer)

    # ---------------- 统一反爬决策（两个入口共用） ----------------

    def decide(
        self,
        metrics: dict[str, Any],
        *,
        source: str,
        version: str | None = None,
        prefer: str = "latest",
        request_batch: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """读取反爬策略并统一决策，生成（或复用）反爬决策数据快照。

        采集入口与代理切换入口都只能走这里：同版本 + 同触发指标必然得到同一份快照，
        阈值调整后不会再出现两个入口结论不一致。
        """
        if source not in VALID_ENTRIES:
            return None, f"来源入口「{source}」不在统一决策允许范围 {VALID_ENTRIES}"
        strategy = decision_store.resolve_version(version, prefer=prefer)
        if strategy is None:
            return None, "没有可用的反爬策略版本，无法统一决策"

        strategy_version = str(strategy["策略版本"])
        rule_fp = str(strategy["规则指纹"])
        metric_fp = antiblock_rules.metrics_fingerprint(metrics)

        # 幂等：同一 (版本, 规则指纹, 指标指纹, 入口) 的快照只生效一次
        duplicate = decision_store.find_duplicate(strategy_version, rule_fp, metric_fp, source)
        if duplicate is not None:
            return duplicate, "命中已存在的统一决策快照，重复提交只生效一次"

        # 共用的唯一判断写法
        judgment = antiblock_rules.evaluate(strategy, metrics)
        snapshot_id = f"SNAP-{strategy_version}-{rule_fp[:6]}-{metric_fp[:6]}-{source}"
        snapshot = {
            "快照编号": snapshot_id,
            "策略编号": strategy["策略编号"],
            "策略版本": strategy_version,
            "规则指纹": rule_fp,
            "指标指纹": metric_fp,
            "触发条件": strategy["触发条件"],
            "延迟范围": strategy["延迟范围"],
            "熔断阈值": strategy["熔断阈值"],
            "请求指标": dict(metrics),
            "命中结论": {
                "hit": judgment["hit"],
                "triggered": judgment["triggered"],
                "delay_ms": judgment["delay_ms"],
                "delay_range_ms": judgment["delay_range_ms"],
                "circuit_open": judgment["circuit_open"],
                "circuit_triggered": judgment["circuit_triggered"],
            },
            "来源入口": source,
            "请求批次": request_batch or "",
            "版本状态": strategy["版本状态"],
            "创建时间": now_text(),
        }
        saved, created = decision_store.save_snapshot(snapshot)

        # 历史请求按当前版本追加固化；统一决策不修改任何既有历史命中结果
        decision_store.append_history({
            "记录编号": f"HIST-{len(decision_store.list_history()) + 1:04d}",
            "请求批次": snapshot["请求批次"] or snapshot_id,
            "来源入口": source,
            "策略编号": strategy["策略编号"],
            "策略版本": strategy_version,
            "规则指纹": rule_fp,
            "请求指标": dict(metrics),
            "命中结论": saved["命中结论"],
            "说明": "统一决策快照生成时追加，按该版本阈值解释",
        })
        return saved, ("统一决策快照已生成" if created else "复用已存在的统一决策快照")

    def list_snapshots(self, *, version: str | None = None) -> list[dict[str, Any]]:
        return decision_store.list_snapshots(version=version)

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        return decision_store.find_snapshot(snapshot_id)

    def list_history(self) -> list[dict[str, Any]]:
        return decision_store.list_history()

    def compare_entries(
        self,
        metrics: dict[str, Any],
        *,
        version: str | None = None,
        prefer: str = "latest",
    ) -> dict[str, Any]:
        """同一份策略版本下分别以两个入口视角决策，供使用人员核对命中结论一致。"""
        strategy = decision_store.resolve_version(version, prefer=prefer)
        if strategy is None:
            return {"error": "没有可用的反爬策略版本"}
        crawler_snap, _ = self.decide(
            metrics, source=ENTRY_CRAWLER, version=strategy["策略版本"],
            request_batch="COMPARE-CRAWLER")
        proxy_snap, _ = self.decide(
            metrics, source=ENTRY_PROXY, version=strategy["策略版本"],
            request_batch="COMPARE-PROXY")
        return {
            "策略版本": strategy["策略版本"],
            "规则指纹": strategy["规则指纹"],
            "请求指标": dict(metrics),
            "采集入口结论": crawler_snap["命中结论"] if crawler_snap else None,
            "代理入口结论": proxy_snap["命中结论"] if proxy_snap else None,
            "结论一致": bool(
                crawler_snap and proxy_snap
                and crawler_snap["命中结论"] == proxy_snap["命中结论"]
            ),
        }
