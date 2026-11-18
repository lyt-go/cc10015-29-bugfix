<template>
  <section class="page" data-module="segment_compare">
    <header class="page-head">
      <div><h2>分群对比</h2><p class="page-desc">按同一成员集合计算分群对比结果表并落库，差异方向与显著性均来自同一对分群，列表与详情同源。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <input v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <label><span>重叠处理</span>
        <select v-model="params['重叠处理']">
          <option v-for="o in overlapOptions" :key="o" :value="o">{{ o }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody>
        <tr v-for="row in results" :key="row.id" :data-testid="`row-${row.id}`" @click="openDetail(row)" style="cursor:pointer">
          <td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <aside v-if="detail" class="side-detail" data-testid="compare-detail">
      <header class="page-head" style="margin:0 0 8px">
        <h3 style="margin:0">对比任务 {{ detail['任务编号'] }}</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>
      <dl class="dag-detail" style="border-top:none">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ detail[field] ?? '—' }}</dd>
        </template>
      </dl>
      <div class="page-actions" style="margin-top:12px">
        <button class="btn" type="button" data-testid="detail-recompute" @click="runAction('开始计算')">重新计算</button>
        <button class="btn primary" type="button" data-testid="detail-confirm" @click="runAction('确认完成')">确认完成</button>
      </div>
      <p v-if="actionMessage" style="font-size:12px;color:var(--muted)">{{ actionMessage }}</p>
    </aside>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/segment_compare'
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({ '重叠处理': '排除重叠' })
const paramFields = ["任务编号", "分群A", "分群B", "对比指标"]
const overlapOptions = ['排除重叠', '保留重叠']
const resultColumns = ["任务编号", "分群A", "分群B", "对比指标", "指标值", "对比基准", "差异", "差异方向", "显著性", "重叠处理", "排除数量"]
const detailFields = ["任务编号", "分群A", "分群B", "对比指标", "指标编号", "精度位数", "指标值", "对比基准", "差异", "差异方向", "显著性", "t统计量", "样本数A", "样本数B", "重叠成员数", "排除数量", "重叠处理", "重叠成员影响", "计算状态", "计算时间", "结果版本"]
const detail = ref<any | null>(null)
const actionMessage = ref('')
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function fetchList() {
  try {
    const res = await request(ENDPOINT)
    const data = await res.json()
    results.value = data.items || []
    chartData.value = results.value.map((r: any) => ({ label: String(r['任务编号'] ?? r.id), value: Math.abs(Number(r['差异']) || 0) })).slice(0, 6)
  } catch(e) { results.value = [] }
}
async function runAnalysis() {
  loading.value = true
  try {
    const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
    const data = await res.json()
    results.value = data.items || []
    await fetchList(); await fetchSummary()
  } catch(e) { results.value = [] }
  loading.value = false
}
async function openDetail(row: any) {
  actionMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${row.id}`)
    detail.value = await res.json()
  } catch(e) { detail.value = row }
}
async function runAction(action: string) {
  if (!detail.value) return
  try {
    const res = await request(`${ENDPOINT}/${detail.value.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) })
    const data = await res.json()
    actionMessage.value = data.message || ''
    if (data.ok && data.entry) detail.value = data.entry
    await fetchList(); await fetchSummary()
  } catch(e) { actionMessage.value = '动作未生效，请稍后重试' }
}
onMounted(() => { fetchSummary(); fetchList() })
</script>
