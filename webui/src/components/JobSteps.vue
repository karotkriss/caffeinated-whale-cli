<script setup>
// A job's event log as collapsible steps (Pilot's TaskSteps): one row per phase,
// with its state, its own duration, and the output that belongs to it. Running
// and failed steps open themselves; finished steps settle closed unless opened.
// Built from the phase/action and *_step_start/*_step_end events the daemon
// already records, so the job protocol is untouched.
import { computed, reactive } from 'vue'
import { Button, toast } from 'frappe-ui'
import { feed, openLine } from '../data/follow'

const props = defineProps({
  events: { type: Array, required: true },
  state: { type: String, default: '' }, // the job state, to mark the last step as running
})
const opened = reactive({}) // step key -> true/false, the reader's own choice

const steps = computed(() => {
  const out = []
  let current = null
  const stepFor = (event) => event.phase || event.action || null
  for (const event of props.events) {
    const type = String(event.type || '')
    const key = stepFor(event)
    // Verbs without phases (rm, rm-site, rm-bench) announce each step as a `*_step`
    // event and narrate inside it with `*_trace` / `*_notice` / `*_warning` lines.
    if (type.endsWith('_step') && !type.endsWith('_step_start')) {
      current = { key: `${type}-${event.seq}`, title: event.message || titleOf(type), started: event.at, ended: null, ok: null, status: null, lines: [], term: { lines: [], open: '' }, events: [] }
      out.push(current)
      continue
    }
    if (/_(trace|notice|warning)$/.test(type) && current) {
      current.lines.push(event.message || event.data?.text || type)
      current.events.push(event)
      continue
    }
    if (type.endsWith('_step_start') || (key && (!current || current.key !== key) && !type.endsWith('_output'))) {
      if (!current || current.key !== key || type.endsWith('_step_start')) {
        current = { key: key || type, title: event.message || titleOf(key || type), started: event.at, ended: null, ok: null, status: null, lines: [], term: { lines: [], open: '' }, events: [] }
        out.push(current)
      }
    }
    if (!current) {
      current = { key: 'start', title: 'Started', started: event.at, ended: null, ok: null, status: null, lines: [], term: { lines: [], open: '' }, events: [] }
      out.push(current)
    }
    if (type.endsWith('_output')) {
      feed(current.term, event.data?.text ?? event.message ?? '')
      continue
    }
    current.events.push(event)
    if (type.endsWith('_step_end') || type === 'job_finished' || type.endsWith('_done')) {
      current.ended = event.at
      current.ok = event.ok !== false && !(event.status && event.status !== 'ok' && event.status !== 'warning')
      current.status = event.status || (event.ok === false ? 'failed' : 'ok')
      if (event.message) current.title = event.message
      current = null
    }
  }
  // A step with no explicit end event ended when its successor started (init records
  // no per-phase step_end), so only the LAST open step can still be running.
  for (let i = 0; i < out.length - 1; i++) {
    if (out[i].ended == null) { out[i].ended = out[i + 1].started; out[i].ok = out[i].ok ?? true }
  }
  return out.map((s, i) => {
    const last = i === out.length - 1
    const running = s.ended == null && ['queued', 'running', 'interrupting'].includes(props.state) && last
    const failed = s.ok === false || (s.ended == null && props.state === 'failed' && last)
    const lines = s.lines.concat(s.term.lines, s.term.open ? [openLine(s.term)] : [])
    return { ...s, running, failed, lines, duration: durationOf(s.started, s.ended) }
  })
})

function titleOf(key) {
  const name = String(key || '').replaceAll(/[-_]/g, ' ')
  return name.charAt(0).toUpperCase() + name.slice(1)
}
function durationOf(a, b) {
  const start = Date.parse(a || '')
  const end = Date.parse(b || '')
  if (!Number.isFinite(start) || !Number.isFinite(end)) return ''
  const s = Math.max(0, Math.round((end - start) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}
const isOpen = (s) => (s.key in opened ? opened[s.key] : s.running || s.failed)
const toggle = (s) => (opened[s.key] = !isOpen(s))
async function copy(s) {
  try {
    await navigator.clipboard.writeText([...s.events.map((e) => e.message || e.type), ...s.lines].join('\n'))
    toast.success('Step output copied', { id: 'copy' })
  } catch {
    toast.error('Copy failed', { id: 'copy' })
  }
}
const ICON = (s) => (s.running ? 'lucide-loader-circle animate-spin text-ink-blue-6' : s.failed ? 'lucide-circle-x text-ink-red-5' : s.ended ? 'lucide-circle-check text-ink-green-5' : 'lucide-circle-dashed text-ink-gray-4')
</script>

<template>
  <div class="divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
    <div v-for="s in steps" :key="s.key + s.started">
      <button class="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-surface-gray-1" @click="toggle(s)">
        <span class="size-4 shrink-0" :class="ICON(s)" aria-hidden="true" />
        <span class="min-w-0 flex-1 truncate text-base text-ink-gray-8">{{ s.title }}</span>
        <span v-if="s.lines.length" class="text-xs text-ink-gray-4">{{ s.lines.length }} lines</span>
        <span class="w-14 text-right text-xs tabular-nums text-ink-gray-5">{{ s.duration }}</span>
        <span class="size-4 shrink-0 text-ink-gray-5 transition-transform" :class="['lucide-chevron-down', isOpen(s) && 'rotate-180']" aria-hidden="true" />
      </button>
      <div v-if="isOpen(s)" class="border-t border-outline-gray-1 bg-surface-gray-1 px-3 py-2">
        <div class="flex items-center justify-end"><Button size="xs" variant="ghost" icon-left="lucide-copy" label="Copy" @click="copy(s)" /></div>
        <div v-for="(e, i) in s.events.filter((e) => !/_(trace|notice|warning)$/.test(e.type))" :key="i" class="font-mono text-xs leading-5 text-ink-gray-6">{{ e.message || e.data?.summary || e.type }}<span v-if="e.item || e.data?.item" class="text-ink-gray-5"> · {{ e.item || e.data?.item }}</span></div>
        <pre v-if="s.lines.length" class="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-5 text-ink-gray-7">{{ s.lines.join('\n') }}</pre>
      </div>
    </div>
    <div v-if="!steps.length" class="px-3 py-6 text-center text-p-sm text-ink-gray-5">Waiting for the first event...</div>
  </div>
</template>
