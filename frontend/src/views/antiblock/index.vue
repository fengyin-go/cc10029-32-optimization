<template>
  <section class="page" data-module="antiblock">
    <header class="page-head">
      <div>
        <h2>反爬对抗管理</h2>
        <p class="page-desc">配置反爬对抗策略，在触发封禁前自动切换 UA/Referer/延迟。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记反爬策略</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出反爬对抗清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无反爬对抗数据，可先登记反爬策略</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记反爬策略</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="decision-panel" data-testid="decision-panel">
      <h3>统一反爬决策（采集入口与代理切换入口读取同一策略版本）</h3>
      <p class="page-desc">
        命中条件与延迟范围只有反爬对抗一份判断写法；输入同一批请求指标，对比两个入口的命中结论。
      </p>
      <form class="filter-bar" @submit.prevent="runCompare">
        <label class="filter-item">
          <span>连续失败</span>
          <input v-model.number="metricForm.连续失败" data-testid="metric-failures" type="number" min="0" />
        </label>
        <label class="filter-item">
          <span>封禁率</span>
          <input v-model.number="metricForm.封禁率" data-testid="metric-block-rate" type="number" min="0" step="0.01" />
        </label>
        <label class="filter-item">
          <span>平均延迟ms</span>
          <input v-model.number="metricForm.平均延迟" data-testid="metric-latency" type="number" min="0" />
        </label>
        <label class="filter-item">
          <span>版本口径</span>
          <select v-model="prefer" data-testid="metric-prefer">
            <option value="latest">最新版（current）</option>
            <option value="frozen">冻结版（frozen）</option>
          </select>
        </label>
        <button class="btn primary" data-testid="run-compare" type="submit" :disabled="comparing">
          {{ comparing ? '决策中…' : '对比两入口命中结论' }}
        </button>
      </form>

      <div v-if="compareResult" class="analysis-card" data-testid="compare-result">
        <p>
          策略版本：<strong>{{ compareResult.策略版本 }}</strong>
          ｜规则指纹：<code>{{ compareResult.规则指纹 }}</code>
          ｜结论一致性：
          <strong data-testid="compare-flag">{{ compareResult.结论一致 ? '一致' : '不一致' }}</strong>
        </p>
        <table class="data-table">
          <thead>
            <tr><th>入口</th><th>是否命中</th><th>命中指标</th><th>建议延迟ms</th><th>熔断</th></tr>
          </thead>
          <tbody>
            <tr data-testid="compare-crawler">
              <td>采集入口</td>
              <td>{{ compareResult.采集入口结论?.hit ? '命中' : '未命中' }}</td>
              <td>{{ formatTriggered(compareResult.采集入口结论) }}</td>
              <td>{{ compareResult.采集入口结论?.delay_ms ?? '—' }}</td>
              <td>{{ compareResult.采集入口结论?.circuit_open ? '熔断开启' : '正常' }}</td>
            </tr>
            <tr data-testid="compare-proxy">
              <td>代理切换入口</td>
              <td>{{ compareResult.代理入口结论?.hit ? '命中' : '未命中' }}</td>
              <td>{{ formatTriggered(compareResult.代理入口结论) }}</td>
              <td>{{ compareResult.代理入口结论?.delay_ms ?? '—' }}</td>
              <td>{{ compareResult.代理入口结论?.circuit_open ? '熔断开启' : '正常' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4>策略版本（阈值调整后旧版自动冻结，当前版本随做法改变）</h4>
      <table class="data-table">
        <thead>
          <tr><th>策略版本</th><th>版本状态</th><th>触发条件</th><th>延迟范围</th><th>熔断阈值</th><th>变更说明</th><th>发布时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="version in versions" :key="String(version.策略版本)" :data-testid="`version-row-${version.策略版本}`">
            <td>{{ version.策略版本 }}</td>
            <td>{{ version.版本状态 === 'current' ? '最新版' : '冻结版' }}</td>
            <td>{{ version.触发条件 }}</td>
            <td>{{ version.延迟范围 }}</td>
            <td>{{ version.熔断阈值 }}</td>
            <td>{{ version.变更说明 }}</td>
            <td>{{ version.发布时间 }}</td>
          </tr>
          <tr v-if="!versions.length"><td colspan="7" class="empty-state">暂无策略版本</td></tr>
        </tbody>
      </table>

      <h4>反爬决策数据快照（绑定策略版本与触发条件指纹，重复提交只生效一次）</h4>
      <table class="data-table">
        <thead>
          <tr><th>快照编号</th><th>策略版本</th><th>来源入口</th><th>命中</th><th>建议延迟ms</th><th>熔断</th><th>创建时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="snapshot in snapshots" :key="String(snapshot.快照编号)" :data-testid="`snapshot-row-${snapshot.快照编号}`">
            <td>{{ snapshot.快照编号 }}</td>
            <td>{{ snapshot.策略版本 }}</td>
            <td>{{ snapshot.来源入口 === 'crawler' ? '采集入口' : '代理切换入口' }}</td>
            <td>{{ snapshot.命中结论?.hit ? '命中' : '未命中' }}</td>
            <td>{{ snapshot.命中结论?.delay_ms ?? '—' }}</td>
            <td>{{ snapshot.命中结论?.circuit_open ? '熔断开启' : '正常' }}</td>
            <td>{{ snapshot.创建时间 }}</td>
          </tr>
          <tr v-if="!snapshots.length"><td colspan="7" class="empty-state">暂无决策快照，先在上方发起统一决策</td></tr>
        </tbody>
      </table>

      <h4>历史请求记录（按当时策略版本与原阈值解释，统一决策不追溯改写）</h4>
      <table class="data-table">
        <thead>
          <tr><th>记录编号</th><th>请求批次</th><th>来源入口</th><th>策略版本</th><th>命中结论</th><th>记录时间</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in history" :key="String(item.记录编号)" :data-testid="`history-row-${item.记录编号}`">
            <td>{{ item.记录编号 }}</td>
            <td>{{ item.请求批次 }}</td>
            <td>{{ item.来源入口 === 'crawler' ? '采集入口' : '代理切换入口' }}</td>
            <td>{{ item.策略版本 }}</td>
            <td>{{ item.命中结论?.hit ? '命中' : '未命中' }}</td>
            <td>{{ item.记录时间 }}</td>
            <td>{{ item.说明 }}</td>
          </tr>
          <tr v-if="!history.length"><td colspan="7" class="empty-state">暂无历史请求记录</td></tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条反爬对抗记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type LooseRow = Record<string, unknown>
type Conclusion = {
  hit?: boolean
  triggered?: Array<Record<string, string | number>>
  delay_ms?: number
  circuit_open?: boolean
}
type SnapshotRow = LooseRow & { 命中结论?: Conclusion }
type HistoryRow = LooseRow & { 命中结论?: { hit?: boolean } }
type CompareResult = {
  策略版本: string
  规则指纹: string
  结论一致: boolean
  采集入口结论: Conclusion
  代理入口结论: Conclusion
} | null

const ENDPOINT = '/api/antiblock'
const columns = ["策略编号", "策略名称", "触发条件", "延迟范围", "UA伪装", "Referer生成", "Cookie策略", "熔断阈值"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["反爬策略总数", "待校验反爬策略", "已启用反爬策略"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

// 统一决策面板状态
const metricForm = reactive<Record<string, number>>({ 连续失败: 4, 封禁率: 0.25, 平均延迟: 3200 })
const prefer = ref<'latest' | 'frozen'>('latest')
const comparing = ref(false)
const compareResult = ref<CompareResult>(null)
const versions = ref<LooseRow[]>([])
const snapshots = ref<SnapshotRow[]>([])
const history = ref<HistoryRow[]>([])

function formatTriggered(conclusion: Conclusion | null | undefined): string {
  if (!conclusion || !conclusion.triggered || !conclusion.triggered.length) return '—'
  return conclusion.triggered.map((item) => String(item.指标)).join('、')
}

async function loadDecisionData() {
  try {
    const [versionResp, snapshotResp, historyResp] = await Promise.all([
      request(`${ENDPOINT}/versions`),
      request(`${ENDPOINT}/snapshots`),
      request(`${ENDPOINT}/history`),
    ])
    if (versionResp.ok) versions.value = (await versionResp.json()).items ?? []
    if (snapshotResp.ok) snapshots.value = (await snapshotResp.json()).items ?? []
    if (historyResp.ok) history.value = (await historyResp.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '统一决策数据读取失败'
  }
}

async function runCompare() {
  comparing.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/decisions/compare`, {
      method: 'POST',
      body: JSON.stringify({
        metrics: {
          连续失败: metricForm.连续失败,
          封禁率: metricForm.封禁率,
          平均延迟: metricForm.平均延迟,
        },
        prefer: prefer.value,
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '统一决策失败')
    compareResult.value = payload.entry
    await loadDecisionData()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '统一决策失败'
  } finally {
    comparing.value = false
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'antiblock.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('反爬对抗动作未生效，请稍后重试')
    }
    await reload()
    await loadDecisionData()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '反爬对抗操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('反爬策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '反爬对抗列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadDecisionData()
})
</script>
