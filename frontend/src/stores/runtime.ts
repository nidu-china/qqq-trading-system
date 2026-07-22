import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../api'

export const useRuntimeStore = defineStore('runtime', () => {
  const status = ref<Record<string, any>>({})
  const online = ref(false)
  let source: EventSource | undefined
  const stateName = computed(() => ({ ready:'就绪', open:'持仓中', halted:'已熔断', starting:'启动中', entry_pending:'入场中', exit_pending:'退出中' } as Record<string,string>)[status.value.state] || status.value.state || '未知')

  async function connect() {
    try { status.value = (await api.get('/status')).data; online.value = true } catch { online.value = false }
    source?.close()
    source = new EventSource('/api/v1/events')
    source.onmessage = event => { status.value = JSON.parse(event.data); online.value = true }
    source.onerror = () => { online.value = false }
  }
  function disconnect() { source?.close(); source = undefined }
  return { status, online, stateName, connect, disconnect }
})
