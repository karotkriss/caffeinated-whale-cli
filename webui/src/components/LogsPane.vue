<script setup>
// The bench's per-process logs: a bounded tail, polled with a byte cursor.
//
//   GET /api/v1/instances/<p>/logs?bench=&lines=&process=&after=<proc>:<offset>,...
//
// Polling a resumable tail (every 2s while this pane is mounted) instead of
// holding an exec stream: core/logs.py documents that a streamed `tail -F`
// leaks an orphan per stop because Docker has no kill-exec API.
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { TabButtons, ErrorMessage, LoadingText, TextInput, Button } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import FollowPane from './FollowPane.vue'
import { apiBase, getJson } from '../data/api'

const props = defineProps({
  project: { type: String, required: true },
  host: { type: String, default: null }, // a remote host's instance: read through the hub
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
const search = ref('')
const shown = computed(() => {
  const q = search.value.trim().toLowerCase()
  return q ? items.value.filter((r) => r.text.toLowerCase().includes(q)) : items.value
})
function download() {
  const blob = new Blob([shown.value.map((r) => (filter.value === 'all' ? `${r.p}\t${r.text}` : r.text)).join('\n') + '\n'], { type: 'text/plain' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.project}-bench${props.bench}-${filter.value}.log`
  a.click()
  URL.revokeObjectURL(a.href)
}
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
    const data = await getJson(`${apiBase(props.host)}/instances/${encodeURIComponent(props.project)}/logs?${q}`)
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
      <TextInput v-model="search" class="w-56" type="search" placeholder="Search these lines">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <span v-if="search.trim()" class="text-xs tabular-nums text-ink-gray-5">{{ shown.length }} {{ shown.length === 1 ? 'match' : 'matches' }}</span>
      <Button size="sm" variant="ghost" icon-left="lucide-download" label="Download" :disabled="!shown.length" @click="download" />
      <span class="ml-auto text-xs text-ink-gray-5">polled every {{ POLL_MS / 1000 }}s · last 200 lines, then only what is new</span>
    </div>
    <p v-if="notSupervised" class="text-p-sm text-ink-amber-6">Raw bench logs: this bench runs outside cwcli's supervisor.</p>
    <ErrorMessage :message="error" />
    <LoadingText v-if="loading && !processes.length" text="Reading logs..." />
    <div v-else-if="!items.length && !error" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No logs written yet.</div>
    <div v-else-if="items.length && !shown.length" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No line matches "{{ search }}".</div>
    <FollowPane v-else-if="items.length" :items="shown" class="max-h-[60vh] rounded-6 border border-outline-gray-1 bg-surface-gray-1 py-2">
      <template #default="{ item }">
        <div class="flex gap-3 px-3 font-mono text-xs leading-5">
          <span v-if="filter === 'all'" class="w-28 shrink-0 break-words text-ink-gray-5">{{ item.p }}</span>
          <span class="min-h-5 min-w-0 whitespace-pre-wrap break-words text-ink-gray-8">{{ item.text }}</span>
        </div>
      </template>
    </FollowPane>
    <CommandLine :command="`cwcli logs ${project} --bench ${bench}${filter !== 'all' ? ' --process ' + filter : ''} -f`" />
  </div>
</template>
