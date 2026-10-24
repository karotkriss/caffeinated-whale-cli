<script setup>
import { Button } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import StatusBadge from './StatusBadge.vue'
import { duration } from '../data/fleet'
import { openAction } from '../data/dialog'

const props = defineProps({
  bench: { type: Object, required: true }, // the live BenchStatus from the fleet
  project: { type: String, required: true },
})
const emit = defineEmits(['logs'])
const mem = (kb) => (kb == null ? 'unknown' : `${Math.round(kb / 1024)} MB`)
const ctx = (p) => ({ project: props.project, bench: String(props.bench.index), process: p.label })
</script>

<template>
  <div v-if="!bench.processes?.length" class="px-3 py-8 text-center text-p-sm text-ink-gray-5">
    <template v-if="bench.overall === 'online'">Supervisor not started on this bench. Nothing is running.</template>
    <template v-else-if="bench.not_cwcli_supervised">Running outside cwcli's supervisor; per-process state is not available.</template>
    <template v-else>No process has been probed on this bench yet.</template>
  </div>
  <List
    v-else
    class="-mx-3 list-row-px-3"
    :columns="{ base: ['minmax(0,1fr)', '6.5rem', '4.5rem'], md: ['minmax(0,1fr)', '6.5rem', '5rem', '6rem', '4rem', '5.5rem', '4.5rem'] }"
    :row-height="40"
  >
    <ListHeader>
      <ListHeaderCell>Process</ListHeaderCell>
      <ListHeaderCell>State</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">PID</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Uptime</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">CPU</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Memory</ListHeaderCell>
      <ListHeaderCell />
    </ListHeader>
    <ListRows :items="bench.processes" row-key="label" v-slot="{ item }">
      <ListRow>
        <ListCell><span class="font-mono text-sm text-ink-gray-8">{{ item.label }}</span></ListCell>
        <ListCell><StatusBadge kind="process" :token="item.state || (item.up ? 'up' : 'down')" /></ListCell>
        <ListCell class="max-md:hidden"><span class="tabular-nums text-ink-gray-6">{{ item.pid ?? 'none' }}</span></ListCell>
        <ListCell class="max-md:hidden"><span class="tabular-nums text-ink-gray-6">{{ item.up ? duration(item.uptime_s) : 'down' }}</span></ListCell>
        <ListCell class="max-md:hidden"><span class="tabular-nums text-ink-gray-6">{{ item.cpu_pct == null ? 'unknown' : item.cpu_pct.toFixed(1) + '%' }}</span></ListCell>
        <ListCell class="max-md:hidden"><span class="tabular-nums text-ink-gray-6">{{ mem(item.rss_kb) }}</span></ListCell>
        <ListCell class="justify-end gap-0.5">
          <Button size="xs" variant="ghost" icon="lucide-scroll-text" aria-label="Logs" @click.stop="emit('logs', item.label)" />
          <Button v-if="!bench.not_cwcli_supervised" size="xs" variant="ghost" icon="lucide-rotate-cw" aria-label="Restart process" @click.stop="openAction('process.restart', ctx(item))" />
        </ListCell>
      </ListRow>
    </ListRows>
  </List>
</template>
