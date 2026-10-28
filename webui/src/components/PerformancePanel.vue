<script setup>
// Real performance graphs for one instance (and one of its benches when
// `bench` is given): container CPU / memory / network / disk from docker stats,
// per-process CPU and RSS from the health probe, RQ queue depth from Redis.
// Sampled by the daemon at the probe interval, kept for an hour, streamed live.
import { computed, onMounted, watch } from 'vue'
import TimeSeries from './TimeSeries.vue'
import { containerSeries, fmtBytes, fmtCount, fmtPct, fmtRate, latest, loadMetrics, metrics, processSeries, queueSeries } from '../data/metrics'
import { instance } from '../data/fleet'

const props = defineProps({ project: { type: String, required: true }, bench: { type: String, default: null } })
const points = computed(() => metrics.projects[props.project] || [])
const running = computed(() => !!instance(props.project)?.container_running)
const last = computed(() => latest(points.value))
const benches = computed(() => {
  if (props.bench != null) return [props.bench]
  const set = new Set()
  for (const p of points.value) for (const b of Object.keys(p.benches || {})) set.add(b)
  return [...set].sort()
})
const frappeNet = computed(() => {
  const rx = containerSeries(points.value, 'net_rx_bps').find((s) => s.name === 'frappe')
  const tx = containerSeries(points.value, 'net_tx_bps').find((s) => s.name === 'frappe')
  return [rx && { ...rx, name: 'received' }, tx && { ...tx, name: 'sent' }].filter(Boolean)
})
const frappeDisk = computed(() => {
  const r = containerSeries(points.value, 'blk_read_bps').find((s) => s.name === 'frappe')
  const w = containerSeries(points.value, 'blk_write_bps').find((s) => s.name === 'frappe')
  return [r && { ...r, name: 'read' }, w && { ...w, name: 'written' }].filter(Boolean)
})
const since = computed(() => (points.value.length ? Math.round((Date.now() / 1000 - points.value[0].ts) / 60) : 0))
onMounted(() => loadMetrics(props.project))
watch(() => props.project, (p) => loadMetrics(p))
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-gray-5">
      <span v-if="running && last">Measured every probe tick; {{ points.length }} points over the last {{ since }} min.</span>
      <span v-else-if="running">Measuring; the first point lands within a few seconds.</span>
      <span v-else-if="points.length">{{ project }} is stopped: the lines end where it stopped and resume when it starts.</span>
      <span v-else>{{ project }} is stopped and nothing was measured since the daemon started. Nothing is drawn for a stopped instance.</span>
    </div>

    <template v-if="bench == null">
      <div class="grid gap-3 lg:grid-cols-2">
        <TimeSeries title="CPU by container" :series="containerSeries(points, 'cpu_pct')" :format="fmtPct" kind="percent" :settings-key="`instance:${project}:cpu`">
          <template #meta><span v-if="last?.containers?.frappe?.cpu_pct != null" class="text-xs text-ink-gray-5">frappe {{ fmtPct(last.containers.frappe.cpu_pct) }}</span></template>
        </TimeSeries>
        <TimeSeries title="Memory by container" :series="containerSeries(points, 'mem_bytes')" :format="fmtBytes" kind="bytes" stacked :settings-key="`instance:${project}:memory`">
          <template #meta><span v-if="last?.containers?.frappe?.mem_bytes != null" class="text-xs text-ink-gray-5">frappe {{ fmtBytes(last.containers.frappe.mem_bytes) }}</span></template>
        </TimeSeries>
        <TimeSeries title="Network, frappe container" :series="frappeNet" :format="fmtRate" kind="rate" :settings-key="`instance:${project}:network`" />
        <TimeSeries title="Disk I/O, frappe container" :series="frappeDisk" :format="fmtRate" kind="rate" :settings-key="`instance:${project}:disk`" />
      </div>
    </template>

    <div v-for="b in benches" :key="b" class="space-y-3">
      <div v-if="bench == null" class="text-sm text-ink-gray-6">Bench {{ b }}</div>
      <div class="grid gap-3 lg:grid-cols-2" :class="bench != null ? 'xl:grid-cols-3' : ''">
        <TimeSeries title="Process CPU" :series="processSeries(points, b, 'cpu_pct')" :format="fmtPct" kind="percent" :settings-key="`bench:${project}:${b}:cpu`" />
        <TimeSeries title="Process memory (RSS)" :series="processSeries(points, b, 'rss_kb').map((s) => ({ ...s, points: s.points.map(([t, v]) => [t, v == null ? null : v * 1024]) }))" :format="fmtBytes" kind="bytes" :settings-key="`bench:${project}:${b}:rss`" />
        <TimeSeries v-if="bench != null" title="Queue depth (RQ)" :series="queueSeries(points)" :format="fmtCount" kind="count" :settings-key="`bench:${project}:${b}:queues`">
          <template #empty>No queue read yet</template>
        </TimeSeries>
      </div>
    </div>
    <p class="text-xs text-ink-gray-4">Web response time is not measured: the health probe records the HTTP status of one request per tick, not its latency.</p>
  </div>
</template>
