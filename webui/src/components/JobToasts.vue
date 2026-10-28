<script setup>
// The running cards for jobs started from this page (captain note 23:52Z: an
// action's job must not take over the screen). Each card: a spinner, the action
// and target, the current step, elapsed time, View job and Dismiss. A finished
// success dismisses itself after a few seconds; a failure or a needed decision
// stays until dismissed. Steps come from the same job record the job page reads.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button } from 'frappe-ui'
import { tracked, untrackJob } from '../data/job-toasts'
import { ACTIVE_STATES, elapsed, jobLabel, jobTarget, jobs, loadJob } from '../data/jobs'

const router = useRouter()
const SUCCESS_LINGER_MS = 8000
const cards = computed(() => tracked.ids.map((id) => jobs.records[id] || { id, state: 'queued', verb: '', target: {} }))
const now = ref(Date.now())

function currentStep(job) {
  const events = job.events || []
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]
    const type = String(e.type || '')
    if (type.endsWith('_output')) continue
    if (type.endsWith('_step_start') || type.endsWith('_step')) return e.message || e.phase || 'working'
    if (type === 'job_finished' || type === 'job_failed') return e.message || type.replace('_', ' ')
    if (e.message) return e.message
  }
  return job.state === 'queued' ? 'queued' : 'starting'
}
const done = (job) => !ACTIVE_STATES.has(job.state)
const tone = (job) => (job.state === 'succeeded' ? 'ok' : job.state === 'failed' ? 'bad' : job.state === 'needs_choice' || job.state === 'interrupted' || job.state === 'unknown' ? 'warn' : 'run')

let timer = null
async function tick() {
  now.value = Date.now()
  for (const id of [...tracked.ids]) {
    const job = jobs.records[id]
    if (!job || !done(job) || !job.events) {
      try { await loadJob(id, job?.events?.at(-1)?.seq || 0) } catch { /* the next tick retries */ }
    }
    const fresh = jobs.records[id]
    if (fresh?.state === 'succeeded') {
      const finished = Date.parse(fresh.finished_at || '') || tracked.startedAt[id] || 0
      if (Date.now() - finished > SUCCESS_LINGER_MS) untrackJob(id)
    }
  }
}
onMounted(() => (timer = setInterval(tick, 1500)))
onBeforeUnmount(() => clearInterval(timer))
function view(job) {
  untrackJob(job.id)
  router.push(`/jobs/${job.id}`)
}
</script>

<template>
  <Teleport to="body">
  <!-- Bottom right, newest at the bottom. Cards slide in
       from the right edge and leave the same way; the stack closes up with a move
       transition when one goes (TransitionGroup; the classes live in style.css). -->
  <TransitionGroup tag="div" name="jobcard" class="pointer-events-none fixed bottom-16 right-4 z-[9990] flex w-[24rem] max-w-[calc(100vw-2rem)] flex-col gap-2">
    <div v-for="job in cards" :key="job.id" class="jobcard pointer-events-auto flex w-full items-start gap-3 rounded-7 border border-outline-gray-2 bg-surface-elevation-1 p-3 shadow-lg">
      <span class="mt-0.5 size-5 shrink-0" :class="{ 'lucide-loader-circle animate-spin text-ink-blue-6': tone(job) === 'run', 'lucide-circle-check text-ink-green-5': tone(job) === 'ok', 'lucide-circle-x text-ink-red-5': tone(job) === 'bad', 'lucide-circle-alert text-ink-amber-6': tone(job) === 'warn' }" aria-hidden="true" />
      <div class="min-w-0 flex-1">
        <div class="flex items-baseline gap-2">
          <span class="truncate text-base-medium text-ink-gray-9">{{ jobLabel(job) }}</span>
          <span class="truncate text-sm text-ink-gray-6">{{ jobTarget(job) }}</span>
          <span class="ml-auto shrink-0 text-xs tabular-nums text-ink-gray-5">{{ elapsed(job) }}</span>
        </div>
        <div class="mt-0.5 truncate text-sm" :class="tone(job) === 'bad' ? 'text-ink-red-6' : tone(job) === 'warn' ? 'text-ink-amber-6' : 'text-ink-gray-6'">
          {{ done(job) ? job.state.replace('_', ' ') + (job.state === 'needs_choice' ? ': open the job to answer' : '') : currentStep(job) }}
        </div>
        <div class="mt-2 flex items-center gap-1">
          <Button size="sm" :variant="done(job) && tone(job) !== 'ok' ? 'solid' : 'subtle'" label="View job" @click="view(job)" />
          <Button size="sm" variant="ghost" :label="done(job) ? 'Dismiss' : 'Hide'" @click="untrackJob(job.id)" />
        </div>
      </div>
    </div>
  </TransitionGroup>
  </Teleport>
</template>
