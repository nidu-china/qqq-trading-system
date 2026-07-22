<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { money } from '../api'
import { useRuntimeStore } from '../stores/runtime'

const runtime = useRuntimeStore()
const { status, online, stateName } = storeToRefs(runtime)
onMounted(runtime.connect)
onBeforeUnmount(runtime.disconnect)
</script>

<template>
  <div class="grid metrics">
    <div class="panel metric"><label>系统状态</label><strong class="accent">{{ stateName }}</strong><small><span class="status-dot" :style="{background:online?'var(--green)':'var(--red)'}"></span>{{ online?'服务连接正常':'连接中断' }}</small></div>
    <div class="panel metric"><label>当日已实现盈亏</label><strong :class="Number(status.realized_pnl)>=0?'positive':'negative'">{{ money(status.realized_pnl) }}</strong><small>开盘权益 {{ money(status.opening_equity) }}</small></div>
    <div class="panel metric"><label>今日交易次数</label><strong>{{ status.trades_today || 0 }}</strong><small>当前模式 {{ (status.trading_mode || '—').toUpperCase() }}</small></div>
    <div class="panel metric"><label>当前配置版本</label><strong>v{{ status.pending_config_version || status.config_version || 0 }}</strong><small>{{ status.pending_config_version ? `待平仓后生效，运行中 v${status.config_version}` : '已在交易引擎生效' }}</small></div>
  </div>
  <div class="grid two" style="margin-top:18px">
    <div class="panel">
      <div class="panel-title"><h2>交易引擎</h2><span>{{ status.underlying }}</span></div>
      <div class="kv">
        <div><label>交易状态</label><b>{{ stateName }}</b></div><div><label>当前持仓</label><b>{{ status.position || '空仓' }}</b></div>
        <div><label>持仓参数版本</label><b>{{ status.position_config_version ? `v${status.position_config_version}` : '—' }}</b></div><div><label>错误信息</label><b :class="status.last_error?'negative':''">{{ status.last_error || '无' }}</b></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-title"><h2>波动率过滤</h2><span>VIX REGIME</span></div>
      <div v-if="status.volatility" class="kv"><div><label>状态</label><b class="accent">{{ status.volatility.regime }}</b></div><div><label>数值</label><b>{{ status.volatility.value || '—' }}</b></div><div><label>5分钟变化</label><b>{{ status.volatility.change_5m || '—' }}</b></div><div><label>15分钟变化</label><b>{{ status.volatility.change_15m || '—' }}</b></div></div>
      <div v-else class="empty">等待交易时段波动率快照</div>
    </div>
  </div>
</template>
