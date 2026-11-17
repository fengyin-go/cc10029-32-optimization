<template>
  <section class="page" data-module="proxy">
    <header class="page-head">
      <div>
        <h2>代理池管理</h2>
        <p class="page-desc">管理代理 IP 的来源、存活检测和区域分发，为采集请求提供透明的代理切换。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记代理节点</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出代理池清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无代理池数据，可先登记代理节点</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记代理节点</h3>
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

    <section class="decision-panel" data-testid="switch-panel">
      <h3>代理切换清单（回写自反爬决策数据快照）</h3>
      <p class="page-desc">
        切换动作必须来自当前一致的策略版本；同一快照只生效一次，代理池拒绝第二份结果。
      </p>
      <form class="filter-bar" @submit.prevent="createSwitch">
        <label class="filter-item">
          <span>决策快照编号</span>
          <input v-model="snapshotId" data-testid="switch-snapshot-id" placeholder="粘贴反爬决策快照编号" required />
        </label>
        <label class="filter-item">
          <span>代理节点（可选）</span>
          <input v-model="proxyNodeId" data-testid="switch-node-id" placeholder="留空则自动选择" />
        </label>
        <button class="btn primary" data-testid="switch-submit" type="submit" :disabled="switching">
          {{ switching ? '生成中…' : '按快照生成切换清单' }}
        </button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>切换编号</th><th>来源快照编号</th><th>策略版本</th><th>代理节点</th>
            <th>命中</th><th>建议延迟ms</th><th>熔断</th><th>来源入口</th><th>写入时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in switchList" :key="String(item.切换编号)" :data-testid="`switch-row-${item.切换编号}`">
            <td>{{ item.切换编号 }}</td>
            <td>{{ item.来源快照编号 }}</td>
            <td>{{ item.策略版本 }}</td>
            <td>{{ item.代理节点 }}</td>
            <td>{{ item.命中 ? '命中' : '未命中' }}</td>
            <td>{{ item.建议延迟ms ?? '—' }}</td>
            <td>{{ item.熔断开启 ? '熔断开启' : '正常' }}</td>
            <td>{{ item.来源入口 === 'crawler' ? '采集入口' : '代理切换入口' }}</td>
            <td>{{ item.写入时间 }}</td>
          </tr>
          <tr v-if="!switchList.length">
            <td colspan="9" class="empty-state">暂无切换清单，先到反爬对抗生成决策快照</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条代理池记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type LooseRow = Record<string, unknown>

const ENDPOINT = '/api/proxy'
const columns = ["节点编号", "IP地址", "端口号", "协议类型", "匿程度", "响应延迟", "存活检测", "所属区域"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["代理节点总数", "待校验代理节点", "已启用代理节点"]

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

// 代理切换清单（数据来源：反爬决策数据快照）
const switchList = ref<LooseRow[]>([])
const snapshotId = ref('')
const proxyNodeId = ref('')
const switching = ref(false)

async function loadSwitches() {
  try {
    const response = await request(`${ENDPOINT}/switch-list`)
    if (!response.ok) throw new Error('切换清单读取失败')
    switchList.value = (await response.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '切换清单读取失败'
  }
}

async function createSwitch() {
  switching.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/switch-list`, {
      method: 'POST',
      body: JSON.stringify({
        snapshot_id: snapshotId.value.trim(),
        proxy_node_id: proxyNodeId.value.trim() || null,
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '切换清单生成失败')
    snapshotId.value = ''
    proxyNodeId.value = ''
    await loadSwitches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '切换清单生成失败'
  } finally {
    switching.value = false
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
    link.download = 'proxy.csv'
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
      throw new Error('代理池动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('代理节点列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadSwitches()
})
</script>
