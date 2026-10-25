<script setup>
// The bench's per-process logs: a bounded tail, polled with a byte cursor.
//
//   GET /api/v1/instances/<p>/logs?bench=&lines=&process=&after=<proc>:<offset>,...
//
// Polling a resumable tail (every 2s while this pane is mounted) instead of
// holding an exec stream: core/logs.py documents that a streamed `tail -F`
// leaks an orphan per stop because Docker has no kill-exec API.
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { TabButtons, ErrorMessage, LoadingText } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import FollowPane from './FollowPane.vue'
import { getJson } from '../data/api'

const props = defineProps({
  project: { type: String, required: true },
  bench: { type: String, required: true },
  process: { type: String, default: null }, // initial filter
})
const POLL_MS = 2000
// Frappe's dev servers colour their output; show the text, not the escape glyphs.
const ANSI_SGR = /\x1b\[[0-9;]*m/g

// Status labels a worker `worker:<q>`; its log is keyed by the raw program `worker_<q>`.
const filter = ref(props.process ? props.process.replace(/^worker:/, 'worker_') : 'all')
const error = ref(null)
const loading = ref(true)
const notSupervised = ref(false)
// Append-only, so the windowed pane never moves under a paused reader: every
// process's lines in arrival order, and the same { p, text } rows per process.
const logs = {} // process -> { rows: [], offset }
const all = []
const processes = ref([])
const items = shallowRef([])
let timer = null
let inflight = false

const options = computed(() => [{ label: 'All', value: 'all' }, ...processes.value.map((p) => ({ label: p, value: p }))])
const show = () => (items.value = (filter.value === 'all' ? all : logs[filter.value]?.rows || []).slice())
watch(filter, show)

async function poll() {
  if (inflight) return
  inflight = true
  try {
    const q = new URLSearchParams({ bench: props.bench, lines: '200' })
    const after = Object.entries(logs)
      .map(([p, l]) => `${p}:${l.offset}`)
      .join(',')
    if (after) q.set('after', after)
    const data = await getJson(`/api/v1/instances/${encodeURIComponent(props.project)}/logs?${q}`)
    notSupervised.value = data.outcome.not_cwcli_supervised
    for (const t of data.outcome.logs) {
      const entry = logs[t.process] || (logs[t.process] = { rows: [], offset: 0 })
      const gap = t.skipped_bytes ? [`... ${t.skipped_bytes} bytes skipped (written faster than this pane polls) ...`] : []
      for (const text of gap.concat(t.lines.map((l) => l.replace(ANSI_SGR, '')))) {
        const row = { p: t.process, text }
        entry.rows.push(row)
        all.push(row)
      }
      entry.offset = t.offset
    }
    processes.value = Object.keys(logs).sort()
    if (filter.value !== 'all' && !logs[filter.value]) filter.value = 'all'
    show()
    error.value = null
  } catch (e) {
    error.value = e.hint ? `${e.message} ${e.hint}` : e.message
  } finally {
    loading.value = false
    inflight = false
  }
}

onMounted(() => {
  poll()
  timer = setInterval(poll, POLL_MS)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-3">
      <TabButtons v-if="processes.length" v-model="filter" :options="options" />
      <span class="ml-auto text-xs text-ink-gray-5">polled every {{ POLL_MS / 1000 }}s · last 200 lines, then only what is new</span>
    </div>
    <p v-if="notSupervised" class="text-p-sm text-ink-amber-6">Raw bench logs: this bench runs outside cwcli's supervisor.</p>
    <ErrorMessage :message="error" />
    <LoadingText v-if="loading && !processes.length" text="Reading logs..." />
    <div v-else-if="!items.length && !error" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No logs written yet.</div>
    <FollowPane v-else-if="items.length" :items="items" :item-height="20" class="max-h-[60vh] rounded-6 border border-outline-gray-1 bg-surface-gray-1 py-2">
      <template #default="{ item }">
        <div class="flex gap-3 px-3 font-mono text-xs leading-5">
          <span v-if="filter === 'all'" class="w-28 shrink-0 truncate text-ink-gray-5">{{ item.p }}</span>
          <span class="whitespace-pre text-ink-gray-8">{{ item.text }}</span>
        </div>
      </template>
    </FollowPane>
    <CommandLine :command="`cwcli logs ${project} --bench ${bench}${filter !== 'all' ? ' --process ' + filter : ''} -f`" />
  </div>
</template>
