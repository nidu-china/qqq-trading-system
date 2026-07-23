<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, localTime, etTime, money } from '../api'

const loading = ref(false)
const liveSignals = ref<any[]>([]), liveTotal = ref(0)
const jobs = ref<any[]>([]), selectedJob = ref(''), backtestSignals = ref<any[]>([])
const detail = ref<any>(), drawer = ref(false)
const liveFilter = reactive({ dates: [] as string[], action: '', status: '', page: 1, page_size: 50 })
const btFilter = reactive({ action: '', status: '' })
let timer: number

async function loadLive() {
  loading.value = true
  try {
    const res = await api.get('/signals', {
      params: {
        start_date: liveFilter.dates?.[0],
        end_date: liveFilter.dates?.[1],
        action: liveFilter.action || undefined,
        status: liveFilter.status || undefined,
        page: liveFilter.page,
        page_size: liveFilter.page_size,
      },
    })
    liveSignals.value = res.data.items
    liveTotal.value = res.data.total
  } finally { loading.value = false }
}

async function loadJobs() {
  const res = await api.get('/backtests')
  jobs.value = res.data.filter((x: any) => x.status === 'completed' && x.result?.signal_records?.length)
}

function loadBacktest() {
  const job = jobs.value.find(x => x.id === selectedJob.value)
  if (!job) { backtestSignals.value = []; return }
  let records = job.result.signal_records || []
  if (btFilter.action) records = records.filter((r: any) => r.action === btFilter.action)
  if (btFilter.status) records = records.filter((r: any) => r.status === btFilter.status)
  backtestSignals.value = records
}

function open(row: any) { detail.value = row; drawer.value = true }

onMounted(async () => { await loadJobs(); await loadLive(); timer = window.setInterval(loadLive, 5000) })
onBeforeUnmount(() => clearInterval(timer))

const STATUS_MAP: Record<string, { text: string; type: string }> = {
  accepted: { text: '待执行', type: 'primary' },
  executed: { text: '已执行', type: 'success' },
  rejected: { text: '已拒绝', type: 'warning' },
  failed: { text: '失败', type: 'danger' },
}
</script>

<template>
  <div class="signals-layout">
    <!-- 实盘/Paper 信号 -->
    <div class="panel">
      <div class="panel-title">
        <div><h2>实盘 / Paper 信号</h2><span>数据库实时信号流水</span></div>
        <span>共 {{ liveTotal }} 条</span>
      </div>
      <div class="toolbar compact">
        <el-date-picker v-model="liveFilter.dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始" end-placeholder="结束" style="width:220px"/>
        <el-select v-model="liveFilter.action" clearable placeholder="动作" style="width:90px" @change="liveFilter.page=1;loadLive()">
          <el-option label="买入" value="buy"/><el-option label="卖出" value="sell"/>
        </el-select>
        <el-select v-model="liveFilter.status" clearable placeholder="状态" style="width:100px" @change="liveFilter.page=1;loadLive()">
          <el-option label="待执行" value="accepted"/><el-option label="已拒绝" value="rejected"/><el-option label="已执行" value="executed"/><el-option label="失败" value="failed"/>
        </el-select>
        <el-button type="primary" size="small" @click="liveFilter.page=1;loadLive()">查询</el-button>
      </div>
      <el-table v-loading="loading" :data="liveSignals" @row-click="open" size="small" style="cursor:pointer" max-height="420">
        <el-table-column label="K线时间 (ET)" min-width="165"><template #default="s">{{ etTime(s.row.decision_at) }}</template></el-table-column>
        <el-table-column label="动作" width="60"><template #default="s"><b :class="s.row.action==='buy'?'positive':'negative'">{{ s.row.action==='buy'?'买':'卖' }}</b></template></el-table-column>
        <el-table-column label="方向" width="65"><template #default="s"><el-tag :type="s.row.direction==='call'?'success':'danger'" size="small">{{ s.row.direction==='call'?'Call':'Put' }}</el-tag></template></el-table-column>
        <el-table-column label="状态" width="80"><template #default="s"><el-tag :type="(STATUS_MAP[s.row.status]?.type as any)||'info'" size="small">{{ STATUS_MAP[s.row.status]?.text||s.row.status }}</el-tag></template></el-table-column>
        <el-table-column label="价格" width="80" align="right"><template #default="s">{{ s.row.price ? '$'+Number(s.row.price).toFixed(2) : '—' }}</template></el-table-column>
        <el-table-column label="原因" min-width="160"><template #default="s"><span class="reason-text">{{ s.row.reason||'—' }}</span></template></el-table-column>
      </el-table>
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <el-pagination v-model:current-page="liveFilter.page" v-model:page-size="liveFilter.page_size" :total="liveTotal" :page-sizes="[50,100]" layout="total, prev, pager, next" size="small" @change="loadLive"/>
      </div>
      <div v-if="!liveSignals.length && !loading" class="empty">暂无信号记录（Paper 模式运行后将自动产生）</div>
    </div>

    <!-- 回测信号 -->
    <div class="panel">
      <div class="panel-title">
        <div><h2>回测信号</h2><span>选择回测任务查看其信号</span></div>
        <span v-if="backtestSignals.length">{{ backtestSignals.length }} 条</span>
      </div>
      <div class="toolbar compact">
        <el-select v-model="selectedJob" clearable placeholder="选择回测任务" style="width:280px" @change="loadBacktest">
          <el-option v-for="job in jobs" :key="job.id" :label="`${job.request.start_date}→${job.request.end_date} (${job.result.signal_records.length}条)`" :value="job.id"/>
        </el-select>
        <el-select v-model="btFilter.action" clearable placeholder="动作" style="width:90px" @change="loadBacktest">
          <el-option label="买入" value="buy"/><el-option label="卖出" value="sell"/>
        </el-select>
        <el-select v-model="btFilter.status" clearable placeholder="状态" style="width:100px" @change="loadBacktest">
          <el-option label="已执行" value="executed"/><el-option label="已拒绝" value="rejected"/>
        </el-select>
      </div>
      <el-table :data="backtestSignals" @row-click="open" size="small" style="cursor:pointer" max-height="420">
        <el-table-column label="K线时间 (ET)" min-width="165"><template #default="s">{{ etTime(s.row.decision_at) }}</template></el-table-column>
        <el-table-column label="动作" width="60"><template #default="s"><b :class="s.row.action==='buy'?'positive':'negative'">{{ s.row.action==='buy'?'买':'卖' }}</b></template></el-table-column>
        <el-table-column label="方向" width="65"><template #default="s"><el-tag :type="s.row.direction==='call'?'success':'danger'" size="small">{{ s.row.direction==='call'?'Call':'Put' }}</el-tag></template></el-table-column>
        <el-table-column label="状态" width="80"><template #default="s"><el-tag :type="(STATUS_MAP[s.row.status]?.type as any)||'info'" size="small">{{ STATUS_MAP[s.row.status]?.text||s.row.status }}</el-tag></template></el-table-column>
        <el-table-column label="价格" width="80" align="right"><template #default="s">{{ s.row.price ? '$'+Number(s.row.price).toFixed(2) : '—' }}</template></el-table-column>
        <el-table-column label="原因" min-width="160"><template #default="s"><span class="reason-text">{{ s.row.reason||'—' }}</span></template></el-table-column>
      </el-table>
      <div v-if="!selectedJob" class="empty">选择一个已完成的回测任务</div>
      <div v-else-if="!backtestSignals.length" class="empty">该回测无匹配信号</div>
    </div>
  </div>

  <el-drawer v-model="drawer" title="信号详情" size="460px">
    <div v-if="detail" class="detail-grid">
      <div class="detail-row"><label>K线时间 (ET)</label><b>{{ etTime(detail.decision_at) }}</b></div>
      <div class="detail-row"><label>动作</label><b>{{ detail.action === 'buy' ? '买入' : '卖出' }}</b></div>
      <div class="detail-row"><label>方向</label><b>{{ detail.direction === 'call' ? '看涨 (Call)' : '看跌 (Put)' }}</b></div>
      <div class="detail-row"><label>状态</label><el-tag :type="(STATUS_MAP[detail.status]?.type as any)||'info'" size="small">{{ STATUS_MAP[detail.status]?.text || detail.status }}</el-tag></div>
      <div class="detail-row"><label>合约</label><b>{{ detail.symbol || '—' }}</b></div>
      <div class="detail-row"><label>价格</label><b>{{ detail.price ? money(detail.price) : '—' }}</b></div>
      <div class="detail-row"><label>数量</label><b>{{ detail.quantity ?? '—' }}</b></div>
      <div class="detail-row"><label>原因</label><b>{{ detail.reason || '—' }}</b></div>
    </div>
    <div v-if="detail?.indicators" class="detail-section">
      <h4>指标快照</h4>
      <pre>{{ JSON.stringify(detail.indicators, null, 2) }}</pre>
    </div>
  </el-drawer>
</template>

<style scoped>
.signals-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
@media (max-width: 1200px) {
  .signals-layout { grid-template-columns: 1fr }
}
.toolbar.compact { gap: 8px; flex-wrap: wrap }
.reason-text { font-size: 11px; color: #7890ad }
.detail-grid { display: flex; flex-direction: column; gap: 2px }
.detail-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1a2a3d }
.detail-row label { font-size: 13px; color: #68809b }
.detail-row b { font-size: 14px; color: #c8d6e5 }
.detail-section { margin-top: 24px }
.detail-section h4 { font-size: 13px; color: #7890ad; margin-bottom: 10px }
pre { padding: 14px; background: #081421; border: 1px solid #172a40; border-radius: 8px; color: #8fb6dd; white-space: pre-wrap; font: 12px/1.7 Consolas, monospace }
</style>
