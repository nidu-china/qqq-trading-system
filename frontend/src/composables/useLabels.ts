import { ref } from 'vue'
import { api } from '../api'

const exitReasonLabels = ref<Record<string, string>>({})
const rejectLabels = ref<Record<string, string>>({})
const regimeLabels = ref<Record<string, string>>({})
let loaded = false

async function loadLabels() {
  if (loaded) return
  try {
    const res = await api.get('/labels')
    exitReasonLabels.value = res.data.exit_reasons || {}
    rejectLabels.value = res.data.reject_reasons || {}
    regimeLabels.value = res.data.regimes || {}
    loaded = true
  } catch {}
}

function exitReasonLabel(key: string): string {
  return exitReasonLabels.value[key] || key.replace(/_/g, ' ')
}

function exitReasonType(key: string): string {
  if (key === 'take_profit_1' || key === 'take_profit_2' || key === 'trailing_stop') return 'success'
  if (key === 'stop_loss' || key === 'structure_stop' || key === 'daily_loss') return 'danger'
  return 'info'
}

function rejectLabel(key: string): string {
  return rejectLabels.value[key] || key.replace(/_/g, ' ')
}

function regimeLabel(key: string): string {
  return regimeLabels.value[key] || key
}

function signalReasonLabel(reason: string, status: string): string {
  if (status === 'rejected') return rejectLabel(reason)
  return exitReasonLabel(reason)
}

export function useLabels() {
  return {
    loadLabels,
    exitReasonLabel,
    exitReasonType,
    rejectLabel,
    regimeLabel,
    signalReasonLabel,
    exitReasonLabels,
    rejectLabels,
    regimeLabels,
  }
}
