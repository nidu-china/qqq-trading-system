import axios from 'axios'

export const api = axios.create({ baseURL: '/api/v1', timeout: 20000 })

export function money(value: unknown) {
  const number = Number(value ?? 0)
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'USD' }).format(number)
}

export function percent(value: unknown) {
  return `${(Number(value ?? 0) * 100).toFixed(2)}%`
}

export function localTime(value: string | undefined) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}
