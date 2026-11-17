"""反爬命中规则引擎：采集入口与代理切换入口共用的同一份判断写法。

历史背景：反爬策略判断过去在采集入口和代理切换入口各写了一遍，阈值调整后两个入口
可能给出不同结论。这里把命中条件、延迟范围、熔断阈值的解析与判定收敛成纯函数，
两个入口都只能调用本模块，不再各自维护一份判断。

纯函数不读取任何全局配置：调用方必须把"某个策略版本的完整规则"作为 strategy 传入，
从而保证两个入口拿到同一版本时结论必然一致。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# 触发条件里可识别的指标阈值，写法示例：
#   "连续失败>=3;HTTP403>=2;封禁率>=0.2;平均延迟>=3000"
_CONDITION_PATTERN = re.compile(
    r"(连续失败|HTTP\s*403|HTTP\s*429|封禁率|平均延迟)\s*(>=|>|<=|<|=)\s*([0-9]+(?:\.[0-9]+)?)"
)
# 延迟范围，写法示例："800-2000ms"
_DELAY_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*[-~]\s*([0-9]+(?:\.[0-9]+)?)\s*m?s?", re.IGNORECASE)
# 熔断阈值，写法示例："错误率>=0.5" 或 "连续失败>=5"
_CIRCUIT_PATTERN = re.compile(
    r"(连续失败|错误率|封禁率|平均延迟)\s*(>=|>|<=|<|=)\s*([0-9]+(?:\.[0-9]+)?)"
)

# 触发指标 -> 请求指标入参里允许出现的键名（兼容中英文口径）
_METRIC_KEYS = {
    "连续失败": ("连续失败", "consecutive_failures", "failures"),
    "HTTP403": ("HTTP403", "http403", "403"),
    "HTTP429": ("HTTP429", "http429", "429"),
    "封禁率": ("封禁率", "block_rate", "ban_rate"),
    "平均延迟": ("平均延迟", "平均延迟ms", "avg_latency", "latency"),
}
# 熔断指标 -> 请求指标入参键名
_CIRCUIT_KEYS = {
    "连续失败": ("连续失败", "consecutive_failures", "failures"),
    "错误率": ("错误率", "error_rate"),
    "封禁率": ("封禁率", "block_rate", "ban_rate"),
    "平均延迟": ("平均延迟", "平均延迟ms", "avg_latency", "latency"),
}

# 解析失败时的兜底口径（仅在策略文本缺省时使用，不改变既有命中条件的语义）
DEFAULT_CONDITION: dict[str, tuple[str, float]] = {
    "连续失败": (">=", 3.0),
    "封禁率": (">=", 0.2),
    "平均延迟": (">=", 3000.0),
}
DEFAULT_DELAY_RANGE = (800.0, 2000.0)
DEFAULT_CIRCUIT: dict[str, tuple[str, float]] = {"错误率": (">=", 0.5)}


def _normalize_metric(name: str) -> str:
    return name.replace(" ", "").upper() if name.upper().startswith("HTTP") else name.replace(" ", "")


def parse_condition(text: Any) -> dict[str, tuple[str, float]]:
    """解析触发条件文本为 {指标: (运算符, 阈值)}。"""
    parsed: dict[str, tuple[str, float]] = {}
    for name, operator, value in _CONDITION_PATTERN.findall(str(text or "")):
        parsed[_normalize_metric(name)] = (operator, float(value))
    return parsed


def parse_delay_range(text: Any) -> tuple[float, float]:
    """解析延迟范围文本，返回 (下界ms, 上界ms)。"""
    match = _DELAY_PATTERN.search(str(text or ""))
    if not match:
        return DEFAULT_DELAY_RANGE
    low, high = float(match.group(1)), float(match.group(2))
    if low > high:
        low, high = high, low
    return low, high


def parse_circuit(text: Any) -> dict[str, tuple[str, float]]:
    """解析熔断阈值文本为 {指标: (运算符, 阈值)}。"""
    parsed: dict[str, tuple[str, float]] = {}
    for name, operator, value in _CIRCUIT_PATTERN.findall(str(text or "")):
        parsed[_normalize_metric(name)] = (operator, float(value))
    return parsed


def _read_metric(metrics: dict[str, Any], aliases: tuple[str, ...]) -> float | None:
    for key in aliases:
        if key in metrics and metrics[key] not in (None, ""):
            try:
                return float(metrics[key])
            except (TypeError, ValueError):
                continue
    return None


def _compare(actual: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return actual >= threshold
    if operator == ">":
        return actual > threshold
    if operator == "<=":
        return actual <= threshold
    if operator == "<":
        return actual < threshold
    return actual == threshold


def evaluate(strategy: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    """对一次请求指标执行反爬判定（采集入口、代理切换入口共用此写法）。

    返回结构固定，字段名保持稳定，供快照与两个入口直接透传：
      hit              是否命中任一触发条件
      triggered        逐条命中的指标及当时取值/阈值
      delay_ms         按延迟范围确定性选取的建议延迟（同输入必同输出）
      circuit_open     是否触发熔断
      strategy_version 判定所依据的策略版本
    """
    version = str(strategy.get("策略版本", strategy.get("version", "")))
    conditions = parse_condition(strategy.get("触发条件")) or dict(DEFAULT_CONDITION)
    delay_low, delay_high = parse_delay_range(strategy.get("延迟范围"))
    circuit = parse_circuit(strategy.get("熔断阈值")) or dict(DEFAULT_CIRCUIT)

    triggered: list[dict[str, Any]] = []
    for metric_name, (operator, threshold) in conditions.items():
        aliases = _METRIC_KEYS.get(metric_name, (metric_name,))
        actual = _read_metric(metrics, aliases)
        if actual is None:
            continue
        if _compare(actual, operator, threshold):
            triggered.append({
                "指标": metric_name,
                "运算符": operator,
                "阈值": threshold,
                "实际值": actual,
            })

    circuit_hits: list[dict[str, Any]] = []
    for metric_name, (operator, threshold) in circuit.items():
        aliases = _CIRCUIT_KEYS.get(metric_name, (metric_name,))
        actual = _read_metric(metrics, aliases)
        if actual is None:
            continue
        if _compare(actual, operator, threshold):
            circuit_hits.append({
                "指标": metric_name,
                "运算符": operator,
                "阈值": threshold,
                "实际值": actual,
            })

    # 确定性选延迟：以请求指标的稳定哈希落在 [low, high]，保证两个入口同输入同结论
    digest = hashlib.sha256(
        (version + "|" + str(sorted(metrics.items(), key=lambda item: str(item[0])))).encode("utf-8")
    ).hexdigest()
    span = delay_high - delay_low
    delay_ms = round(delay_low + (int(digest[:8], 16) % 10000) / 10000 * span, 2)

    return {
        "hit": bool(triggered),
        "triggered": triggered,
        "delay_ms": delay_ms,
        "delay_range_ms": [delay_low, delay_high],
        "circuit_open": bool(circuit_hits),
        "circuit_triggered": circuit_hits,
        "strategy_version": version,
    }


def rule_fingerprint(strategy: dict[str, Any]) -> str:
    """对策略版本里参与判定的规则文本计算指纹，触发条件或阈值一变指纹即变。"""
    basis = {
        "策略编号": strategy.get("策略编号"),
        "触发条件": str(strategy.get("触发条件", "")),
        "延迟范围": str(strategy.get("延迟范围", "")),
        "熔断阈值": str(strategy.get("熔断阈值", "")),
        "UA伪装": str(strategy.get("UA伪装", "")),
        "Referer生成": str(strategy.get("Referer生成", "")),
        "Cookie策略": str(strategy.get("Cookie策略", "")),
    }
    return hashlib.sha256(repr(sorted(basis.items())).encode("utf-8")).hexdigest()[:16]


def metrics_fingerprint(metrics: dict[str, Any]) -> str:
    """对触发判定的请求指标口径计算指纹，作为快照幂等键的一部分。"""
    basis = {str(key): metrics[key] for key in sorted(metrics, key=str)}
    return hashlib.sha256(repr(basis).encode("utf-8")).hexdigest()[:16]
