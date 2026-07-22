import { createApp } from 'vue'
import {
  ElButton, ElDatePicker, ElDrawer, ElInput, ElInputNumber, ElLoading, ElOption,
  ElPagination, ElProgress, ElSelect, ElSwitch, ElTabPane, ElTable, ElTableColumn,
  ElTabs, ElTag, ElTimePicker,
} from 'element-plus'
import { createPinia } from 'pinia'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App).use(createPinia()).use(router).use(ElLoading)
for (const component of [
  ElButton, ElDatePicker, ElDrawer, ElInput, ElInputNumber, ElOption, ElPagination,
  ElProgress, ElSelect, ElSwitch, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag,
  ElTimePicker,
]) app.component(component.name!, component)
app.mount('#app')
