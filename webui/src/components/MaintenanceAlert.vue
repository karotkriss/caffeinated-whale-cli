<script setup>
// Maintenance mode on one bench (core.maintenance, carried on the fleet's per-bench
// health): which sites answer HTTP 503, which write holds them there, who set it,
// and the offer to take them out. One component for the instance, bench and site
// pages and the Sites list, so every page asks the same question in the server's
// own words (`summary`), and none offers the clear while something that sets
// maintenance mode still runs (`busy`) or another job is changing the instance.
import { computed } from 'vue'
import { Alert } from 'frappe-ui'
import { openAction } from '../data/dialog'
import { activeJobs, jobLabel } from '../data/jobs'

const props = defineProps({
  maintenance: { type: Object, default: null }, // BenchStatus.maintenance
  ctx: { type: Object, required: true }, // { project, host, bench, benchName }
  site: { type: String, default: null }, // a site page: shown only while THIS site is down
  where: { type: String, default: '' }, // ' on <bench>' where a page lists several benches
})

const m = computed(() => props.maintenance)
const down = computed(() => m.value?.down || [])
const shown = computed(() => {
  if (!m.value) return false
  if (props.site) return down.value.includes(props.site)
  return down.value.length > 0 || m.value.bench_flags.includes('maintenance_mode')
})
const job = computed(() =>
  activeJobs.value.find((j) => j.target?.project === props.ctx.project && (j.host || null) === (props.ctx.host || null)),
)
const title = computed(() => {
  if (props.site) return `${props.site} is in maintenance mode`
  if (!down.value.length) return `Maintenance mode is set bench-wide${props.where}`
  if (down.value.length === 1) return `${down.value[0]} is in maintenance mode${props.where}`
  return `${down.value.length} sites are in maintenance mode${props.where}`
})
const waiting = computed(() => {
  if (m.value?.busy?.length) return `cwcli does not offer to clear it while it is still in use: ${m.value.busy.join('; ')}.`
  if (job.value) return `${jobLabel(job.value)} is running on this instance; the offer returns when it finishes.`
  return ''
})
const description = computed(() => [m.value?.summary, waiting.value].filter(Boolean).join(' '))
const label = computed(() => {
  if (!down.value.length) return 'Clear the bench-wide flag'
  return down.value.length === 1 ? 'Take it out of maintenance' : `Take ${down.value.length} sites out of maintenance`
})
const action = computed(() =>
  waiting.value ? undefined : { label: label.value, onClick: () => openAction('bench.maintenance_off', { ...props.ctx, maintenance: m.value }) },
)
</script>

<template>
  <Alert v-if="shown" theme="amber" :title="title" :description="description" :primary-action="action" />
</template>
