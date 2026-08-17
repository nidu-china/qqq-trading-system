<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, localTime } from '../api'

type Field = {
  key: string
  label: string
  unit?: string
  step?: number
  min?: number
  max?: number
  kind?: 'bool' | 'text'
}

const groups: Record<string, Field[]> = {
  'VIX 波动率过滤': [
    { key: 'volatility_filter_enabled', label: '启用 VIX 过滤', kind: 'bool' },
    { key: 'volatility_symbol', label: '波动率标的', kind: 'text' },
    { key: 'volatility_lookback_days', label: '回看交易日', unit: '日', min: 2 },
    { key: 'volatility_max_staleness_minutes', label: '最大滞后', unit: '分钟', min: 1 },
    { key: 'volatility_risk_off_percentile', label: 'Risk-off 分位', step: 0.01 },
    { key: 'volatility_recovery_percentile', label: 'Recovery 分位', step: 0.01 },
    { key: 'volatility_rise_5m', label: '5 分钟上升阈值', step: 0.01 },
    { key: 'volatility_rise_15m', label: '15 分钟上升阈值', step: 0.01 },
    { key: 'volatility_fall_5m', label: '5 分钟回落阈值', step: 0.01 },
    { key: 'volatility_fall_15m', label: '15 分钟回落阈值', step: 0.01 },
    { key: 'volatility_shock_5m', label: '5 分钟冲击阈值', step: 0.01 },
    { key: 'volatility_shock_15m', label: '15 分钟冲击阈值', step: 0.01 },
  ],
}

const active = ref('VIX 波动率过滤')
const version = ref(0)
const values = reactive<Record<string, any>>({})
const original = ref<Record<string, any>>({})
const versions = ref<any[]>([])
const saving = ref(false)
const state = ref<any>({})
const changes = computed(() => Object.keys(values).filter(k => String(values[k]) !== String(original.value[k])))

async function load() {
  const [config, history] = await Promise.all([api.get('/config'), api.get('/config/versions')])
  version.value = config.data.version
  Object.assign(values, config.data.values)
  original.value = { ...config.data.values }
  state.value = config.data
  versions.value = history.data
}

async function save() {
  if (!changes.value.length) return
  await ElMessageBox.confirm(
    `确认保存 ${changes.value.length} 项修改？新配置用于后续开仓，当前持仓继续使用原参数。`,
    '实时应用配置',
    { type: 'warning', confirmButtonText: '保存并生效' },
  )
  saving.value = true
  try {
    const result = (await api.put('/config', { expected_version: version.value, values: { ...values } })).data
    ElMessage.success(result.pending ? `配置 v${result.version} 已保存，将在平仓后生效` : `配置 v${result.version} 已实时生效`)
    await load()
  } finally {
    saving.value = false
  }
}

function restore(item: any) {
  Object.assign(values, item.values)
  ElMessage.info(`已载入 v${item.version}，保存后将创建新版本`)
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="panel-title">
      <div>
        <h2>运行参数 v{{ version }}</h2>
        <span v-if="state.pending_version" class="negative">引擎仍运行 v{{ state.engine_version }}，v{{ state.pending_version }} 待平仓</span>
        <span v-else>VIX 波动率过滤参数可在线调整；策略与风控参数通过 .env 配置</span>
      </div>
      <div>
        <el-button @click="Object.assign(values, original)">撤销</el-button>
        <el-button type="primary" :disabled="!changes.length" :loading="saving" @click="save">保存 {{ changes.length ? `(${changes.length})` : '' }}</el-button>
      </div>
    </div>
    <el-tabs v-model="active">
      <el-tab-pane v-for="(fields, name) in groups" :key="name" :label="name" :name="name">
        <div class="form-grid">
          <div v-for="field in fields" :key="field.key" class="field">
            <label>{{ field.label }} <small>{{ field.unit }}</small></label>
            <el-switch v-if="field.kind === 'bool'" v-model="values[field.key]" />
            <el-input v-else-if="field.kind === 'text'" v-model="values[field.key]" />
            <el-input-number v-else v-model="values[field.key]" :step="field.step || 1" :min="field.min" :max="field.max" controls-position="right" />
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>

  <div class="panel" style="margin-top: 18px">
    <div class="panel-title"><h2>版本历史</h2><span>不可变审计记录</span></div>
    <el-table :data="versions">
      <el-table-column prop="version" label="版本" width="90"><template #default="scope">v{{ scope.row.version }}</template></el-table-column>
      <el-table-column label="创建时间"><template #default="scope">{{ localTime(scope.row.created_at) }}</template></el-table-column>
      <el-table-column label="状态" width="100"><template #default="scope"><el-tag :type="scope.row.active ? 'success' : 'info'">{{ scope.row.active ? '当前' : '历史' }}</el-tag></template></el-table-column>
      <el-table-column width="120"><template #default="scope"><el-button link type="primary" @click="restore(scope.row)">载入此版本</el-button></template></el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.form-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px 24px;padding:12px 4px 20px}
.field{display:flex;justify-content:space-between;align-items:center;gap:14px;padding:12px;background:#0a1625;border:1px solid #172a40;border-radius:8px}
.field label{font-size:12px;color:#a9bdd5}
.field small{color:#607b9a}
.field .el-input-number,.field .el-input{width:145px}
@media(max-width:1250px){.form-grid{grid-template-columns:repeat(2,1fr)}}
</style>
