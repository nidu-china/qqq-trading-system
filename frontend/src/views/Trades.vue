<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, localTime, money } from '../api'

const loading = ref(false), rows = ref<any[]>([]), total = ref(0), detail = ref<any>(), drawer = ref(false)
const filter = reactive({ dates: [] as string[], symbol:'', direction:'', pnl_sign:'', page:1, page_size:20 })
async function load(){ loading.value=true; try{ const {data}=await api.get('/trades',{params:{start_date:filter.dates?.[0],end_date:filter.dates?.[1],symbol:filter.symbol||undefined,direction:filter.direction||undefined,pnl_sign:filter.pnl_sign||undefined,page:filter.page,page_size:filter.page_size}});rows.value=data.items;total.value=data.total }finally{loading.value=false} }
async function open(row:any){ detail.value=(await api.get(`/trades/${row.id}`)).data;drawer.value=true }
function exportCsv(){ if(!rows.value.length){ElMessage.warning('当前没有可导出的记录');return} const fields=['symbol','direction','quantity','entry_price','exit_price','pnl','fees','entry_at','exit_at','exit_reason'];const csv=[fields.join(','),...rows.value.map(r=>fields.map(f=>JSON.stringify(r[f]??'')).join(','))].join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv'}));a.download='qqq-trades.csv';a.click();URL.revokeObjectURL(a.href) }
onMounted(load)
</script>
<template>
  <div class="panel">
    <div class="panel-title"><h2>成交与平仓记录</h2><span>共 {{ total }} 条</span></div>
    <div class="toolbar"><el-date-picker v-model="filter.dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期"/><el-input v-model="filter.symbol" placeholder="合约代码" clearable style="width:210px"/><el-select v-model="filter.direction" placeholder="方向" clearable style="width:110px"><el-option label="Call" value="call"/><el-option label="Put" value="put"/></el-select><el-select v-model="filter.pnl_sign" placeholder="盈亏" clearable style="width:110px"><el-option label="盈利" value="profit"/><el-option label="亏损" value="loss"/><el-option label="持平" value="flat"/></el-select><el-button type="primary" @click="filter.page=1;load()">查询</el-button><span class="spacer"></span><el-button @click="exportCsv">导出 CSV</el-button></div>
    <el-table v-loading="loading" :data="rows" @row-click="open" style="cursor:pointer"><el-table-column prop="symbol" label="合约" min-width="190"/><el-table-column prop="direction" label="方向" width="80"/><el-table-column prop="quantity" label="数量" width="70"/><el-table-column label="入场价" width="100"><template #default="s">{{ money(s.row.entry_price) }}</template></el-table-column><el-table-column label="出场价" width="100"><template #default="s">{{ money(s.row.exit_price) }}</template></el-table-column><el-table-column label="净盈亏" width="120"><template #default="s"><b :class="Number(s.row.pnl)>=0?'positive':'negative'">{{ money(s.row.pnl) }}</b></template></el-table-column><el-table-column prop="exit_reason" label="退出原因" min-width="140"/><el-table-column label="退出时间" min-width="175"><template #default="s">{{ localTime(s.row.exit_at) }}</template></el-table-column></el-table>
    <div style="display:flex;justify-content:flex-end;margin-top:18px"><el-pagination v-model:current-page="filter.page" v-model:page-size="filter.page_size" :total="total" layout="total, sizes, prev, pager, next" @change="load"/></div>
  </div>
  <el-drawer v-model="drawer" title="交易详情" size="430px"><div v-if="detail" class="kv"><div v-for="(value,key) in detail" :key="key"><label>{{ key }}</label><b>{{ String(value??'—') }}</b></div></div></el-drawer>
</template>
