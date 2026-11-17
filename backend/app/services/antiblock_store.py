"""反爬决策数据仓库：策略版本、决策快照、历史请求记录与代理切换清单的唯一存放处。

设计约束（对应统一决策改造）：
- 决策快照必须绑定同一策略版本和触发条件，两个入口不能各自读取不同配置；
- 并发写入快照时只允许一个提交成功，重复提交同一快照只生效一次；
- 历史请求记录按当时策略版本固化，统一决策不追溯改写已完成的命中结果；
- 代理池只接收当前一致策略版本的快照，拒绝第二份结果。

仓库只负责存取与并发控制，判定规则全部在 antiblock_rules 纯函数里，便于两个入口复用。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.seed import ANTIBLOCK_VERSIONS, ANTIBLOCK_HISTORY


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now() -> str:
    return now_text()


class AntiblockDecisionStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 策略版本：version -> 完整策略快照（含规则文本与状态）
        self._versions: dict[str, dict[str, Any]] = {
            item["策略版本"]: dict(item) for item in ANTIBLOCK_VERSIONS
        }
        # 决策快照：snapshot_id -> 快照记录
        self._snapshots: dict[str, dict[str, Any]] = {}
        # 幂等索引：(策略版本, 规则指纹, 指标指纹, 入口) -> snapshot_id
        self._snapshot_index: dict[tuple[str, str, str, str], str] = {}
        # 历史请求记录（按当时版本固化，不再改写）
        self._history: list[dict[str, Any]] = [dict(item) for item in ANTIBLOCK_HISTORY]
        # 代理切换清单（代理池业务面）
        self._proxy_switches: list[dict[str, Any]] = []
        # 已经写入代理切换清单的快照，拒绝第二份结果
        self._proxy_snapshot_ids: set[str] = set()

    # ---- 策略版本 -------------------------------------------------------

    def list_versions(self, strategy_code: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(item) for item in self._versions.values()]
            if strategy_code:
                items = [item for item in items if item.get("策略编号") == strategy_code]
            return sorted(items, key=lambda item: str(item.get("策略版本")))

    def get_version(self, version: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._versions.get(version)
            return dict(item) if item else None

    def current_version(self, strategy_code: str) -> dict[str, Any] | None:
        """读取某策略当前生效版本（status=current）；不存在则返回 None。"""
        with self._lock:
            for item in self._versions.values():
                if item.get("策略编号") == strategy_code and item.get("版本状态") == "current":
                    return dict(item)
            return None

    def latest_current(self) -> dict[str, Any] | None:
        """读取全局最新的当前生效版本（两个入口统一使用的默认版本来源）。"""
        with self._lock:
            currents = [
                item for item in self._versions.values() if item.get("版本状态") == "current"
            ]
            if not currents:
                return None
            return dict(sorted(currents, key=lambda item: str(item["策略版本"]))[-1])

    def publish_version(self, strategy: dict[str, Any], *, change_note: str = "") -> dict[str, Any]:
        """发布新版本：同编号旧 current 版本冻结，新版本成为唯一 current。

        规则指纹未变化时不产生新版本，直接返回当前版本，避免无意义分叉。
        """
        from app.services.antiblock_rules import rule_fingerprint

        with self._lock:
            code = str(strategy["策略编号"])
            fingerprint = rule_fingerprint(strategy)
            # 直接定位存储中的当前版本记录（不能用 current_version 的拷贝，改拷贝不会落库）
            stored_current = next(
                (item for item in self._versions.values()
                 if item.get("策略编号") == code and item.get("版本状态") == "current"),
                None,
            )
            if stored_current and stored_current.get("规则指纹") == fingerprint:
                return dict(stored_current)

            if stored_current:
                stored_current["版本状态"] = "frozen"
            next_seq = 1
            for item in self._versions.values():
                if item.get("策略编号") == code:
                    # 版本号形如 "<策略编号>-v<序号>"，取末尾 v 之后的数字
                    tail = str(item["策略版本"]).rsplit("-v", 1)
                    if len(tail) == 2 and tail[1].isdigit():
                        next_seq = max(next_seq, int(tail[1]) + 1)
            version = f"{code}-v{next_seq}"
            record = {
                "策略版本": version,
                "策略编号": code,
                "策略名称": strategy.get("策略名称", ""),
                "触发条件": strategy.get("触发条件", ""),
                "延迟范围": strategy.get("延迟范围", ""),
                "UA伪装": strategy.get("UA伪装", ""),
                "Referer生成": strategy.get("Referer生成", ""),
                "Cookie策略": strategy.get("Cookie策略", ""),
                "熔断阈值": strategy.get("熔断阈值", ""),
                "规则指纹": fingerprint,
                "版本状态": "current",
                "变更说明": change_note or "启用配置发布",
                "发布时间": _now(),
            }
            self._versions[version] = record
            return dict(record)

    def resolve_version(self, version: str | None, *, prefer: str = "latest") -> dict[str, Any] | None:
        """在冻结版或最新版之间取舍。

        - 显式给 version：只返回该版本（可能是 frozen），找不到返回 None；
        - prefer=latest：返回当前生效版本；
        - prefer=frozen：优先返回最近冻结的版本，没有冻结版时回退当前版本。
        """
        with self._lock:
            if version:
                return self.get_version(version)
            if prefer == "frozen":
                frozen = sorted(
                    (item for item in self._versions.values() if item.get("版本状态") == "frozen"),
                    key=lambda item: str(item.get("发布时间", "")),
                )
                if frozen:
                    return dict(frozen[-1])
            return self.latest_current()

    # ---- 决策快照 -------------------------------------------------------

    def find_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._snapshots.get(snapshot_id)
            return dict(item) if item else None

    def find_duplicate(
        self, version: str, rule_fp: str, metric_fp: str, source: str
    ) -> dict[str, Any] | None:
        with self._lock:
            snapshot_id = self._snapshot_index.get((version, rule_fp, metric_fp, source))
            return self.find_snapshot(snapshot_id) if snapshot_id else None

    def save_snapshot(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """幂等并发写入。

        返回 (快照, created)：相同 (版本, 规则指纹, 指标指纹, 入口) 的重复提交直接返回
        已存在快照且 created=False，不会产生第二条记录；并发下只有第一个提交落库。
        """
        key = (
            str(snapshot["策略版本"]),
            str(snapshot["规则指纹"]),
            str(snapshot["指标指纹"]),
            str(snapshot["来源入口"]),
        )
        with self._lock:
            existing_id = self._snapshot_index.get(key)
            if existing_id:
                return dict(self._snapshots[existing_id]), False
            self._snapshots[snapshot["快照编号"]] = dict(snapshot)
            self._snapshot_index[key] = snapshot["快照编号"]
            return dict(snapshot), True

    def list_snapshots(self, *, version: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(item) for item in self._snapshots.values()]
            if version:
                items = [item for item in items if item.get("策略版本") == version]
            return sorted(items, key=lambda item: str(item.get("创建时间")))

    # ---- 历史请求（只读追加，不追溯改写） ---------------------------------

    def append_history(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = dict(record)
            entry.setdefault("记录时间", _now())
            self._history.append(entry)
            return dict(entry)

    def list_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._history]

    # ---- 代理切换清单 ----------------------------------------------------

    def proxy_switch_exists(self, snapshot_id: str) -> bool:
        with self._lock:
            return snapshot_id in self._proxy_snapshot_ids

    def append_proxy_switch(self, switch: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """代理池写入切换清单。

        同一快照只生效一次；代理池拒绝第二份结果（created=False 表示拒收）。
        """
        snapshot_id = str(switch["来源快照编号"])
        with self._lock:
            if snapshot_id in self._proxy_snapshot_ids:
                return {}, False
            entry = dict(switch)
            entry.setdefault("写入时间", _now())
            self._proxy_switches.append(entry)
            self._proxy_snapshot_ids.add(snapshot_id)
            return dict(entry), True

    def list_proxy_switches(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._proxy_switches]


decision_store = AntiblockDecisionStore()
