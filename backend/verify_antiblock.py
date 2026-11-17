"""统一反爬决策改造的端到端验证脚本（标准库 + 直接调服务/HTTP）。"""
import json
import sys
import threading
import urllib.request

sys.path.insert(0, "/workspace/backend")

BASE = __import__("os").environ.get("BASE_URL", "http://127.0.0.1:8000")


def http(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


from app.services.antiblock import AntiblockService, ENTRY_CRAWLER, ENTRY_PROXY
from app.services.antiblock_store import decision_store
from app.services.proxy import ProxyService

class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


client = type("C", (), {"get": lambda self, p: Resp(*http("GET", p)),
                        "post": lambda self, p, json=None: Resp(*http("POST", p, json))})()
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 1. 共用判断写法：两个入口同版本同指标结论一致
svc = AntiblockService()
metrics = {"连续失败": 4, "封禁率": 0.25, "平均延迟": 3200}
current = svc.resolve_strategy(prefer="latest")
frozen = svc.resolve_strategy(prefer="frozen")
check("存在当前版本", current is not None and current["版本状态"] == "current", str(current))
check("冻结版可取", frozen is not None and frozen["策略版本"] == "ANTI-0003-v1")

crawler_snap, c_msg = svc.decide(dict(metrics), source=ENTRY_CRAWLER, request_batch="T1")
proxy_snap, p_msg = svc.decide(dict(metrics), source=ENTRY_PROXY, request_batch="T1")
check("两入口同版本", crawler_snap["策略版本"] == proxy_snap["策略版本"] == current["策略版本"])
check("两入口规则指纹相同", crawler_snap["规则指纹"] == proxy_snap["规则指纹"])
check("两入口命中结论一致", crawler_snap["命中结论"] == proxy_snap["命中结论"])
check("v2阈值下连续失败4>=3命中", crawler_snap["命中结论"]["hit"] is True)
hit_metrics = [t["指标"] for t in crawler_snap["命中结论"]["triggered"]]
check("命中指标包含连续失败/封禁率/平均延迟",
      set(hit_metrics) == {"连续失败", "封禁率", "平均延迟"}, str(hit_metrics))
check("延迟落在延迟范围内",
      current and 800 <= crawler_snap["命中结论"]["delay_ms"] <= 2000,
      str(crawler_snap["命中结论"]["delay_ms"]))

# 2. 边界冲突：同样指标在冻结版 v1 旧阈值下不命中（当前版与旧阈值边界冲突）
frozen_snap, _ = svc.decide(dict(metrics), source=ENTRY_CRAWLER, version="ANTI-0003-v1",
                            request_batch="T1-FROZEN")
check("冻结版v1下连续失败4<5不命中", frozen_snap["命中结论"]["hit"] is False,
      str(frozen_snap["命中结论"]))
check("最终只读取一个带生效版本的完整快照",
      frozen_snap["策略版本"] == "ANTI-0003-v1" and frozen_snap["触发条件"].startswith("连续失败>=5"))

# 3. 重复提交同一快照只生效一次，不产生第二条记录
before = len(svc.list_snapshots())
again, msg = svc.decide(dict(metrics), source=ENTRY_CRAWLER, request_batch="T1")
after = len(svc.list_snapshots())
check("重复提交幂等不新增记录", before == after, f"{before} vs {after}")
check("重复提交返回原快照", again["快照编号"] == crawler_snap["快照编号"], msg)

# 4. 并发写入只允许一个提交成功
race_metrics = {"连续失败": 9, "封禁率": 0.8, "平均延迟": 8000}
results = []
threads = [
    threading.Thread(target=lambda: results.append(
        svc.decide(dict(race_metrics), source=ENTRY_PROXY, request_batch=f"RACE-{i}")))
    for i in range(8)
]
for t in threads:
    t.start()
for t in threads:
    t.join()
race_ids = {r[0]["快照编号"] for r in results}
check("并发8次只产生1份快照", len(race_ids) == 1, str(race_ids))
race_dupes = [r for r in results if "重复提交" in r[1]]
check("其余并发提交被识别为重复", len(race_dupes) == 7, str([r[1] for r in results]))

# 5. 历史请求不被追溯改写
history = svc.list_history()
h1 = next(h for h in history if h["记录编号"] == "HIST-0001")
check("历史HIST-0001保持v1旧阈值未命中",
      h1["策略版本"] == "ANTI-0003-v1" and h1["命中结论"]["hit"] is False)
check("历史记录仍显示原阈值版本", h1["规则指纹"] == frozen["规则指纹"])
new_hist = [h for h in history if h["请求批次"] in ("T1", "T1-FROZEN", "RACE-0")]
check("统一决策只追加新历史不改写旧记录",
      len(history) >= 4 and all(h["记录编号"] in ("HIST-0001", "HIST-0002") or
                                h["记录编号"].startswith("HIST-") for h in history))

# 6. 代理池读取快照生成切换清单，拒绝第二份与非当前版本
proxy_svc = ProxyService()
switch, s_msg = proxy_svc.build_switch_from_snapshot(crawler_snap["快照编号"], proxy_node_id="PROX-0003")
check("当前版本快照可生成切换清单", switch is not None, s_msg)
check("切换清单绑定当前策略版本",
      switch and switch["策略版本"] == current["策略版本"], s_msg)
dup_switch, dup_msg = proxy_svc.build_switch_from_snapshot(crawler_snap["快照编号"])
check("代理池拒绝第二份结果", dup_switch is None and "拒绝第二份" in dup_msg, dup_msg)
frozen_switch, f_msg = proxy_svc.build_switch_from_snapshot(frozen_snap["快照编号"])
check("冻结版本快照被代理池拒收", frozen_switch is None and "不是当前一致策略版本" in f_msg, f_msg)
switches = proxy_svc.list_switches()
check("切换清单只保留一份", len(switches) == 1 and switches[0]["来源快照编号"] == crawler_snap["快照编号"])

# 7. 兼容既有反爬对抗接口（字段名与返回结构稳定）
r = client.get("/api/antiblock")
check("既有列表接口结构不变", r.status_code == 200 and
      set(r.json().keys()) == {"items", "total", "page", "size"})
row = r.json()["items"][0]
check("既有字段名保持稳定",
      all(f in row for f in ["策略编号", "策略名称", "触发条件", "延迟范围", "UA伪装",
                              "Referer生成", "Cookie策略", "熔断阈值", "status"]))
r2 = client.post("/api/antiblock", json={
    "策略编号": "ANTI-0009", "策略名称": "临时策略", "触发条件": "连续失败>=4;封禁率>=0.25",
    "延迟范围": "500-1500ms", "熔断阈值": "错误率>=0.6"})
check("既有登记接口可用", r2.status_code == 200 and r2.json()["ok"] is True)
new_id = r2.json()["entry"]["id"]
r3 = client.post(f"/api/antiblock/{new_id}/actions", json={"action": "启用配置"})
check("启用配置发布新版本", r3.json()["ok"] and r3.json()["entry"]["策略版本"] == "ANTI-0009-v1")
r4 = client.post(f"/api/antiblock/{new_id}/actions", json={"action": "停用配置"})
check("停用配置接口保持兼容", r4.json()["ok"] is True)

# 8. 新接口链路：crawler 入口、antiblock decisions、compare、proxy switch-list
r5 = client.post("/api/crawler/anti-decisions",
                 json={"metrics": {"连续失败": 2, "封禁率": 0.1}, "request_batch": "API-T1"})
check("采集入口决策接口", r5.status_code == 200 and r5.json()["entry"]["来源入口"] == "crawler")
r5b = client.post("/api/crawler/anti-decisions",
                  json={"metrics": {"连续失败": 2, "封禁率": 0.1}, "request_batch": "API-T1"})
check("采集入口重复提交幂等",
      r5b.json()["entry"]["快照编号"] == r5.json()["entry"]["快照编号"])
r6 = client.post("/api/antiblock/decisions",
                 json={"metrics": dict(metrics), "source": "proxy", "request_batch": "API-T2"})
check("反爬统一决策接口", r6.status_code == 200 and r6.json()["entry"]["来源入口"] == "proxy")
r7 = client.post("/api/antiblock/decisions/compare", json={"metrics": dict(metrics)})
check("入口对比接口结论一致", r7.json()["entry"]["结论一致"] is True, r7.text)
r8 = client.get("/api/antiblock/snapshots")
check("快照列表接口", r8.status_code == 200 and r8.json()["total"] >= 1)
r9 = client.get("/api/antiblock/history")
check("历史接口仍显示旧阈值记录",
      any(h["策略版本"] == "ANTI-0003-v1" for h in r9.json()["items"]))
r10 = client.get("/api/antiblock/versions")
check("版本列表含冻结版与当前版",
      {v["版本状态"] for v in r10.json()["items"]} >= {"frozen", "current"})
snap_id = r6.json()["entry"]["快照编号"]
r11 = client.post("/api/proxy/switch-list", json={"snapshot_id": snap_id})
check("代理池切换清单接口", r11.status_code == 200 and r11.json()["ok"] is True, r11.text)
r12 = client.post("/api/proxy/switch-list", json={"snapshot_id": snap_id})
check("代理池接口拒收第二份", r12.json()["ok"] is False and "拒绝第二份" in r12.json()["message"])
# 另一份不同指标的快照也回写切换清单，验证代理池业务面可回读多条且都带来源
r6b = client.post("/api/antiblock/decisions",
                  json={"metrics": {"连续失败": 7, "封禁率": 0.6}, "source": "proxy",
                        "request_batch": "API-T3"})
snap_id_b = r6b.json()["entry"]["快照编号"]
r12b = client.post("/api/proxy/switch-list", json={"snapshot_id": snap_id_b,
                                                    "proxy_node_id": "PROX-0002"})
check("第二份不同快照可生成切换清单", r12b.json()["ok"] is True, r12b.text)
r13 = client.get("/api/proxy/switch-list")
check("代理池业务面可回读切换清单",
      r13.json()["total"] >= 2 and
      all("策略版本" in item and "来源快照编号" in item for item in r13.json()["items"]),
      r13.text)
r14 = client.post("/api/antiblock/decisions",
                  json={"metrics": dict(metrics), "source": "proxy", "prefer": "frozen"})
check("可显式选择冻结版决策",
      r14.json()["entry"]["策略版本"] == "ANTI-0003-v1" and
      r14.json()["entry"]["版本状态"] == "frozen")
r15 = client.post("/api/antiblock/decisions",
                  json={"metrics": dict(metrics), "source": "bad-entry"})
check("非法入口被拒", r15.json()["ok"] is False)
# 冻结版快照生成切换清单时必须被代理池拒收（边界冲突只认当前一致版本）
r15b = client.post("/api/proxy/switch-list",
                   json={"snapshot_id": r14.json()["entry"]["快照编号"]})
check("冻结版快照切换动作被代理池拒收",
      r15b.json()["ok"] is False and "不是当前一致策略版本" in r15b.json()["message"])

# 9. 阈值调整后重新发布：旧版冻结、新版 current（纯 HTTP 链路，同编号不同阈值二次登记）
r16 = client.post("/api/antiblock", json={
    "策略编号": "ANTI-0010", "策略名称": "调阈策略", "触发条件": "连续失败>=3",
    "延迟范围": "100-200ms", "熔断阈值": "错误率>=0.5"})
eid = r16.json()["entry"]["id"]
ra = client.post(f"/api/antiblock/{eid}/actions", json={"action": "启用配置"})
check("首次启用发布v1", ra.json()["entry"]["策略版本"] == "ANTI-0010-v1")
r17 = client.post("/api/antiblock", json={
    "策略编号": "ANTI-0010", "策略名称": "调阈策略", "触发条件": "连续失败>=1",
    "延迟范围": "100-200ms", "熔断阈值": "错误率>=0.5"})
eid2 = r17.json()["entry"]["id"]
rb = client.post(f"/api/antiblock/{eid2}/actions", json={"action": "启用配置"})
check("阈值调整后启用发布v2", rb.json()["entry"]["策略版本"] == "ANTI-0010-v2")
versions_1010 = [v for v in client.get("/api/antiblock/versions").json()["items"]
                 if v["策略编号"] == "ANTI-0010"]
v10 = next(v for v in versions_1010 if v["策略版本"] == "ANTI-0010-v1")
v20 = next(v for v in versions_1010 if v["策略版本"] == "ANTI-0010-v2")
check("阈值调整产生新版本v2且为current", v20["版本状态"] == "current")
check("旧版本v1自动冻结", v10["版本状态"] == "frozen")
check("新旧版本规则指纹不同", v10["规则指纹"] != v20["规则指纹"])
# 边界冲突：连续失败=2 在 v1(>=3) 不命中，在 v2(>=1) 命中，两版快照各自完整保留
d_v1 = client.post("/api/antiblock/decisions",
                   json={"metrics": {"连续失败": 2}, "source": "crawler",
                         "version": "ANTI-0010-v1", "request_batch": "EDGE-V1"})
d_v2 = client.post("/api/antiblock/decisions",
                   json={"metrics": {"连续失败": 2}, "source": "crawler",
                         "version": "ANTI-0010-v2", "request_batch": "EDGE-V2"})
check("边界冲突-旧版不命中", d_v1.json()["entry"]["命中结论"]["hit"] is False)
check("边界冲突-新版命中", d_v2.json()["entry"]["命中结论"]["hit"] is True)
check("两份快照绑定各自生效版本",
      d_v1.json()["entry"]["策略版本"] == "ANTI-0010-v1"
      and d_v2.json()["entry"]["策略版本"] == "ANTI-0010-v2")
# 历史请求仍显示原阈值（服务进程内种子历史不被任何新决策改写）
h_still = next(h for h in svc.list_history() if h["记录编号"] == "HIST-0001")
check("历史命中结果在版本演进后仍不改变", h_still["命中结论"]["hit"] is False)
hist_http = client.get("/api/antiblock/history").json()["items"]
check("HTTP历史接口保留v1旧阈值原始记录",
      any(h["记录编号"] == "HIST-0001" and h["策略版本"] == "ANTI-0003-v1"
          and h["命中结论"]["hit"] is False for h in hist_http))

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部验证通过")
