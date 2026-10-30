<script setup>
// One site's logs (captain note 04:41Z): its own Frappe log files under
// sites/<site>/logs (resumable tail, polled every 2 s, follow mode, search with a
// match count, Download), its Error Log entries newest first with the traceback
// on expand, and an honest word on web access lines. A stopped instance reads
// the files from the bench on this host's disk; the Error Log needs the database.
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { Button, ErrorMessage, LoadingText, TabButtons, TextInput } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import FollowPane from './FollowPane.vue'
import { apiBase, getJson } from '../data/api'
import { benchPath } from '../data/fleet'

const props = defineProps({
  project: { type: String, required: true },
  host: { type: String, default: null }, // a remote host's site: read through the hub
  bench: { type: String, required: true },
  benchPath: { type: String, required: true },
  site: { type: String, required: true },
  running: { type: Boolean, default: false },
})
const POLL_MS = 2000
const files = ref([])
const source = ref(null)
const view = ref(null) // 'all' | a file name | 'error-log' | 'web'
const error = ref(null)
const loading = ref(true)
const search = ref('')

// --- file tail (append-only rows, byte cursor; 'all' keeps one cursor per live file) ---
let offset = null
let offsets = {}
const rows = []
const items = shallowRef([])
const shown = computed(() => {
  const q = search.value.trim().toLowerCase()
  return q ? items.value.filter((r) => r.text.toLowerCase().includes(q)) : items.value
})
let timer = null
let inflight = false
const base = () => `${apiBase(props.host)}/instances/${encodeURIComponent(props.project)}`
const params = () => new URLSearchParams({ bench_path: props.benchPath, site: props.site })

async function poll() {
  if (inflight) return
  inflight = true
  try {
    const q = params()
    const all = view.value === 'all'
    const file = view.value && !all && view.value !== 'error-log' && view.value !== 'web' ? view.value : null
    if (all) {
      q.set('file', '*')
      q.set('lines', Object.keys(offsets).length ? '200' : '60')
      q.set('after', JSON.stringify(offsets))
    } else if (file) {
      q.set('file', file)
      q.set('lines', '200')
      if (offset != null) q.set('after', String(offset))
    }
    const data = await getJson(`${base()}/site-logs?${q}`)
    files.value = data.files
    source.value = data.source
    if (view.value == null) view.value = pickDefault(data.files)
    if (all && data.tails) {
      // Captain note 39: All is the default. Each live file's new lines arrive as a block,
      // tagged with the file, in file order; rotated copies stay under their own tabs.
      for (const name of Object.keys(data.tails).sort()) {
        const tail = data.tails[name]
        const tag = name.replace(/\.log$/, '')
        const gap = tail.skipped_bytes ? [`... ${tail.skipped_bytes} bytes skipped (written faster than this pane polls) ...`] : []
        for (const text of gap.concat(tail.lines)) rows.push({ text, tag })
        offsets[name] = tail.offset
      }
      items.value = rows.slice()
    }
    if (file && data.tail) {
      const gap = data.tail.skipped_bytes ? [`... ${data.tail.skipped_bytes} bytes skipped (written faster than this pane polls) ...`] : []
      for (const text of gap.concat(data.tail.lines)) rows.push({ text })
      offset = data.tail.offset
      items.value = rows.slice()
    }
    error.value = null
  } catch (e) {
    error.value = e.hint ? `${e.message} ${e.hint}` : e.message
  } finally {
    loading.value = false
    inflight = false
  }
}
function pickDefault(list) {
  return list.length ? 'all' : 'error-log'
}
function resetTail() {
  offset = null
  offsets = {}
  rows.length = 0
  items.value = []
  search.value = ''
}
watch(view, (v) => {
  resetTail()
  if (v === 'error-log') loadErrors()
  else if (v !== 'web') poll()
})

// --- Error Log ---
const errors = ref({ loading: false, entries: null, error: null })
const expanded = ref({})
async function loadErrors() {
  errors.value = { loading: true, entries: null, error: null }
  try {
    const data = await getJson(`${base()}/error-log?${params()}&limit=100`)
    errors.value = { loading: false, entries: data.entries, error: null }
  } catch (e) {
    errors.value = { loading: false, entries: null, error: e }
  }
}
const shownErrors = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = errors.value.entries || []
  return q ? list.filter((e) => `${e.method} ${e.error}`.toLowerCase().includes(q)) : list
})

const liveFiles = computed(() => files.value.filter((f) => !/\.log\.\d+$/.test(f.name)))
const options = computed(() => [
  ...(files.value.length ? [{ label: 'All', value: 'all' }] : []),
  ...files.value.map((f) => ({ label: f.name.replace(/\.log$/, ''), value: f.name })),
  { label: 'Error Log', value: 'error-log' },
  { label: 'Web access', value: 'web' },
])
const current = computed(() => files.value.find((f) => f.name === view.value))
function download() {
  const text = view.value === 'error-log' ? shownErrors.value.map((e) => `${e.creation}\t${e.method}\n${e.error}\n`).join('\n') : shown.value.map((r) => r.text).join('\n') + '\n'
  const blob = new Blob([text], { type: 'text/plain' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.project}-${props.site}-${view.value === 'error-log' ? 'error-log.txt' : view.value === 'all' ? 'all-logs.txt' : view.value}`
  a.click()
  URL.revokeObjectURL(a.href)
}
const fmtSize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`)

onMounted(() => {
  poll()
  timer = setInterval(() => { if (view.value && view.value !== 'error-log' && view.value !== 'web') poll() }, POLL_MS)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-3">
      <TabButtons v-if="options.length > 2 || files.length" v-model="view" :options="options" />
      <TextInput v-if="view !== 'web'" v-model="search" class="w-56" type="search" :placeholder="view === 'error-log' ? 'Search method or traceback' : 'Search these lines'">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <span v-if="search.trim() && view !== 'web'" class="text-xs tabular-nums text-ink-gray-5">{{ view === 'error-log' ? shownErrors.length : shown.length }} {{ (view === 'error-log' ? shownErrors.length : shown.length) === 1 ? 'match' : 'matches' }}</span>
      <Button v-if="view !== 'web'" size="sm" variant="ghost" icon-left="lucide-download" label="Download" :disabled="view === 'error-log' ? !shownErrors.length : !shown.length" @click="download" />
      <Button v-if="view === 'error-log'" size="sm" variant="ghost" icon-left="lucide-refresh-cw" label="Refresh" :loading="errors.loading" @click="loadErrors" />
      <span class="ml-auto text-xs text-ink-gray-5">
        <template v-if="view === 'error-log'">{{ running ? 'read from the site database, newest first, up to 100' : 'needs the instance running' }}</template>
        <template v-else-if="view === 'web'" />
        <template v-else-if="view === 'all'">{{ liveFiles.length }} live {{ liveFiles.length === 1 ? 'file' : 'files' }} together, newest lines grouped by file · {{ source === 'host' ? 'read from the bench on this disk (instance stopped)' : `polled every ${POLL_MS / 1000}s` }} · rotated copies under their own tabs</template>
        <template v-else>{{ source === 'host' ? 'read from the bench on this disk (instance stopped)' : `polled every ${POLL_MS / 1000}s · last 200 lines, then only what is new` }}<template v-if="current"> · {{ fmtSize(current.size) }}</template></template>
      </span>
    </div>
    <ErrorMessage :message="error" />

    <template v-if="view === 'error-log'">
      <LoadingText v-if="errors.loading" text="Reading the Error Log..." />
      <ErrorMessage v-else-if="errors.error" :message="errors.error.hint ? `${errors.error.message} ${errors.error.hint}` : errors.error.message" />
      <div v-else-if="errors.entries && !errors.entries.length" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No Error Log entries on {{ site }}.</div>
      <div v-else-if="errors.entries && !shownErrors.length" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No entry matches "{{ search }}".</div>
      <div v-else-if="errors.entries" class="divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
        <div v-for="e in shownErrors" :key="e.name" class="px-3 py-2">
          <button type="button" class="flex w-full items-start gap-3 text-left" :aria-expanded="!!expanded[e.name]" @click="expanded[e.name] = !expanded[e.name]">
            <span class="mt-0.5 shrink-0 text-ink-gray-4" :class="expanded[e.name] ? 'lucide-chevron-down' : 'lucide-chevron-right'" aria-hidden="true" />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-base text-ink-gray-8">{{ e.method || '(no method)' }}</span>
              <span class="block truncate font-mono text-xs text-ink-gray-5">{{ e.creation }} · {{ e.name }}<template v-if="!expanded[e.name]"> · {{ (e.error.trim().split('\n').pop() || '').slice(0, 160) }}</template></span>
            </span>
          </button>
          <pre v-if="expanded[e.name]" class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-5 bg-surface-gray-1 p-3 font-mono text-xs leading-5 text-ink-gray-8">{{ e.error }}</pre>
        </div>
      </div>
      <CommandLine :command="`cwcli run ${project} --site ${site} mariadb -e 'select creation, method from \`tabError Log\` order by creation desc limit 20'`" />
    </template>

    <template v-else-if="view === 'web'">
      <div class="rounded-6 border border-outline-gray-1 bg-surface-gray-1 px-4 py-6 text-p-sm text-ink-gray-6">
        <p>A development bench serves through Werkzeug, whose access lines carry no Host, so a request cannot be attributed to one site here. The bench's web log holds every site's requests together.</p>
        <div class="mt-3"><Button variant="subtle" icon-left="lucide-scroll-text" label="Open the bench web log" @click="$router.push(`${benchPath({ project, host }, bench)}?tab=logs&process=web`)" /></div>
      </div>
    </template>

    <template v-else>
      <LoadingText v-if="loading && !files.length" text="Reading the site's log files..." />
      <div v-else-if="!files.length && !error" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">{{ site }} has written no log file yet (Frappe creates sites/{{ site }}/logs/*.log on first use).</div>
      <div v-else-if="items.length && !shown.length" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">No line matches "{{ search }}".</div>
      <div v-else-if="!items.length && view && !loading" class="px-3 py-10 text-center text-p-sm text-ink-gray-5">{{ view === 'all' ? 'The live log files are empty.' : `${view} is empty.` }}</div>
      <FollowPane v-else-if="items.length" :items="shown" class="max-h-[60vh] rounded-6 border border-outline-gray-1 bg-surface-gray-1 py-2">
        <template #default="{ item }">
          <div class="flex gap-2 px-3 font-mono text-xs leading-5"><span v-if="item.tag" class="w-24 shrink-0 truncate text-ink-gray-4" :title="item.tag">{{ item.tag }}</span><span class="min-h-5 min-w-0 whitespace-pre-wrap break-words text-ink-gray-8">{{ item.text }}</span></div>
        </template>
      </FollowPane>
      <CommandLine v-if="view" :command="`cwcli run ${project} --site ${site} tail -f sites/${site}/logs/${view === 'all' ? '*.log' : view}`" />
    </template>
  </div>
</template>
