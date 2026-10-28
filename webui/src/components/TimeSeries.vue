<script setup>
// One line chart over time (echarts, already a dependency through frappe-ui), with
// a settings menu per chart (captain note 04:51Z): time range, which series show,
// stacked or separate, units, y-axis auto or fixed, pause/resume, CSV and PNG
// download, remembered per browser per chart with a reset. Gaps stay gaps: a null
// value is not connected, so a stopped instance shows nothing for the time it was
// down instead of a flat invented line.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Button, Checkbox, Popover, Select, TabButtons, Tooltip } from 'frappe-ui'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { RANGES, toCsv, useChartSettings } from '../data/chart-settings'

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  title: { type: String, default: '' },
  series: { type: Array, default: () => [] }, // [{ name, points: [[ms, value]] }]
  format: { type: Function, default: (v) => String(v) },
  kind: { type: String, default: 'count' }, // percent | bytes | rate | count
  max: { type: Number, default: null },
  height: { type: String, default: 'h-40' },
  stacked: { type: Boolean, default: false }, // the default; the menu can change it
  stackable: { type: Boolean, default: true },
  settingsKey: { type: String, default: '' }, // remembered per browser when set
})
const el = ref(null)
let chart = null
let observer = null
const { settings, reset, isDefault } = useChartSettings(props.settingsKey, props.kind, { stacked: props.stacked })

// --- settings applied to the data ---
const frozen = ref(null) // the series as they were when paused
watch(() => settings.paused, (p) => (frozen.value = p ? JSON.parse(JSON.stringify(props.series)) : null))
const source = computed(() => (settings.paused && frozen.value ? frozen.value : props.series))
const names = computed(() => source.value.map((s) => s.name))
const unitFactor = computed(() => (props.kind === 'percent' && settings.unit === 'cores' ? 0.01 : 1))
const visible = computed(() => {
  const since = Date.now() - settings.range * 1000
  return source.value
    .filter((s) => !settings.hidden.includes(s.name))
    .map((s) => ({ name: s.name, points: s.points.filter((p) => p[0] >= since).map(([t, v]) => [t, v == null ? null : v * unitFactor.value]) }))
})
const MB = 1024 * 1024
const GB = MB * 1024
const fmt = computed(() => {
  if (props.kind === 'percent' && settings.unit === 'cores') return (v) => (v == null ? '–' : `${Number(v).toFixed(v < 1 ? 2 : 1)} cores`)
  if (props.kind === 'bytes' && settings.unit === 'mb') return (v) => (v == null ? '–' : `${(v / MB).toFixed(v / MB < 10 ? 1 : 0)} MB`)
  if (props.kind === 'bytes' && settings.unit === 'gb') return (v) => (v == null ? '–' : `${(v / GB).toFixed(2)} GB`)
  return props.format
})
const yMax = computed(() => {
  if (props.kind === 'percent' && settings.yFixed) return settings.unit === 'cores' ? 1 : 100
  return props.max ?? undefined
})
const stacked = computed(() => props.stackable && settings.stacked)
const unitOptions = computed(() =>
  props.kind === 'percent'
    ? [{ label: 'Percent', value: 'percent' }, { label: 'Cores', value: 'cores' }]
    : props.kind === 'bytes'
      ? [{ label: 'Auto', value: 'auto' }, { label: 'MB', value: 'mb' }, { label: 'GB', value: 'gb' }]
      : [],
)
const rangeLabel = computed(() => RANGES.find((r) => r.value === settings.range)?.label || `${settings.range}s`)
function toggleSeries(name, on) {
  settings.hidden = on ? settings.hidden.filter((n) => n !== name) : [...new Set([...settings.hidden, name])]
}
function downloadCsv() {
  save(new Blob([toCsv(visible.value)], { type: 'text/csv' }), `${slug()}.csv`)
}
function downloadPng() {
  if (!chart) return
  // The export takes the panel's own surface colour so it follows the theme (and stays inside the token lint).
  const url = chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: getComputedStyle(chart.getDom().parentElement).backgroundColor })
  const a = document.createElement('a')
  a.href = url
  a.download = `${slug()}.png`
  a.click()
}
function save(blob, name) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}
const slug = () => (props.settingsKey || props.title || 'chart').replace(/[^a-z0-9]+/gi, '-').toLowerCase()

// --- rendering ---
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}
function palette() {
  return [cssVar('--ink-blue-5', '#2d7ff9'), cssVar('--ink-green-5', '#1f9d55'), cssVar('--ink-amber-5', '#d97706'), cssVar('--ink-purple-5', '#7c3aed'), cssVar('--ink-red-5', '#dc2626'), cssVar('--ink-cyan-5', '#0891b2'), cssVar('--ink-pink-5', '#db2777'), cssVar('--ink-gray-6', '#6b7280')]
}
const hasData = computed(() => visible.value.some((s) => s.points.some((p) => p[1] != null)))
function render() {
  if (!chart) return
  const gray = cssVar('--ink-gray-5', '#6b7280')
  const line = cssVar('--outline-gray-1', '#e5e7eb')
  const f = fmt.value
  chart.setOption(
    {
      color: palette(),
      animation: false,
      grid: { left: 48, right: 12, top: visible.value.length > 1 ? 28 : 10, bottom: 22 },
      legend: visible.value.length > 1 ? { top: 0, left: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: gray, fontSize: 11 } } : { show: false },
      tooltip: { trigger: 'axis', renderMode: 'richText', valueFormatter: (v) => f(v), axisPointer: { type: 'line', lineStyle: { color: line } }, textStyle: { fontSize: 12 } },
      xAxis: { type: 'time', axisLine: { lineStyle: { color: line } }, axisTick: { show: false }, axisLabel: { color: gray, fontSize: 11, hideOverlap: true }, splitLine: { show: false } },
      yAxis: { type: 'value', min: 0, max: yMax.value, axisLabel: { color: gray, fontSize: 11, formatter: (v) => f(v) }, splitLine: { lineStyle: { color: line } } },
      series: visible.value.map((s) => ({
        id: s.name,
        name: s.name,
        type: 'line',
        data: s.points,
        showSymbol: false,
        connectNulls: false,
        smooth: 0.2,
        lineStyle: { width: 1.5 },
        areaStyle: stacked.value ? { opacity: 0.12 } : undefined,
        stack: stacked.value ? 'total' : undefined,
        emphasis: { disabled: true },
      })),
    },
    { replaceMerge: ['series'], lazyUpdate: true },
  )
}
onMounted(() => {
  chart = echarts.init(el.value, null, { renderer: 'canvas' })
  render()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(el.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
watch([visible, yMax, stacked, fmt], render)
</script>

<template>
  <div class="rounded-6 border border-outline-gray-1 bg-surface-elevation-1 p-3">
    <div class="mb-1 flex items-center gap-2">
      <span v-if="title" class="text-base-medium text-ink-gray-8">{{ title }}</span>
      <slot name="meta" />
      <span class="ml-auto flex items-center gap-1 text-xs text-ink-gray-5">
        <span v-if="settings.paused" class="flex items-center gap-1 text-ink-amber-6"><span class="lucide-pause size-3" aria-hidden="true" />paused</span>
        <span v-if="settings.range !== 900">{{ rangeLabel }}</span>
        <Popover side="bottom" align="end">
          <template #trigger>
            <Button size="xs" variant="ghost" icon="lucide-settings-2" :aria-label="`Settings for ${title || 'this chart'}`" />
          </template>
          <div class="w-64 space-y-3 p-3 text-sm" data-chart-settings @click.stop>
            <div>
              <div class="mb-1 text-xs text-ink-gray-5">Time range</div>
              <TabButtons v-model="settings.range" :options="RANGES" />
            </div>
            <div v-if="names.length > 1">
              <div class="mb-1 text-xs text-ink-gray-5">Series</div>
              <div class="grid grid-cols-2 gap-x-3 gap-y-1">
                <Checkbox v-for="n in names" :key="n" :label="n" :model-value="!settings.hidden.includes(n)" size="sm" @update:model-value="(on) => toggleSeries(n, on)" />
              </div>
            </div>
            <Checkbox v-if="stackable && names.length > 1" v-model="settings.stacked" label="Stacked" size="sm" />
            <div v-if="unitOptions.length" class="flex items-center gap-2">
              <span class="w-16 text-xs text-ink-gray-5">Unit</span>
              <Select v-model="settings.unit" :options="unitOptions" size="sm" class="flex-1" />
            </div>
            <Checkbox v-if="kind === 'percent'" v-model="settings.yFixed" :label="settings.unit === 'cores' ? 'Y axis fixed 0–1 core' : 'Y axis fixed 0–100%'" size="sm" />
            <div class="flex flex-wrap gap-1.5 border-t border-outline-gray-1 pt-2">
              <Button size="sm" :variant="settings.paused ? 'solid' : 'subtle'" :icon-left="settings.paused ? 'lucide-play' : 'lucide-pause'" :label="settings.paused ? 'Resume live' : 'Pause live'" @click="settings.paused = !settings.paused" />
              <Button size="sm" variant="subtle" icon-left="lucide-download" label="CSV" :disabled="!hasData" @click="downloadCsv" />
              <Button size="sm" variant="subtle" icon-left="lucide-image-down" label="PNG" :disabled="!hasData" @click="downloadPng" />
              <Tooltip text="Back to the defaults for this chart"><Button size="sm" variant="ghost" icon-left="lucide-rotate-ccw" label="Reset" :disabled="isDefault()" @click="reset" /></Tooltip>
            </div>
            <p v-if="settingsKey" class="text-xs text-ink-gray-4">Remembered in this browser for this chart.</p>
          </div>
        </Popover>
      </span>
    </div>
    <div class="relative" :class="height">
      <div ref="el" class="absolute inset-0" />
      <div v-if="!hasData" class="absolute inset-0 flex items-center justify-center text-sm text-ink-gray-4"><slot name="empty">No measurements yet</slot></div>
    </div>
  </div>
</template>
