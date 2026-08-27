<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'

interface BarData {
  time: string; open: number; high: number; low: number; close: number; volume: number
  ema9?: number; ema20?: number; vwap?: number
  boll_upper?: number; boll_mid?: number; boll_lower?: number
  macd_line?: number; macd_signal?: number; macd_hist?: number
}

const chartRef = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

const selectedDate = ref('')
const timeframe = ref('1m')
const dates = ref<string[]>([])
const loading = ref(false)
const error = ref('')
const barCount = ref(0)
const priceRange = ref('')

function isRegularSession(value: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date(value))
  const hour = Number(parts.find(part => part.type === 'hour')?.value ?? 0) % 24
  const minute = Number(parts.find(part => part.type === 'minute')?.value ?? 0)
  const minutes = hour * 60 + minute
  return minutes >= 9 * 60 + 30 && minutes < 16 * 60
}

async function loadDates() {
  try {
    const res = await api.get('/market-data/availability')
    dates.value = res.data.filter((d: any) => d.bars).map((d: any) => d.date).sort().reverse()
    if (dates.value.length && !selectedDate.value) {
      selectedDate.value = dates.value[0]
    }
  } catch { /* availability may not be ready */ }
}

async function loadKline() {
  if (!selectedDate.value) return
  loading.value = true
  error.value = ''
  try {
    const res = await api.get('/market-data/kline', {
      params: { date: selectedDate.value, timeframe: timeframe.value },
    })
    const sourceBars: BarData[] = res.data.bars
    const bars = timeframe.value === 'day'
      ? sourceBars
      : sourceBars.filter(bar => isRegularSession(bar.time))
    barCount.value = bars.length
    if (bars.length) {
      const highs = bars.map(b => b.high)
      const lows = bars.map(b => b.low)
      priceRange.value = `${Math.min(...lows).toFixed(2)} – ${Math.max(...highs).toFixed(2)}`
    } else {
      priceRange.value = ''
    }
    renderChart(bars)
  } catch (e: any) {
    error.value = e.response?.data?.message || '加载失败'
    barCount.value = 0
    priceRange.value = ''
  } finally {
    loading.value = false
  }
}

function renderChart(bars: BarData[]) {
  if (!chart) return
  const categoryData: string[] = []
  const ohlc: number[][] = []
  const volumes: number[] = []
  const volumeColors: string[] = []
  const ema9Data: (number | null)[] = []
  const ema20Data: (number | null)[] = []
  const vwapData: (number | null)[] = []
  const bollUpper: (number | null)[] = []
  const bollMid: (number | null)[] = []
  const bollLower: (number | null)[] = []
  const macdLine: (number | null)[] = []
  const macdSignal: (number | null)[] = []
  const macdHist: (number | null)[] = []

  for (const b of bars) {
    const d = new Date(b.time)
    const label = d.toLocaleString('en-US', {
      timeZone: 'America/New_York',
      hour: '2-digit', minute: '2-digit', hour12: false,
    })
    categoryData.push(label)
    ohlc.push([b.open, b.close, b.low, b.high])
    volumes.push(b.volume)
    volumeColors.push(b.close >= b.open ? 'rgba(38,166,91,0.6)' : 'rgba(220,53,69,0.6)')
    ema9Data.push(b.ema9 ?? null)
    ema20Data.push(b.ema20 ?? null)
    vwapData.push(b.vwap ?? null)
    bollUpper.push(b.boll_upper ?? null)
    bollMid.push(b.boll_mid ?? null)
    bollLower.push(b.boll_lower ?? null)
    macdLine.push(b.macd_line ?? null)
    macdSignal.push(b.macd_signal ?? null)
    macdHist.push(b.macd_hist ?? null)
  }

  chart.setOption({
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#0d1b2a',
      borderColor: '#1a3350',
      textStyle: { color: '#c8d6e5', fontSize: 12 },
      formatter(params: any) {
        const k = params.find((p: any) => p.seriesName === 'K线')
        const v = params.find((p: any) => p.seriesName === '成交量')
        const bU = params.find((p: any) => p.seriesName === 'BOLL上轨')
        const bM = params.find((p: any) => p.seriesName === 'BOLL中轨')
        const bL = params.find((p: any) => p.seriesName === 'BOLL下轨')
        const ml = params.find((p: any) => p.seriesName === 'MACD')
        const ms = params.find((p: any) => p.seriesName === 'Signal')
        const mh = params.find((p: any) => p.seriesName === 'Histogram')
        if (!k) return ''
        const [open, close, low, high] = k.data
        const pct = ((close - open) / open * 100).toFixed(2)
        const color = close >= open ? '#26a65b' : '#dc3545'
        let html = `<b>${k.axisValue}</b><br/>` +
          `开 <b>${open.toFixed(2)}</b>　高 <b>${high.toFixed(2)}</b><br/>` +
          `低 <b>${low.toFixed(2)}</b>　收 <b style="color:${color}">${close.toFixed(2)}</b><br/>` +
          `涨跌 <b style="color:${color}">${pct}%</b>`
        if (v) html += `<br/>成交量 <b>${v.data.toLocaleString()}</b>`
        const e9 = params.find((p: any) => p.seriesName === 'EMA9')
        const e20 = params.find((p: any) => p.seriesName === 'EMA21')
        const vw = params.find((p: any) => p.seriesName === 'VWAP')
        if (e9?.data != null || e20?.data != null) html += `<br/>EMA9 <span style="color:#22c55e">${e9?.data?.toFixed(2) ?? '—'}</span> EMA21 <span style="color:#f472b6">${e20?.data?.toFixed(2) ?? '—'}</span>`
        if (vw?.data != null) html += ` VWAP <span style="color:#a78bfa">${vw.data.toFixed(2)}</span>`
        if (bU?.data != null) html += `<br/>BOLL <span style="color:#e6a23c">上${bU.data.toFixed(2)}</span> 中${bM?.data?.toFixed(2) ?? '—'} <span style="color:#409eff">下${bL?.data?.toFixed(2) ?? '—'}</span>`
        if (ml?.data != null) html += `<br/>MACD <span style="color:#e6a23c">${ml.data.toFixed(4)}</span> Signal <span style="color:#409eff">${ms?.data?.toFixed(4) ?? '—'}</span> Hist <b>${mh?.data?.toFixed(4) ?? '—'}</b>`
        return html
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 60, right: 30, top: 20, height: '48%' },
      { left: 60, right: 30, top: '58%', height: '12%' },
      { left: 60, right: 30, top: '74%', height: '18%' },
    ],
    xAxis: [
      {
        type: 'category', data: categoryData, boundaryGap: true,
        axisLine: { lineStyle: { color: '#1a3350' } },
        axisLabel: { color: '#68809b', fontSize: 11 },
        splitLine: { show: false },
        gridIndex: 0,
      },
      {
        type: 'category', data: categoryData, boundaryGap: true,
        axisLine: { lineStyle: { color: '#1a3350' } },
        axisLabel: { show: false },
        splitLine: { show: false },
        gridIndex: 1,
      },
      {
        type: 'category', data: categoryData, boundaryGap: true,
        axisLine: { lineStyle: { color: '#1a3350' } },
        axisLabel: { color: '#68809b', fontSize: 10 },
        splitLine: { show: false },
        gridIndex: 2,
      },
    ],
    yAxis: [
      {
        scale: true,
        axisLine: { lineStyle: { color: '#1a3350' } },
        axisLabel: { color: '#68809b', fontSize: 11 },
        splitLine: { lineStyle: { color: '#0e2233' } },
        gridIndex: 0,
      },
      {
        scale: true,
        axisLine: { lineStyle: { color: '#1a3350' } },
        axisLabel: { show: false },
        splitLine: { show: false },
        gridIndex: 1,
      },
      {
        scale: true,
        axisLine: { lineStyle: { color: '#1a3350' } },
        axisLabel: { color: '#68809b', fontSize: 10 },
        splitLine: { lineStyle: { color: '#0e2233', type: 'dashed' } },
        gridIndex: 2,
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], start: 0, end: 100 },
      {
        type: 'slider', xAxisIndex: [0, 1, 2], bottom: 4, height: 18,
        borderColor: '#1a3350', backgroundColor: '#081421',
        fillerColor: 'rgba(20,60,100,0.3)',
        textStyle: { color: '#68809b', fontSize: 10 },
      },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: ohlc,
        xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: {
          color: '#26a65b', color0: '#dc3545',
          borderColor: '#26a65b', borderColor0: '#dc3545',
        },
      },
      {
        name: 'BOLL上轨', type: 'line', data: bollUpper,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: '#e6a23c', width: 1 },
        itemStyle: { color: '#e6a23c' },
        symbol: 'none', smooth: false, connectNulls: true,
      },
      {
        name: 'BOLL中轨', type: 'line', data: bollMid,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: '#909399', width: 1, type: 'dashed' },
        itemStyle: { color: '#909399' },
        symbol: 'none', smooth: false, connectNulls: true,
      },
      {
        name: 'BOLL下轨', type: 'line', data: bollLower,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: '#409eff', width: 1 },
        itemStyle: { color: '#409eff' },
        symbol: 'none', smooth: false, connectNulls: true,
      },
      {
        name: 'EMA9', type: 'line', data: ema9Data,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: '#22c55e', width: 1.2 },
        itemStyle: { color: '#22c55e' },
        symbol: 'none', connectNulls: true,
      },
      {
        name: 'EMA21', type: 'line', data: ema20Data,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: '#f472b6', width: 1.2 },
        itemStyle: { color: '#f472b6' },
        symbol: 'none', connectNulls: true,
      },
      {
        name: 'VWAP', type: 'line', data: vwapData,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: '#a78bfa', width: 1.5, type: 'dotted' },
        itemStyle: { color: '#a78bfa' },
        symbol: 'none', connectNulls: true,
      },
      {
        name: '成交量', type: 'bar', data: volumes,
        xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: {
          color: (params: any) => volumeColors[params.dataIndex] || 'rgba(38,166,91,0.6)',
        },
      },
      {
        name: 'MACD', type: 'line', data: macdLine,
        xAxisIndex: 2, yAxisIndex: 2,
        lineStyle: { color: '#e6a23c', width: 1.5 },
        itemStyle: { color: '#e6a23c' },
        symbol: 'none', connectNulls: true,
      },
      {
        name: 'Signal', type: 'line', data: macdSignal,
        xAxisIndex: 2, yAxisIndex: 2,
        lineStyle: { color: '#409eff', width: 1.5 },
        itemStyle: { color: '#409eff' },
        symbol: 'none', connectNulls: true,
      },
      {
        name: 'Histogram', type: 'bar', data: macdHist,
        xAxisIndex: 2, yAxisIndex: 2,
        itemStyle: {
          color: (params: any) => {
            const v = params.data as number | null
            if (v == null) return 'transparent'
            return v >= 0 ? 'rgba(38,166,91,0.7)' : 'rgba(220,53,69,0.7)'
          },
        },
      },
    ],
    legend: {
      data: ['K线', 'EMA9', 'EMA21', 'VWAP', 'BOLL上轨', 'BOLL中轨', 'BOLL下轨', '成交量', 'MACD', 'Signal', 'Histogram'],
      top: -5,
      textStyle: { color: '#68809b', fontSize: 11 },
      itemWidth: 14, itemHeight: 10,
      selectedMode: true,
    },
  }, true)
}

watch([() => selectedDate.value, () => timeframe.value], () => loadKline())

onMounted(async () => {
  chart = echarts.init(chartRef.value!, 'dark')
  chart.getZr().setCursorStyle('default')
  window.addEventListener('resize', handleResize)
  await loadDates()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
})

function handleResize() { chart?.resize() }

function disabledDate(d: Date) {
  // Use local date parts to avoid timezone offset issues
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return !dates.value.includes(iso)
}
</script>

<template>
  <div class="kline-page">
    <div class="panel">
      <div class="panel-title">
        <div>
          <h2>QQQ K线图</h2>
          <span>EMA9/21 · VWAP · BOLL(20,2) · MACD(8,17,9)</span>
        </div>
        <div class="kline-stats" v-if="barCount">
          <span>{{ barCount }} 根K线</span>
          <span v-if="priceRange">价格区间 {{ priceRange }}</span>
        </div>
      </div>

      <div class="toolbar compact">
        <el-date-picker
          v-model="selectedDate"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="选择交易日 (ET)"
          :disabled-date="disabledDate"
          style="width: 160px"
        />
        <span class="tz-hint">美东时间</span>
        <el-select v-model="timeframe" style="width: 100px">
          <el-option label="1 分钟" value="1m" />
          <el-option label="5 分钟" value="5m" />
          <el-option label="日线" value="day" />
        </el-select>
        <el-button type="primary" size="small" :loading="loading" @click="loadKline">刷新</el-button>
      </div>

      <div v-if="error" class="error-msg">{{ error }}</div>

      <div ref="chartRef" class="kline-chart" v-loading="loading" />

      <div v-if="!selectedDate && !loading" class="empty">请选择一个有数据的交易日</div>
    </div>
  </div>
</template>

<style scoped>
.kline-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.kline-chart {
  width: 100%;
  height: 640px;
  min-height: 500px;
}
.kline-stats {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: #68809b;
}
.tz-hint {
  font-size: 11px;
  color: #68809b;
  margin-left: 4px;
}
.error-msg {
  padding: 10px 14px;
  margin-bottom: 8px;
  background: rgba(220, 53, 69, 0.1);
  border: 1px solid rgba(220, 53, 69, 0.3);
  border-radius: 6px;
  color: #dc3545;
  font-size: 13px;
}
</style>
