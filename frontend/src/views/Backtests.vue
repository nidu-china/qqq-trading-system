<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getInstanceByDom, init, use } from 'echarts/core'
import { LineChart, ScatterChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, MarkPointComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api, localTime, etTime, money, percent } from '../api'
use([LineChart, ScatterChart, BarChart, GridComponent, TooltipComponent, LegendComponent, MarkPointComponent, DataZoomComponent, CanvasRenderer])
const availability=ref<any[]>([]),jobs=ref<any[]>([]),versions=ref<any[]>([]),selected=ref<any>(),submitting=ref(false)
const form=reactive({
  dates:[] as string[],
  starting_equity:'10000',
  config_version:undefined as number|undefined,
})
const completeDates=computed(()=>availability.value.filter(x=>x.bars).map(x=>x.date))
const chartRef=ref<HTMLElement>(), equityChartRef=ref<HTMLElement>(), dateKey=(d:Date)=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`
let timer:number

const showParams = ref(false)
const strategyMode = ref('')
const strategyParamsMeta = ref<Record<string, any>>({})
const params = reactive<Record<string, any>>({})

async function loadStrategyParams() {
  try {
    const res = await api.get('/strategy-params')
    strategyParamsMeta.value = res.data
    resetParamsDefaults()
  } catch {}
}

function resetParamsDefaults() {
  const meta = strategyParamsMeta.value
  for (const group of Object.values(meta) as any[]) {
    for (const p of (group.params || [])) {
      if (params[p.key] === undefined && p.default !== undefined) {
        params[p.key] = p.default
      }
    }
  }
}

const activeStrategyGroups = computed(() => {
  const meta = strategyParamsMeta.value
  const groups: { name: string; label: string; params: any[] }[] = []
  if (meta.shared) groups.push({ name: 'shared', label: meta.shared.label, params: meta.shared.params })
  const mode = strategyMode.value
  if (mode && meta[mode]) {
    groups.push({ name: mode, label: meta[mode].label, params: meta[mode].params })
  } else {
    if (meta.boll_macd) groups.push({ name: 'boll_macd', label: meta.boll_macd.label, params: meta.boll_macd.params })
    if (meta.trend) groups.push({ name: 'trend', label: meta.trend.label, params: meta.trend.params })
  }
  return groups
})

const flattenedTrades = computed(() => {
  const trades = selected.value?.result?.trades
  if (!trades) return []
  const rows: any[] = []
  for (const t of trades) {
    const legs = t.exit_legs
    if (legs && legs.length > 1) {
      for (const leg of legs) {
        rows.push({
          ...t,
          leg_quantity: leg.quantity,
          leg_exit_at: leg.exit_at,
          leg_price: leg.price,
          leg_pnl: leg.pnl,
          leg_reason: leg.reason,
          leg_stop_price: leg.stop_price,
          leg_trigger_bid: leg.trigger_bid,
          leg_fill_bid: leg.fill_bid,
          leg_stop_penetration: leg.stop_penetration,
          leg_stop_penetration_pct: leg.stop_penetration_pct,
        })
      }
    } else {
      rows.push(t)
    }
  }
  return rows
})

async function loadConfig() {
  try {
    const res = await api.get('/config')
    const vals = res.data.values || {}
    Object.keys(vals).forEach(k => { params[k] = vals[k] })
  } catch {}
}

async function load(){const [a,j,v]=await Promise.all([api.get('/market-data/availability'),api.get('/backtests'),api.get('/config/versions')]);availability.value=a.data;jobs.value=j.data;versions.value=v.data;if(selected.value)selected.value=jobs.value.find(x=>x.id===selected.value.id)||selected.value}
async function submit(){
  if(form.dates.length!==2){ElMessage.warning('请选择回测日期范围');return}
  submitting.value=true
  try{
    const payload: any = {
      start_date: form.dates[0],
      end_date: form.dates[1],
      starting_equity: form.starting_equity,
      config_version: form.config_version,
    }
    if (strategyMode.value) {
      payload.strategy_mode = strategyMode.value
    }
    if (showParams.value) {
      payload.params = { ...params }
    }
    selected.value=(await api.post('/backtests', payload)).data
    await load()
    ElMessage.success('回测任务已进入队列')
  } finally { submitting.value=false }
}
async function cancel(job:any){await api.delete(`/backtests/${job.id}`);await load()}
async function deleteJob(job:any){await api.delete(`/backtests/${job.id}`);if(selected.value?.id===job.id)selected.value=undefined;await load()}
onMounted(async()=>{await Promise.all([load(),loadConfig(),loadLabels(),loadStrategyParams()]);timer=window.setInterval(()=>{if(jobs.value.some(j=>j.status==='queued'||j.status==='running'))load()},2500)});onBeforeUnmount(()=>clearInterval(timer))
watch(()=>selected.value?.result,async res=>{if(!res?.price_series?.length)return;await nextTick();if(!chartRef.value)return;const chart=getInstanceByDom(chartRef.value)||init(chartRef.value);
const fmt=(v:string)=>new Date(v).toLocaleString('en-US',{timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})
const ps=res.price_series
const times=ps.map((x:any)=>fmt(x.time))
const prices=ps.map((x:any)=>x.price)
const bbUpper=ps.map((x:any)=>x.bb_upper??null)
const bbLower=ps.map((x:any)=>x.bb_lower??null)
const bbMid=ps.map((x:any)=>x.bb_middle??null)
const ema9=ps.map((x:any)=>x.ema9??null)
const ema21=ps.map((x:any)=>x.ema21??null)
const macdLine=ps.map((x:any)=>x.macd??null)
const macdSignal=ps.map((x:any)=>x.macd_signal??null)
const macdHist=ps.map((x:any)=>x.macd_hist??null)
const vol=ps.map((x:any)=>x.volume??0)
const buyPoints=(res.trades||[]).filter((t:any)=>t.entry_at).map((t:any)=>{const idx=ps.reduce((best:number,p:any,i:number)=>Math.abs(new Date(p.time).getTime()-new Date(t.entry_at).getTime())<Math.abs(new Date(ps[best].time).getTime()-new Date(t.entry_at).getTime())?i:best,0);return[idx,prices[idx]]})
const sellPoints=(res.trades||[]).filter((t:any)=>t.exit_at).map((t:any)=>{const idx=ps.reduce((best:number,p:any,i:number)=>Math.abs(new Date(p.time).getTime()-new Date(t.exit_at).getTime())<Math.abs(new Date(ps[best].time).getTime()-new Date(t.exit_at).getTime())?i:best,0);return[idx,prices[idx]]})
chart.setOption({
  grid:[
    {left:60,right:20,top:30,height:'46%'},
    {left:60,right:20,top:'56%',height:'12%'},
    {left:60,right:20,top:'72%',height:'18%'},
  ],
  tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
  legend:{data:['QQQ','EMA9','EMA21','布林上轨','布林中轨','布林下轨','买入','卖出','MACD','Signal','Histogram','成交量'],top:0,textStyle:{color:'#7890ad',fontSize:10}},
  xAxis:[
    {type:'category',data:times,gridIndex:0,axisLabel:{show:false}},
    {type:'category',data:times,gridIndex:1,axisLabel:{show:false}},
    {type:'category',data:times,gridIndex:2,axisLabel:{color:'#7890ad',fontSize:9,rotate:30}},
  ],
  yAxis:[
    {type:'value',scale:true,gridIndex:0,axisLabel:{color:'#7890ad',formatter:'${value}'},splitLine:{lineStyle:{color:'#1a2a3d'}}},
    {type:'value',scale:true,gridIndex:1,axisLabel:{color:'#7890ad',fontSize:9},splitLine:{lineStyle:{color:'#1a2a3d',type:'dashed'}}},
    {type:'value',gridIndex:2,axisLabel:{color:'#7890ad',fontSize:9},splitLine:{lineStyle:{color:'#1a2a3d'}}},
  ],
  series:[
    {name:'QQQ',type:'line',xAxisIndex:0,yAxisIndex:0,data:prices,showSymbol:false,lineStyle:{color:'#3457d5',width:1.5},z:2},
    {name:'EMA9',type:'line',xAxisIndex:0,yAxisIndex:0,data:ema9,showSymbol:false,lineStyle:{color:'#22c55e',width:1},connectNulls:true,z:1},
    {name:'EMA21',type:'line',xAxisIndex:0,yAxisIndex:0,data:ema21,showSymbol:false,lineStyle:{color:'#ef4444',width:1},connectNulls:true,z:1},
    {name:'布林上轨',type:'line',xAxisIndex:0,yAxisIndex:0,data:bbUpper,showSymbol:false,lineStyle:{color:'#f59e0b',width:1,type:'dashed'},z:1},
    {name:'布林中轨',type:'line',xAxisIndex:0,yAxisIndex:0,data:bbMid,showSymbol:false,lineStyle:{color:'#7890ad',width:1,type:'dotted'},z:1},
    {name:'布林下轨',type:'line',xAxisIndex:0,yAxisIndex:0,data:bbLower,showSymbol:false,lineStyle:{color:'#f59e0b',width:1,type:'dashed'},z:1},
    {name:'买入',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:buyPoints,symbol:'triangle',symbolSize:14,itemStyle:{color:'#22c55e'},z:10},
    {name:'卖出',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:sellPoints,symbol:'diamond',symbolSize:14,itemStyle:{color:'#ef4444'},z:10},
    {name:'MACD',type:'line',xAxisIndex:1,yAxisIndex:1,data:macdLine,showSymbol:false,lineStyle:{color:'#e6a23c',width:1.5},connectNulls:true},
    {name:'Signal',type:'line',xAxisIndex:1,yAxisIndex:1,data:macdSignal,showSymbol:false,lineStyle:{color:'#409eff',width:1.5},connectNulls:true},
    {name:'Histogram',type:'bar',xAxisIndex:1,yAxisIndex:1,data:macdHist,itemStyle:{color:(p:any)=>{const v=p.data as number|null;if(v==null)return'transparent';return v>=0?'rgba(38,166,91,0.7)':'rgba(220,53,69,0.7)'}}},
    {name:'成交量',type:'bar',xAxisIndex:2,yAxisIndex:2,data:vol,barWidth:'60%',itemStyle:{color:'rgba(34,197,94,0.5)'}},
  ],
  dataZoom:[{type:'inside',xAxisIndex:[0,1,2],start:0,end:100}]
},true)},{deep:true})

watch(()=>selected.value?.result?.equity_curve,async curve=>{
  if(!curve?.length)return
  await nextTick()
  if(!equityChartRef.value)return
  const chart=getInstanceByDom(equityChartRef.value)||init(equityChartRef.value)
  const fmt=(v:string)=>new Date(v).toLocaleString('en-US',{timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})
  chart.setOption({
    grid:{left:70,right:20,top:24,bottom:55},
    tooltip:{trigger:'axis'},
    xAxis:{type:'category',data:curve.map((x:any)=>fmt(x.time)),axisLabel:{color:'#7890ad',fontSize:9,rotate:30}},
    yAxis:{type:'value',scale:true,axisLabel:{color:'#7890ad',formatter:'${value}'},splitLine:{lineStyle:{color:'#1a2a3d'}}},
    series:[{name:'盘中权益',type:'line',data:curve.map((x:any)=>Number(x.equity)),showSymbol:false,lineStyle:{color:'#2dd4bf',width:1.5},areaStyle:{color:'rgba(45,212,191,0.08)'}}],
    dataZoom:[{type:'inside',start:0,end:100}],
  },true)
},{deep:true})

import { useLabels } from '../composables/useLabels'
const { loadLabels, exitReasonLabel, exitReasonType, rejectLabel, regimeLabel } = useLabels()

function paramLabel(key: string): string {
  const meta = strategyParamsMeta.value
  for (const group of Object.values(meta) as any[]) {
    for (const p of (group.params || [])) {
      if (p.key === key) return p.label
    }
  }
  return key.replace(/_/g, ' ')
}

function visibleStrategyKeys(settings: Record<string, any>): string[] {
  return Object.keys(settings).filter(key => settings[key] !== undefined)
}
</script>
<template>
  <div class="panel">
    <div class="panel-title"><h2>创建回测</h2><span>单任务 FIFO 队列</span></div>
    <div class="toolbar">
      <el-date-picker v-model="form.dates" type="daterange" value-format="YYYY-MM-DD" :disabled-date="(d: Date)=>!completeDates.includes(dateKey(d))" start-placeholder="开始日期" end-placeholder="结束日期"/>
      <el-input v-model="form.starting_equity" placeholder="初始权益" style="width:150px"><template #prepend>$</template></el-input>
      <el-select v-model="strategyMode" clearable placeholder="当前策略" style="width:160px">
        <el-option label="BOLL/MACD" value="boll_macd"/>
        <el-option label="Trend ORB" value="trend"/>
        <el-option label="Hybrid" value="hybrid"/>
      </el-select>
      <el-select v-model="form.config_version" clearable placeholder="当前环境参数" style="width:180px"><el-option v-for="v in versions" :key="v.version" :label="`参数版本 v${v.version}`" :value="v.version"/></el-select>
      <el-button :type="showParams?'warning':'default'" @click="showParams=!showParams">{{ showParams ? '收起参数' : '自定义参数' }}</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">开始回测</el-button>
    </div>

    <!-- 参数配置面板 -->
    <el-collapse-transition>
      <div v-show="showParams" class="params-panel">
        <div v-for="group in activeStrategyGroups" :key="group.name" class="params-group">
          <h4>{{ group.label }}</h4>
          <div class="param-grid">
            <div v-for="p in group.params" :key="p.key" class="param-cell">
              <label>{{ p.label }}</label>
              <el-switch v-if="p.type === 'bool'" v-model="params[p.key]" size="small"/>
              <el-input v-else-if="p.type === 'text'" v-model="params[p.key]" size="small" style="width:120px"/>
              <el-time-picker v-else-if="p.type === 'time'" v-model="params[p.key]" size="small" style="width:120px" format="HH:mm:ss" value-format="HH:mm:ss"/>
              <el-input-number v-else-if="p.type === 'int'" v-model="params[p.key]" :min="p.min" :max="p.max" :step="1" controls-position="right" size="small" style="width:110px"/>
              <el-input-number v-else v-model="params[p.key]" :min="p.min" :max="p.max" :step="p.step || 0.01" controls-position="right" size="small" style="width:110px"/>
            </div>
          </div>
        </div>
      </div>
    </el-collapse-transition>
    <div class="data-strip">
      <span v-for="item in availability.slice(0,12)" :key="item.date" :title="`${item.date} K线:${item.bars} 期权:${item.options} VIX:${item.volatility_intraday}`" :class="{complete:item.bars&&item.options&&item.volatility_intraday}">{{ item.date.slice(5) }}</span>
    </div>
  </div>

  <div class="grid two" style="margin-top:18px">
    <!-- 任务队列 -->
    <div class="panel">
      <div class="panel-title"><h2>任务队列</h2><span>{{ jobs.length }} 条历史</span></div>
      <el-table :data="jobs" @row-click="(r: any)=>selected=r">
        <el-table-column label="时间" min-width="150"><template #default="s">{{ localTime(s.row.created_at) }}</template></el-table-column>
        <el-table-column label="范围" min-width="150"><template #default="s">{{ s.row.request.start_date }} → {{ s.row.request.end_date }}</template></el-table-column>
        <el-table-column prop="status" label="状态" width="105"/>
        <el-table-column label="进度" width="120"><template #default="s"><el-progress :percentage="s.row.progress" :show-text="false"/></template></el-table-column>
        <el-table-column width="80"><template #default="s"><el-button v-if="['queued','running'].includes(s.row.status)" link type="danger" @click.stop="cancel(s.row)">取消</el-button><el-button v-else link type="danger" @click.stop="deleteJob(s.row)">删除</el-button></template></el-table-column>
      </el-table>
    </div>

    <!-- 回测结果 -->
    <div class="panel result-panel">
      <div class="panel-title"><h2>回测结果</h2><span>{{ selected?.id?.slice(0,8)||'未选择' }}</span></div>
      <template v-if="selected?.result">
        <!-- 核心指标 -->
        <div class="result-metrics">
          <div class="metric-card">
            <label>净收益</label>
            <strong :class="Number(selected.result.net_pnl)>=0?'positive':'negative'">{{ money(selected.result.net_pnl) }}</strong>
          </div>
          <div class="metric-card">
            <label>收益率</label>
            <strong :class="Number(selected.result.return_rate)>=0?'positive':'negative'">{{ percent(selected.result.return_rate) }}</strong>
          </div>
          <div class="metric-card">
            <label>交易笔数</label>
            <strong>{{ selected.result.trade_count ?? 0 }}</strong>
          </div>
          <div class="metric-card">
            <label>胜率</label>
            <strong>{{ percent(selected.result.win_rate) }}</strong>
          </div>
          <div class="metric-card">
            <label>盈亏比</label>
            <strong>{{ selected.result.profit_factor ? Number(selected.result.profit_factor).toFixed(2) : '—' }}</strong>
          </div>
          <div class="metric-card">
            <label>最大回撤</label>
            <strong class="negative">{{ money(selected.result.max_drawdown) }}</strong>
          </div>
          <div class="metric-card" v-if="selected.result.realized_max_drawdown !== undefined">
            <label>已实现最大回撤</label>
            <strong class="negative">{{ money(selected.result.realized_max_drawdown) }}</strong>
          </div>
        </div>

        <!-- QQQ 走势 + 买卖点 -->
        <div v-if="selected.result.price_series?.length" class="result-section">
          <h3>QQQ 走势与交易点</h3>
          <div ref="chartRef" style="height:560px"></div>
        </div>

        <div v-if="selected.result.equity_curve?.length" class="result-section">
          <h3>盘中权益曲线（已实现 + 持仓按 Bid 清算）</h3>
          <div ref="equityChartRef" style="height:300px"></div>
        </div>

        <!-- 信号与数据统计 -->
        <div class="result-section">
          <h3>信号统计</h3>
          <div class="stat-row">
            <span class="stat-label">产生信号</span>
            <span class="stat-value">{{ selected.result.signals }} 个</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">成功入场</span>
            <span class="stat-value highlight">{{ selected.result.trade_count ?? 0 }} 笔</span>
          </div>
          <div v-if="selected.result.rejected && Object.keys(selected.result.rejected).length" class="stat-row">
            <span class="stat-label">被拒绝</span>
            <span class="stat-value">{{ Object.values(selected.result.rejected).reduce((a: number, b: any) => a + Number(b), 0) }} 个</span>
          </div>
        </div>

        <!-- 拒绝原因 -->
        <div v-if="selected.result.rejected && Object.keys(selected.result.rejected).length" class="result-section">
          <h3>拒绝原因明细</h3>
          <div v-for="(count, key) in selected.result.rejected" :key="key" class="reject-item">
            <span class="reject-label">{{ rejectLabel(key as string) }}</span>
            <el-tag size="small" type="warning">{{ count }}</el-tag>
          </div>
        </div>

        <!-- 波动率状态 -->
        <div v-if="selected.result.volatility_regimes && Object.keys(selected.result.volatility_regimes).length" class="result-section">
          <h3>波动率环境</h3>
          <div v-for="(count, key) in selected.result.volatility_regimes" :key="key" class="reject-item">
            <span class="reject-label">{{ regimeLabel(key as string) }}</span>
            <el-tag size="small" :type="key === 'normal' ? 'success' : key === 'unavailable' ? 'info' : 'danger'">{{ count }}</el-tag>
          </div>
        </div>

        <!-- 数据完整性 -->
        <div class="result-section">
          <h3>数据完整性</h3>
          <div class="stat-row">
            <span class="stat-label">期权报价</span>
            <el-tag size="small" :type="selected.result.option_data_complete ? 'success' : 'warning'">
              {{ selected.result.option_data_complete ? '完整' : '不完整（使用模拟价格）' }}
            </el-tag>
          </div>
          <div class="stat-row">
            <span class="stat-label">VIX 数据</span>
            <el-tag size="small" :type="selected.result.volatility_data_complete ? 'success' : 'warning'">
              {{ selected.result.volatility_data_complete ? '完整' : '部分缺失' }}
            </el-tag>
          </div>
        </div>

        <!-- 回测参数 -->
        <div v-if="selected.result?.settings_used" class="result-section">
          <h3>策略参数</h3>
          <div class="params-grid">
            <div v-for="key in visibleStrategyKeys(selected.result.settings_used)" :key="key" class="param-item">
              <span class="stat-label">{{ paramLabel(key) }}</span>
              <span class="stat-value">{{ selected.result.settings_used[key] }}</span>
            </div>
          </div>
        </div>
      </template>
      <div v-else-if="selected?.error" class="empty negative">{{ selected.error }}</div>
      <div v-else class="empty">{{ selected ? `任务${selected.status==='running'?'运行中':'等待执行'}` : '从队列选择一个任务' }}</div>
    </div>
  </div>

  <!-- 交易明细 -->
  <div v-if="selected?.result?.trades?.length" class="panel" style="margin-top:18px">
    <div class="panel-title"><h2>回测交易明细</h2><span>{{ selected.result.trades.length }} 笔</span></div>
    <el-table :data="flattenedTrades" stripe row-class-name="trade-row">
      <el-table-column label="合约" min-width="180"><template #default="s"><code>{{ s.row.symbol }}</code></template></el-table-column>
      <el-table-column label="方向" width="70"><template #default="s"><el-tag :type="s.row.direction==='call'?'success':'danger'" size="small">{{ s.row.direction==='call'?'看涨':'看跌' }}</el-tag></template></el-table-column>
      <el-table-column label="数量" width="60" align="center"><template #default="s">{{ s.row.leg_quantity ?? s.row.quantity }}</template></el-table-column>
      <el-table-column label="入场时间" width="140"><template #default="s">{{ etTime(s.row.entry_at) }}</template></el-table-column>
      <el-table-column label="入场价" width="90" align="right"><template #default="s">${{ Number(s.row.entry_price).toFixed(2) }}</template></el-table-column>
      <el-table-column label="出场时间" width="140"><template #default="s">{{ etTime(s.row.leg_exit_at ?? s.row.exit_at) }}</template></el-table-column>
      <el-table-column label="出场价" width="90" align="right"><template #default="s">${{ Number(s.row.leg_price ?? s.row.exit_price).toFixed(2) }}</template></el-table-column>
      <el-table-column label="盈亏" width="100" align="right"><template #default="s"><span :class="Number(s.row.leg_pnl ?? s.row.pnl)>=0?'positive':'negative'">${{ Number(s.row.leg_pnl ?? s.row.pnl).toFixed(2) }}</span></template></el-table-column>
      <el-table-column label="出场原因" width="110"><template #default="s"><el-tag size="small" :type="exitReasonType(s.row.leg_reason ?? s.row.exit_reason)">{{ exitReasonLabel(s.row.leg_reason ?? s.row.exit_reason) }}</el-tag></template></el-table-column>
      <el-table-column label="止损线" width="90" align="right"><template #default="s"><span v-if="s.row.leg_stop_price != null">${{ Number(s.row.leg_stop_price).toFixed(2) }}</span><span v-else>—</span></template></el-table-column>
      <el-table-column label="触发 Bid" width="90" align="right"><template #default="s"><span v-if="s.row.leg_trigger_bid != null">${{ Number(s.row.leg_trigger_bid).toFixed(2) }}</span><span v-else>—</span></template></el-table-column>
      <el-table-column label="止损穿透" width="100" align="right"><template #default="s"><span v-if="s.row.leg_stop_penetration_pct != null" class="negative">{{ percent(s.row.leg_stop_penetration_pct) }}</span><span v-else>—</span></template></el-table-column>
    </el-table>
  </div>
</template>
<style scoped>
.data-strip{display:flex;gap:6px;flex-wrap:wrap}
.data-strip span{font:10px Consolas;color:#68809b;background:#091523;padding:5px 7px;border-radius:4px}
.data-strip span.complete{color:var(--green);border:1px solid #185842}

.result-panel{max-height:calc(100vh - 200px);overflow-y:auto}

.result-metrics{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:12px;
}
.metric-card{
  background:#091523;
  border-radius:8px;
  padding:12px 14px;
  display:flex;
  flex-direction:column;
  gap:4px;
}
.metric-card label{font-size:12px;color:#68809b}
.metric-card strong{font-size:18px;color:#e2e8f0}

.result-section{
  margin-top:18px;
  padding-top:14px;
  border-top:1px solid #1a2a3d;
}
.result-section h3{
  font-size:13px;
  color:#7890ad;
  margin-bottom:10px;
  font-weight:500;
}
.stat-row{
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:6px 0;
}
.stat-label{font-size:13px;color:#8899aa}
.stat-value{font-size:14px;color:#c8d6e5;font-weight:500}
.stat-value.highlight{color:#2dd4bf}

.reject-item{
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:5px 0;
}
.reject-label{font-size:13px;color:#8899aa}

.params-grid{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:4px 16px;
}
.param-item{
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:4px 0;
  font-size:12px;
}
.param-item .stat-label{color:#68809b}
.param-item .stat-value{color:#c8d6e5;font-family:Consolas,monospace}

.positive{color:var(--green,#22c55e)!important}
.negative{color:var(--red,#ef4444)!important}

.params-panel{
  margin-top:14px;
  padding:16px;
  background:#091523;
  border:1px solid #1a2a3d;
  border-radius:8px;
}
.params-group{margin-bottom:16px}
.params-group:last-child{margin-bottom:0}
.params-group h4{
  font-size:12px;
  color:#7890ad;
  margin-bottom:10px;
  font-weight:500;
  border-bottom:1px solid #172a40;
  padding-bottom:6px;
}
.param-grid{
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:8px;
}
.param-cell{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:6px;
  padding:6px 8px;
  background:#0a1625;
  border:1px solid #172a40;
  border-radius:6px;
}
.param-cell label{
  font-size:11px;
  color:#68809b;
  white-space:nowrap;
}
@media(max-width:1400px){.param-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:900px){.param-grid{grid-template-columns:repeat(2,1fr)}}

</style>
