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
const form=reactive({dates:[] as string[],starting_equity:'100000',config_version:undefined as number|undefined})
const completeDates=computed(()=>availability.value.filter(x=>x.bars).map(x=>x.date))
const chartRef=ref<HTMLElement>(), dateKey=(d:Date)=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`
let timer:number

const showParams = ref(false)
const params = reactive<Record<string, any>>({
  orb_min_volume_ratio: 1.5,
  ema_fast_period: 9, ema_slow_period: 21,
  bollinger_period: 20, bollinger_stddev: 2.0,
  volume_average_period: 20, min_volume_ratio: 1.0,
  rsi_period: 14, rsi_call_max: 70, rsi_put_min: 30,
  bb_width_max: 0.02,
  strike_offset: 2.0,
  stop_loss_pct: 0.25, take_profit_1_pct: 0.50, take_profit_2_pct: 1.0,
  risk_per_trade: 0.005, daily_loss_limit: 0.02,
  max_contracts: 10, max_premium_fraction: 0.05,
  volatility_filter_enabled: true,
  entry_start: '09:45:00', entry_end: '14:00:00',
})

async function loadConfig() {
  try {
    const res = await api.get('/config')
    const vals = res.data.values || {}
    Object.keys(params).forEach(k => { if (vals[k] !== undefined) params[k] = vals[k] })
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
onMounted(async()=>{await load();await loadConfig();timer=window.setInterval(()=>{if(jobs.value.some(j=>j.status==='queued'||j.status==='running'))load()},2500)});onBeforeUnmount(()=>clearInterval(timer))
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
const vwapLine=ps.map((x:any)=>x.vwap??null)
const vol=ps.map((x:any)=>x.volume??0)
const buyPoints=(res.trades||[]).filter((t:any)=>t.entry_at).map((t:any)=>{const idx=ps.reduce((best:number,p:any,i:number)=>Math.abs(new Date(p.time).getTime()-new Date(t.entry_at).getTime())<Math.abs(new Date(ps[best].time).getTime()-new Date(t.entry_at).getTime())?i:best,0);return[idx,prices[idx]]})
const sellPoints=(res.trades||[]).filter((t:any)=>t.exit_at).map((t:any)=>{const idx=ps.reduce((best:number,p:any,i:number)=>Math.abs(new Date(p.time).getTime()-new Date(t.exit_at).getTime())<Math.abs(new Date(ps[best].time).getTime()-new Date(t.exit_at).getTime())?i:best,0);return[idx,prices[idx]]})
chart.setOption({
  grid:[{left:60,right:20,top:30,height:'60%'},{left:60,right:20,top:'75%',height:'18%'}],
  tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
  legend:{data:['QQQ','布林上轨','布林中轨','布林下轨','EMA9','EMA21','VWAP','买入','卖出','成交量'],top:0,textStyle:{color:'#7890ad',fontSize:10}},
  xAxis:[
    {type:'category',data:times,gridIndex:0,axisLabel:{show:false}},
    {type:'category',data:times,gridIndex:1,axisLabel:{color:'#7890ad',fontSize:9,rotate:30}},
  ],
  yAxis:[
    {type:'value',scale:true,gridIndex:0,axisLabel:{color:'#7890ad',formatter:'${value}'},splitLine:{lineStyle:{color:'#1a2a3d'}}},
    {type:'value',gridIndex:1,axisLabel:{color:'#7890ad',fontSize:9},splitLine:{lineStyle:{color:'#1a2a3d'}}},
  ],
  series:[
    {name:'QQQ',type:'line',xAxisIndex:0,yAxisIndex:0,data:prices,showSymbol:false,lineStyle:{color:'#3457d5',width:1.5},z:2},
    {name:'布林上轨',type:'line',xAxisIndex:0,yAxisIndex:0,data:bbUpper,showSymbol:false,lineStyle:{color:'#f59e0b',width:1,type:'dashed'},z:1},
    {name:'布林中轨',type:'line',xAxisIndex:0,yAxisIndex:0,data:bbMid,showSymbol:false,lineStyle:{color:'#7890ad',width:1,type:'dotted'},z:1},
    {name:'布林下轨',type:'line',xAxisIndex:0,yAxisIndex:0,data:bbLower,showSymbol:false,lineStyle:{color:'#f59e0b',width:1,type:'dashed'},z:1},
    {name:'EMA9',type:'line',xAxisIndex:0,yAxisIndex:0,data:ema9,showSymbol:false,lineStyle:{color:'#22c55e',width:1.2},z:1},
    {name:'EMA21',type:'line',xAxisIndex:0,yAxisIndex:0,data:ema21,showSymbol:false,lineStyle:{color:'#f472b6',width:1.2},z:1},
    {name:'VWAP',type:'line',xAxisIndex:0,yAxisIndex:0,data:vwapLine,showSymbol:false,lineStyle:{color:'#a78bfa',width:1.5,type:'dotted'},z:1},
    {name:'买入',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:buyPoints,symbol:'triangle',symbolSize:14,itemStyle:{color:'#22c55e'},z:10},
    {name:'卖出',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:sellPoints,symbol:'diamond',symbolSize:14,itemStyle:{color:'#ef4444'},z:10},
    {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,barWidth:'60%',itemStyle:{color:'rgba(100,130,170,0.5)'}},
  ],
  dataZoom:[{type:'inside',xAxisIndex:[0,1],start:0,end:100}]
},true)},{deep:true})

const REJECT_LABELS: Record<string, string> = {
  volatility_unavailable_stale_intraday_data: 'VIX 盘中数据过期',
  volatility_unavailable_missing_intraday_data: 'VIX 盘中数据缺失',
  volatility_unavailable_insufficient_daily_history: 'VIX 日线历史不足',
  volatility_unavailable_insufficient_intraday_history: 'VIX 盘中历史不足',
  volatility_risk_off: '波动率风险偏高',
  volatility_shock: '波动率剧烈冲击',
  missing_option_frame: '期权报价缺失',
  relative_spread_too_wide: '期权价差过大',
  absolute_spread_too_wide: '期权绝对价差过大',
  stale_quote: '报价过时',
}
function rejectLabel(key: string): string {
  return REJECT_LABELS[key] || key.replace(/_/g, ' ')
}
const REGIME_LABELS: Record<string, string> = {
  normal: '正常',
  elevated: '偏高',
  risk_off: '风险关闭',
  unavailable: '数据不可用',
}
function regimeLabel(key: string): string {
  return REGIME_LABELS[key] || key
}

const PARAM_LABELS: Record<string, string> = {
  orb_min_volume_ratio: 'ORB 量比阈值',
  ema_fast_period: 'EMA 快线周期',
  ema_slow_period: 'EMA 慢线周期',
  bollinger_period: '布林带周期',
  bollinger_stddev: '布林带标准差',
  volume_average_period: '量均值周期',
  min_volume_ratio: '趋势量比阈值',
  rsi_period: 'RSI 周期',
  rsi_call_max: 'RSI 超买',
  rsi_put_min: 'RSI 超卖',
  bb_width_max: 'BB宽度上限',
  strike_offset: '行权价偏移',
  stop_loss_pct: '止损比例',
  take_profit_1_pct: '第一止盈',
  take_profit_2_pct: '第二止盈',
  risk_per_trade: '单笔风险',
  daily_loss_limit: '日亏上限',
  max_contracts: '最大合约数',
  max_premium_fraction: '最大权利金比例',
  volatility_filter_enabled: 'VIX 过滤',
  entry_start: '开仓开始',
  entry_end: '开仓结束',
  forced_close: '强制平仓',
  cooldown_minutes: '冷却分钟',
  max_trades_per_day: '日最大交易',
}
function paramLabel(key: string): string {
  return PARAM_LABELS[key] || key.replace(/_/g, ' ')
}

const strategyKeys = [
  'orb_min_volume_ratio',
  'ema_fast_period', 'ema_slow_period',
  'bollinger_period', 'bollinger_stddev',
  'volume_average_period', 'min_volume_ratio',
  'rsi_period', 'rsi_call_max', 'rsi_put_min',
  'bb_width_max',
  'entry_start', 'entry_end',
  'strike_offset',
  'stop_loss_pct', 'take_profit_1_pct', 'take_profit_2_pct',
  'risk_per_trade', 'daily_loss_limit', 'max_contracts',
]
</script>
<template>
  <div class="panel">
    <div class="panel-title"><h2>创建回测</h2><span>单任务 FIFO 队列</span></div>
    <div class="toolbar">
      <el-date-picker v-model="form.dates" type="daterange" value-format="YYYY-MM-DD" :disabled-date="(d: Date)=>!completeDates.includes(dateKey(d))" start-placeholder="开始日期" end-placeholder="结束日期"/>
      <el-input v-model="form.starting_equity" placeholder="初始权益" style="width:150px"><template #prepend>$</template></el-input>
      <el-select v-model="form.config_version" clearable placeholder="当前环境参数" style="width:180px"><el-option v-for="v in versions" :key="v.version" :label="`参数版本 v${v.version}`" :value="v.version"/></el-select>
      <el-button :type="showParams?'warning':'default'" @click="showParams=!showParams">{{ showParams ? '收起参数' : '自定义参数' }}</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">开始回测</el-button>
    </div>

    <!-- 参数配置面板 -->
    <el-collapse-transition>
      <div v-show="showParams" class="params-panel">
        <div class="params-group">
          <h4>策略一：ORB 开盘突破 (9:35-10:00)</h4>
          <div class="param-row">
            <label>ORB 量比阈值</label><el-input-number v-model="params.orb_min_volume_ratio" :min="0.5" :max="5" :step="0.1" :precision="1" controls-position="right" size="small"/>
          </div>
        </div>
        <div class="params-group">
          <h4>策略二：EMA 趋势回踩 (10:00-11:30)</h4>
          <div class="param-row">
            <label>EMA 快线</label><el-input-number v-model="params.ema_fast_period" :min="3" :max="20" controls-position="right" size="small"/>
            <label>EMA 慢线</label><el-input-number v-model="params.ema_slow_period" :min="10" :max="50" controls-position="right" size="small"/>
            <label>量比阈值</label><el-input-number v-model="params.min_volume_ratio" :min="0.5" :max="5" :step="0.1" :precision="1" controls-position="right" size="small"/>
          </div>
        </div>
        <div class="params-group">
          <h4>策略三：BB+RSI 均值回归 (11:30-14:00)</h4>
          <div class="param-row">
            <label>布林带周期</label><el-input-number v-model="params.bollinger_period" :min="5" :max="50" controls-position="right" size="small"/>
            <label>标准差</label><el-input-number v-model="params.bollinger_stddev" :min="0.5" :max="5" :step="0.1" :precision="1" controls-position="right" size="small"/>
            <label>BB宽度上限</label><el-input-number v-model="params.bb_width_max" :min="0.005" :max="0.05" :step="0.005" :precision="3" controls-position="right" size="small"/>
          </div>
          <div class="param-row">
            <label>RSI 周期</label><el-input-number v-model="params.rsi_period" :min="5" :max="30" controls-position="right" size="small"/>
            <label>RSI 超卖</label><el-input-number v-model="params.rsi_put_min" :min="10" :max="40" controls-position="right" size="small"/>
            <label>RSI 超买</label><el-input-number v-model="params.rsi_call_max" :min="60" :max="90" controls-position="right" size="small"/>
          </div>
        </div>
        <div class="params-group">
          <h4>风险管理</h4>
          <div class="param-row">
            <label>止损</label><el-input-number v-model="params.stop_loss_pct" :min="0.1" :max="1" :step="0.05" :precision="2" controls-position="right" size="small"/>
            <label>止盈1</label><el-input-number v-model="params.take_profit_1_pct" :min="0.3" :max="5" :step="0.1" :precision="1" controls-position="right" size="small"/>
            <label>止盈2</label><el-input-number v-model="params.take_profit_2_pct" :min="0.5" :max="10" :step="0.1" :precision="1" controls-position="right" size="small"/>
          </div>
          <div class="param-row">
            <label>单笔风险</label><el-input-number v-model="params.risk_per_trade" :min="0.005" :max="0.1" :step="0.005" :precision="3" controls-position="right" size="small"/>
            <label>日亏上限</label><el-input-number v-model="params.daily_loss_limit" :min="0.01" :max="0.1" :step="0.005" :precision="3" controls-position="right" size="small"/>
            <label>最大合约</label><el-input-number v-model="params.max_contracts" :min="1" :max="50" controls-position="right" size="small"/>
          </div>
        </div>
        <div class="params-group">
          <h4>时间与过滤</h4>
          <div class="param-row">
            <label>开仓开始</label><el-time-picker v-model="params.entry_start" value-format="HH:mm:ss" format="HH:mm" size="small"/>
            <label>开仓结束</label><el-time-picker v-model="params.entry_end" value-format="HH:mm:ss" format="HH:mm" size="small"/>
            <label>VIX 过滤</label><el-switch v-model="params.volatility_filter_enabled" size="small"/>
          </div>
          <div class="param-row">
            <label>行权偏移 $</label><el-input-number v-model="params.strike_offset" :min="0" :max="20" :step="0.5" :precision="1" controls-position="right" size="small"/>
            <label>最大权利金比例</label><el-input-number v-model="params.max_premium_fraction" :min="0.01" :max="0.2" :step="0.01" :precision="2" controls-position="right" size="small"/>
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
        </div>

        <!-- QQQ 走势 + 买卖点 -->
        <div v-if="selected.result.price_series?.length" class="result-section">
          <h3>QQQ 走势与交易点</h3>
          <div ref="chartRef" style="height:480px"></div>
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
            <div v-for="key in strategyKeys.filter(k => selected.result.settings_used[k] !== undefined)" :key="key" class="param-item">
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
    <el-table :data="selected.result.trades" stripe>
      <el-table-column label="合约" min-width="200"><template #default="s"><code>{{ s.row.symbol }}</code></template></el-table-column>
      <el-table-column label="方向" width="80"><template #default="s"><el-tag :type="s.row.direction==='call'?'success':'danger'" size="small">{{ s.row.direction==='call'?'看涨':'看跌' }}</el-tag></template></el-table-column>
      <el-table-column prop="quantity" label="数量" width="70" align="center"/>
      <el-table-column label="入场价" width="100" align="right"><template #default="s">${{ Number(s.row.entry_price).toFixed(2) }}</template></el-table-column>
      <el-table-column label="出场价" width="100" align="right"><template #default="s">${{ Number(s.row.exit_price).toFixed(2) }}</template></el-table-column>
      <el-table-column label="盈亏" width="120" align="right"><template #default="s"><span :class="Number(s.row.pnl)>=0?'positive':'negative'">${{ Number(s.row.pnl).toFixed(2) }}</span></template></el-table-column>
      <el-table-column label="出场原因" width="120"><template #default="s"><el-tag size="small" :type="s.row.reason==='take_profit_1'||s.row.reason==='take_profit_2'?'success':s.row.reason==='stop_loss'?'danger':'info'">{{ s.row.reason }}</el-tag></template></el-table-column>
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
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:16px;
}
.params-group h4{
  font-size:12px;
  color:#7890ad;
  margin-bottom:10px;
  font-weight:500;
  border-bottom:1px solid #172a40;
  padding-bottom:6px;
}
.param-row{
  display:flex;
  align-items:center;
  gap:8px;
  margin-bottom:8px;
  flex-wrap:wrap;
}
.param-row label{
  font-size:11px;
  color:#68809b;
  white-space:nowrap;
  min-width:55px;
}
.param-row .el-input-number{width:100px}
.param-row .el-time-picker{width:100px}

@media(max-width:1400px){
  .params-panel{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:900px){
  .params-panel{grid-template-columns:1fr}
}
</style>
