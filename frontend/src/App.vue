<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const collapsed = ref(false)
const title = computed(() => route.meta.title as string)
const menus = [
  ['/', '运行总览', 'OV'],
  ['/signals', '信号对比', 'SG'],
  ['/trades', '交易记录', 'TR'],
  ['/reports', '交易日报', 'RP'],
  ['/backtests', '策略回测', 'BT'],
  ['/configuration', '参数配置', 'CF'],
]
</script>

<template>
  <div class="shell" :class="{ collapsed }">
    <aside>
      <div class="brand"><span class="brand-mark">Q</span><div><strong>QQQ Quant</strong><small>0DTE CONTROL</small></div></div>
      <nav>
        <RouterLink v-for="menu in menus" :key="menu[0]" :to="menu[0]">
          <span class="menu-code">{{ menu[2] }}</span><span>{{ menu[1] }}</span>
        </RouterLink>
      </nav>
      <div class="risk-note"><span class="pulse"></span><div><b>风险控制在线</b><small>默认 Paper 模式</small></div></div>
    </aside>
    <main>
      <header><button class="collapse" @click="collapsed = !collapsed">☰</button><div><span class="eyebrow">QQQ 0DTE AUTOMATION</span><h1>{{ title }}</h1></div><div class="clock">America/New_York</div></header>
      <section class="content"><RouterView /></section>
    </main>
  </div>
</template>
