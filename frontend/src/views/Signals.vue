<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, localTime, money } from '../api'

const loading=ref(false), signals=ref<any[]>([]), total=ref(0), jobs=ref<any[]>([]), selectedJob=ref(''), detail=ref<any>(), drawer=ref(false)
const filter=reactive({dates:[] as string[],action:'',status:'',page:1,page_size:100})
let timer:number
const backtestSignals=computed(()=>jobs.value.find(x=>x.id===selectedJob.value)?.result?.signal_records||[])
const key=(item:any)=>`${Math.round(new Date(item.decision_at).getTime()/300000)}:${item.action}:${item.direction}`
const comparison=computed(()=>{
  if(!selectedJob.value)return {matched:0,different:0,liveOnly:0,backtestOnly:0,rows:[] as any[]}
  const liveMap=new Map<string,any>(signals.value.map(x=>[key(x),x])), backMap=new Map<string,any>(backtestSignals.value.map((x:any)=>[key(x),x])), rows:any[]=[]
  for(const [itemKey,live] of liveMap){const back=backMap.get(itemKey);if(!back)rows.push({key:itemKey,result:'仅实盘/Paper',live,back:null});else if(live.status===back.status&&live.reason===back.reason)rows.push({key:itemKey,result:'一致',live,back});else rows.push({key:itemKey,result:'决策不同',live,back})}
  for(const [itemKey,back] of backMap)if(!liveMap.has(itemKey))rows.push({key:itemKey,result:'仅回测',live:null,back})
  return {matched:rows.filter(x=>x.result==='一致').length,different:rows.filter(x=>x.result==='决策不同').length,liveOnly:rows.filter(x=>x.result==='仅实盘/Paper').length,backtestOnly:rows.filter(x=>x.result==='仅回测').length,rows:rows.filter(x=>x.result!=='一致')}
})
async function load(){loading.value=true;try{const [s,b]=await Promise.all([api.get('/signals',{params:{start_date:filter.dates?.[0],end_date:filter.dates?.[1],action:filter.action||undefined,status:filter.status||undefined,page:filter.page,page_size:filter.page_size}}),api.get('/backtests')]);signals.value=s.data.items;total.value=s.data.total;jobs.value=b.data.filter((x:any)=>x.status==='completed'&&x.result?.signal_records)}finally{loading.value=false}}
function open(row:any){detail.value=row;drawer.value=true}
onMounted(async()=>{await load();timer=window.setInterval(load,5000)});onBeforeUnmount(()=>clearInterval(timer))
</script>

<template>
  <div class="panel">
    <div class="panel-title"><div><h2>实盘 / Paper 信号流水</h2><span>Paper 与实盘都先持久化买入或卖出信号，再向对应券商提交订单；策略拒绝也会保留</span></div><span>共 {{ total }} 条</span></div>
    <div class="toolbar"><el-date-picker v-model="filter.dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期"/><el-select v-model="filter.action" clearable placeholder="动作" style="width:110px"><el-option label="买入" value="buy"/><el-option label="卖出" value="sell"/></el-select><el-select v-model="filter.status" clearable placeholder="状态" style="width:130px"><el-option label="待执行" value="accepted"/><el-option label="已拒绝" value="rejected"/><el-option label="已执行" value="executed"/><el-option label="执行失败" value="failed"/></el-select><el-button type="primary" @click="filter.page=1;load()">查询</el-button><span class="spacer"></span><span style="color:var(--muted);font-size:11px">每 5 秒刷新</span></div>
    <el-table v-loading="loading" :data="signals" @row-click="open" style="cursor:pointer"><el-table-column label="时间" min-width="180"><template #default="s">{{ localTime(s.row.decision_at) }}</template></el-table-column><el-table-column label="动作" width="85"><template #default="s"><b :class="s.row.action==='buy'?'positive':'negative'">{{ s.row.action==='buy'?'买入':'卖出' }}</b></template></el-table-column><el-table-column prop="direction" label="方向" width="80"/><el-table-column prop="status" label="状态" width="100"/><el-table-column prop="symbol" label="合约" min-width="190"><template #default="s">{{ s.row.symbol||'QQQ 策略信号' }}</template></el-table-column><el-table-column label="参考/成交价" width="130"><template #default="s">{{ money(s.row.price) }}</template></el-table-column><el-table-column prop="quantity" label="数量" width="70"><template #default="s">{{ s.row.quantity??'—' }}</template></el-table-column><el-table-column prop="reason" label="原因" min-width="190"/></el-table>
    <div style="display:flex;justify-content:flex-end;margin-top:18px"><el-pagination v-model:current-page="filter.page" v-model:page-size="filter.page_size" :total="total" :page-sizes="[50,100,200]" layout="total, sizes, prev, pager, next" @change="load"/></div>
  </div>

  <div class="panel" style="margin-top:18px">
    <div class="panel-title"><div><h2>与回测信号对比</h2><span>按同一 5 分钟K线、买卖动作和方向匹配</span></div><el-select v-model="selectedJob" clearable placeholder="选择已完成回测" style="width:320px"><el-option v-for="job in jobs" :key="job.id" :label="`${job.request.start_date} → ${job.request.end_date} · ${job.id.slice(0,8)}`" :value="job.id"/></el-select></div>
    <template v-if="selectedJob"><div class="grid metrics"><div class="panel metric mini"><label>一致</label><strong class="positive">{{ comparison.matched }}</strong></div><div class="panel metric mini"><label>决策不同</label><strong class="negative">{{ comparison.different }}</strong></div><div class="panel metric mini"><label>仅实盘/Paper</label><strong>{{ comparison.liveOnly }}</strong></div><div class="panel metric mini"><label>仅回测</label><strong>{{ comparison.backtestOnly }}</strong></div></div><el-table :data="comparison.rows" style="margin-top:18px"><el-table-column prop="result" label="差异" width="120"><template #default="s"><b :class="s.row.result==='决策不同'?'negative':''">{{ s.row.result }}</b></template></el-table-column><el-table-column label="决策时间" min-width="180"><template #default="s">{{ localTime((s.row.live||s.row.back).decision_at) }}</template></el-table-column><el-table-column label="动作/方向" width="120"><template #default="s">{{ (s.row.live||s.row.back).action }} / {{ (s.row.live||s.row.back).direction }}</template></el-table-column><el-table-column label="实盘/Paper"><template #default="s">{{ s.row.live?`${s.row.live.status} · ${s.row.live.reason}`:'—' }}</template></el-table-column><el-table-column label="回测"><template #default="s">{{ s.row.back?`${s.row.back.status} · ${s.row.back.reason}`:'—' }}</template></el-table-column></el-table></template><div v-else class="empty">选择同日期范围的回测任务后查看信号差异</div>
  </div>
  <el-drawer v-model="drawer" title="信号指标快照" size="460px"><div v-if="detail" class="kv"><div><label>时间</label><b>{{ localTime(detail.decision_at) }}</b></div><div><label>动作与方向</label><b>{{ detail.action }} / {{ detail.direction }}</b></div><div><label>状态</label><b>{{ detail.status }}</b></div><div><label>原因</label><b>{{ detail.reason||'—' }}</b></div></div><pre>{{ JSON.stringify(detail?.indicators||{},null,2) }}</pre></el-drawer>
</template>
<style scoped>.mini{padding:14px}.mini strong{font-size:20px}.mini label{font-size:10px}pre{margin-top:18px;padding:15px;background:#081421;border:1px solid var(--line);border-radius:8px;color:#8fb6dd;white-space:pre-wrap;font:12px/1.7 Consolas,monospace}</style>
