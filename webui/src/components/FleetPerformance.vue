<script setup>
// The fleet overview: what the running instances use together, and what the host
// has left. Host memory and load come from /proc; instance totals from docker stats.
import { computed, onMounted } from 'vue'
import TimeSeries from './TimeSeries.vue'
import { fmtBytes, fmtPct, hostSeries, loadMetrics, metrics } from '../data/metrics'
import { instances } from '../data/fleet'

const running = computed(() => instances.value.filter((i) => i.container_running).map((i) => i.project))
// Summed per sampler tick (every instance measured in one pass carries the same
// tick), so a total never mixes one instance's point with another's from the next pass.
function totals(field) {
  const byTick = new Map()
  for (const p of running.value) {
    for (const point of metrics.projects[p] || []) {
      const v = Object.values(point.containers || {}).reduce((t, c) => (c[field] == null ? t : t + c[field]), null)
      if (v == null || point.tick == null) continue
      const cur = byTick.get(point.tick) || { ts: point.ts, v: 0 }
      cur.v += v
      byTick.set(point.tick, cur)
    }
  }
  return [{ name: 'running instances', points: [...byTick.values()].sort((a, b) => a.ts - b.ts).map((e) => [e.ts * 1000, e.v]) }]
}
const cpu = computed(() => totals('cpu_pct'))
const mem = computed(() => totals('mem_bytes'))
const host = computed(() => hostSeries(metrics.host))
const lastHost = computed(() => metrics.host[metrics.host.length - 1] || null)
onMounted(() => loadMetrics())
</script>

<template>
  <section class="space-y-3">
    <div class="flex items-baseline gap-2">
      <h2 class="text-lg-semibold text-ink-gray-9">Performance</h2>
      <span class="text-xs text-ink-gray-5">{{ running.length }} running {{ running.length === 1 ? 'instance' : 'instances' }}<template v-if="lastHost?.mem_total"> · host {{ fmtBytes(lastHost.mem_available) }} of {{ fmtBytes(lastHost.mem_total) }} free<template v-if="lastHost.load1 != null">, load {{ lastHost.load1 }} on {{ lastHost.cpus }} CPUs</template></template></span>
    </div>
    <div class="grid gap-3 sm:grid-cols-3">
      <TimeSeries title="CPU, all running instances" :series="cpu" :format="fmtPct" kind="percent" :stackable="false" settings-key="fleet:cpu" height="h-28"><template #empty>No running instance</template></TimeSeries>
      <TimeSeries title="Memory, all running instances" :series="mem" :format="fmtBytes" kind="bytes" :stackable="false" settings-key="fleet:memory" height="h-28"><template #empty>No running instance</template></TimeSeries>
      <TimeSeries title="Host memory available" :series="host.memAvailable" :format="fmtBytes" kind="bytes" :max="host.memTotal" :stackable="false" settings-key="fleet:host-memory" height="h-28" />
    </div>
  </section>
</template>
