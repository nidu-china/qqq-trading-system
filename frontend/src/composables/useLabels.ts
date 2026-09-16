import { ref } from 'vue'
import { api } from '../api'

const entryReasonLabels = ref<Record<string, string>>({})
const exitReasonLabels = ref<Record<string, string>>({})
const rejectLabels = ref<Record<string, string>>({})
const regimeLabels = ref<Record<string, string>>({})
let loaded = false

async function loadLabels() {
  if (loaded) return
  try {
    const res = await api.get('/labels')
    entryReasonLabels.value = res.data.entry_reasons || {}
    exitReasonLabels.value = res.data.exit_reasons || {}
    rejectLabels.value = res.data.reject_reasons || {}
    regimeLabels.value = res.data.regimes || {}
    loaded = true
  } catch {}
}

function entryReasonLabel(key: string): string {
  return entryReasonLabels.value[key] || ''
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
  if (rejectLabels.value[key]) return rejectLabels.value[key]
  const colonIdx = key.indexOf(':')
  if (colonIdx > 0) {
    const base = key.substring(0, colonIdx)
    const details = key.substring(colonIdx + 1)
    const label = rejectLabels.value[base] || base.replace(/_/g, ' ')
    return `${label} (${details})`
  }
  for (const [prefix, label] of Object.entries(rejectLabels.value)) {
    if (key.startsWith(`${prefix}_`) && prefix.startsWith('volatility_')) {
      return label
    }
  }
  return key.replace(/_/g, ' ')
}

function regimeLabel(key: string): string {
  return regimeLabels.value[key] || key
}

function signalReasonLabel(reason: string, status: string, action?: string): string {
  if (status === 'rejected') return rejectLabel(reason)
  if (rejectLabels.value[reason]) return rejectLabels.value[reason]
  const entry = entryReasonLabel(reason)
  if (entry) return entry
  if (reason.startsWith('entry_')) {
    const bare = reason.slice('entry_'.length)
    const fromBare = entryReasonLabel(bare)
    if (fromBare) return fromBare
  }
  if (action === 'sell') return exitReasonLabel(reason)
  if (exitReasonLabels.value[reason]) return exitReasonLabel(reason)
  return reason.replace(/_/g, ' ')
}

export function useLabels() {
  return {
    loadLabels,
    entryReasonLabel,
    exitReasonLabel,
    exitReasonType,
    rejectLabel,
    regimeLabel,
    signalReasonLabel,
    entryReasonLabels,
    exitReasonLabels,
    rejectLabels,
    regimeLabels,
  }
}
