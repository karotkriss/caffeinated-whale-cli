<script setup>
// The bench's per-process logs: a bounded tail, polled with a byte cursor.
//
//   GET /api/v1/instances/<p>/logs?bench=&lines=&process=&after=<proc>:<offset>,...
//
// Polling a resumable tail (every 2s while this pane is mounted) instead of
// holding an exec stream: core/logs.py documents that a streamed `tail -F`
// leaks an orphan per stop because Docker has no kill-exec API.
import { computed, onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import { TabButtons, Checkbox, ErrorMessage, LoadingText } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { getJson } from '../data/api'

const props = defineProps({
  project: { type: String, required: true },
  bench: { type: String, required: true },
  process: { type: String, default: null }, // initial filter
})
const POLL_MS = 2000
const MAX_LINES = 2000

const filter = ref(props.process || 'all')
const follow = ref(true)
const error = ref(null)
const loading = ref(true)
const notSupervised = ref(false)
const logs = ref({}) // process -> { lines: [], offset }
const pane = ref(null)
let timer = null
let inflight = false

const processes = computed(() => Object.keys(logs.value).sort())
const options = computed(() => [{ label: 'All', value: 'all' }, ...processes.value.map((p) => ({ label: p, value: p }))])
const shown = computed(() => (filter.value === 'all' ? processes.value : processes.value.filter((p) => p === filter.value)))

async function poll() {
  if (inflight) return
  inflight = true
  try {
    const q = new URLSearchParams({ bench: props.bench, lines: '200' })
    const after = Object.entries(logs.value)
      .map(([p, l]) => `${p}:${l.offset}`)
      .join(',')
    if (after) q.set('after', after)
    const data = await getJson(`/api/v1/instances/${encodeURIComponent(props.project)}/logs?${q}`)
    notSupervised.value = data.outcome.not_cwcli_supervised
    for (const t of data.outcome.logs) {
      const entry = logs.value[t.process] || (logs.value[t.process] = { lines: [], offset: 0 })
      // A cold read (no cursor yet) replaces; a warm one appends only what is new.
      const previous = entry.offset
      entry.lines = previous ? entry.lines.concat(t.lines) : t.lines
      if (entry.lines.length > MAX_LINES) entry.lines.splice(0, entry.lines.length - MAX_LINES)
      entry.offset = t.offset
    }
    error.value = null
    if (follow.value) nextTick(() => pane.value && (pane.value.scrollTop = pane.value.scrollHeight))
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
watch(filter, () => follow.value && nextTick(() => pane.value && (pane.value.scrollTop = pane.value.scrollHeight)))
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-3">
      <TabButtons v-if="processes.length" v-model="filter" :options="options" />
      <Checkbox v-model="follow" label="Follow" size="sm" />
      <span class="ml-auto text-xs text-ink-gray-5">polled every {{ POLL_MS / 1000 }}s · last 200 lines, then only what is new</span>
    </div>
    <p v-if="notSupervised" class="text-p-sm text-ink-amber-6">Raw bench logs: this bench runs outside cwcli's supervisor.</p>
    <ErrorMessage :message="error" />
    <LoadingText v-if="loading && !processes.length" text="Reading logs..." />
    <div v-else-if="!processes.length && !error" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No logs written yet.</div>
    <div v-else ref="pane" class="max-h-[60vh] overflow-auto rounded-6 border border-outline-gray-1 bg-surface-gray-1">
      <div v-for="p in shown" :key="p" class="border-b border-outline-gray-1 last:border-b-0">
        <div class="sticky top-0 bg-surface-gray-2 px-3 py-1 font-mono text-xs text-ink-gray-6">{{ p }}</div>
        <pre class="whitespace-pre-wrap break-words px-3 py-2 font-mono text-xs leading-5 text-ink-gray-8">{{ logs[p].lines.join('\n') || '(empty)' }}</pre>
      </div>
    </div>
    <CommandLine :command="`cwcli logs ${project} --bench ${bench}${filter !== 'all' ? ' --process ' + filter : ''} -f`" />
  </div>
</template>
