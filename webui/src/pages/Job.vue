<script setup>
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Badge, Breadcrumbs, Button, ErrorMessage, FormControl, PageHeader, LoadingText } from 'frappe-ui'
import { ACTIVE_STATES, JOB_THEME, createJob, detachJob, elapsed, interruptJob, jobLabel, jobTarget, jobs, loadJob, retryJob } from '../data/jobs'
import { jobDialog, openJob } from '../data/job-dialog'
import { setFocus } from '../data/fleet'
import FollowPane from '../components/FollowPane.vue'
import { feed, openLine } from '../data/follow'

const route = useRoute()
const router = useRouter()
const id = String(route.params.id)
const loading = ref(false)
const actionError = ref(null)
const record = computed(() => jobs.records[id])
const active = computed(() => ACTIVE_STATES.has(record.value?.state))
const preflight = computed(() => record.value?.events?.find((event) => event.type === 'update_preflight')?.data)
const warnings = computed(() => record.value?.warnings || [])
// A failed job leads with its plain cause (MariaDB rejected the password, git could
// not authenticate, ...) when cwcli recognized one in the failing step's own output;
// otherwise it says so and points at that output rather than guessing.
const failure = computed(() => {
  const job = record.value
  if (job?.state !== 'failed') return null
  const cause = job.cause
  const error = job.error
  const message = error ? `${error.message}${error.hint ? ` ${error.hint}` : ''}` : ''
  const unrecognized = 'cwcli did not recognize the cause; the failing step\'s output is in the event log below.'
  if (!cause) {
    if (error && !error.detail?.output) return { title: error.code || 'Job failed', description: message }
    return { title: error?.code || 'Job failed', description: message ? `${message} ${unrecognized}` : unrecognized }
  }
  return { title: cause.title, description: !message ? cause.text : message.includes(cause.text) ? message : `${cause.text} ${message}` }
})
const time = (value) => value ? new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(new Date(value)) : ''
const eventTitle = (event) => {
  if (event.message) return event.message
  if (event.data?.summary) return event.data.summary
  const step = event.phase || event.action
  if (event.type.endsWith('_step_end') && step) {
    const name = step.replaceAll(/[-_]/g, ' ')
    const outcome = event.ok === false ? 'failed' : event.status && event.status !== 'ok' ? event.status : 'finished'
    return `${name.charAt(0).toUpperCase()}${name.slice(1)} ${outcome}`
  }
  return step || event.type.replaceAll('_', ' ')
}
// The event log as one append-only list of rows for the windowed pane: an event,
// or one line of an output block. Consecutive output chunks (the command's own
// bytes) form one terminal block, and each refresh feeds it only the new chunks.
const items = shallowRef([])
let seen = 0
let block = null
watch(() => record.value?.events, (events = []) => {
  let start = events.length
  while (start > 0 && events[start - 1].seq > seen) start--
  if (start === events.length) return
  const added = []
  for (const event of events.slice(start)) {
    seen = event.seq
    if (!event.type.endsWith('_output')) {
      block = null
      added.push({ event })
      continue
    }
    if (!block) block = { at: event.at, lines: [], open: '', rows: 0 }
    feed(block, event.data?.text ?? event.message ?? '')
    while (block.rows < block.lines.length + (block.open ? 1 : 0)) added.push({ block, line: block.rows++ })
  }
  items.value = items.value.concat(added)
}, { immediate: true })
const eventItem = (event) => event.item || event.data?.item
const lineText = ({ block, line }) => (line < block.lines.length ? block.lines[line] : openLine(block))
const isLast = (index) => index === items.value.length - 1
const firstLine = (item) => item.line === 0
const lastLine = (item) => item.line === item.block.rows - 1

async function refresh(after = 0) {
  loading.value = true
  try { await loadJob(id, after) } finally { loading.value = false }
}

onMounted(() => {
  setFocus(record.value?.target?.project || null)
  refresh()
})
watch(() => record.value?.event_count, (count, previous) => {
  if (previous != null && count > previous) refresh(record.value?.events?.at(-1)?.seq || 0)
})

async function interrupt() {
  actionError.value = null
  try { await interruptJob(id) } catch (error) { actionError.value = error }
}

async function detach() {
  actionError.value = null
  try {
    await detachJob(id)
    await router.push('/jobs')
  } catch (error) { actionError.value = error }
}

// Retry starts a NEW job from the same inputs; the daemon reuses the passwords it
// still holds in memory. When it no longer holds them (it restarted), the wizard
// reopens on its last step with everything else filled in.
const retrying = ref(false)
async function retry() {
  const job = record.value
  actionError.value = null
  retrying.value = true
  try {
    const next = await retryJob(id)
    await router.push(`/jobs/${next.id}`)
  } catch (error) {
    if (error.code === 'job.retry_secrets_missing' && job.verb === 'init') {
      openJob('init', { ...job.target, args: job.args })
      jobDialog.step = 2
    } else actionError.value = error
  } finally { retrying.value = false }
}

const benchOptions = computed(() => record.value?.choice?.param === 'bench' ? record.value.choice.options || [] : [])
const consentNeeded = computed(() => record.value?.choice?.param === 'consent')
// The name typed back is the most specific thing the job acts on: a dropped
// site's name, else the instance's (scale, rm, rm-bench).
const consentTarget = computed(() => record.value?.target?.site || record.value?.target?.project)
const typedName = ref('')
const consentTyped = computed(() => !!consentTarget.value && typedName.value.trim() === consentTarget.value)

async function continueChoice(value) {
  const job = record.value
  const param = job.choice?.param
  if (job.verb === 'init') {
    openJob('init', { ...job.target, args: param === 'reuse_bench' || param === 'auto_start' ? { ...job.args, [param]: true } : job.args })
    return
  }
  let spec = { verb: job.verb, target: job.target, args: job.args, consent: job.consent }
  if (param === 'consent') {
    if (!consentTyped.value) return
    spec.consent = true
  }
  else if (param === 'bench') spec.target = { ...job.target, bench: String(value) }
  else if (param === 'auto_start') spec = { verb: 'start', target: { project: job.target.project, ...(job.target.bench != null ? { bench: job.target.bench } : {}) }, args: {} }
  else {
    openJob(job.verb, { ...job.target, args: job.args })
    return
  }
  actionError.value = null
  try {
    const next = await createJob(spec)
    await router.push(`/jobs/${next.id}`)
  } catch (error) { actionError.value = error }
}
</script>

<template>
  <PageHeader>
    <Breadcrumbs :items="[{ label: 'Jobs', route: '/jobs' }, { label: record ? jobLabel(record) : id, route: route.path }]" />
    <div v-if="record" class="flex items-center gap-2">
      <Badge :theme="JOB_THEME[record.state] || 'gray'" :label="record.state.replace('_', ' ')" />
      <Button v-if="active" icon-left="lucide-panel-right-close" label="Detach" @click="detach" />
      <Button v-if="active" theme="red" icon-left="lucide-square" :label="record.state === 'interrupting' ? 'Cleaning up' : 'Interrupt'" :disabled="record.state === 'interrupting'" @click="interrupt" />
    </div>
  </PageHeader>

  <div class="mx-auto max-w-5xl space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <LoadingText v-if="loading && !record" text="Reading the job record..." />
    <ErrorMessage v-if="!record && jobs.error" :message="jobs.error" />
    <template v-if="record">
      <div class="grid grid-cols-2 gap-px overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-gray-2 lg:grid-cols-4">
        <div class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Action</div><div class="mt-1.5 capitalize text-base text-ink-gray-8">{{ jobLabel(record) }}</div></div>
        <div class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Target</div><div class="mt-1.5 truncate text-base text-ink-gray-8">{{ jobTarget(record) }}</div></div>
        <div class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Elapsed</div><div class="mt-1.5 tabular-nums text-base text-ink-gray-8">{{ elapsed(record) }}</div></div>
        <div class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Events</div><div class="mt-1.5 tabular-nums text-base text-ink-gray-8">{{ record.event_count }}</div></div>
      </div>

      <Alert v-if="record.state === 'unknown'" theme="gray" title="The job process vanished" :description="record.error?.message" />
      <template v-else-if="record.state === 'needs_choice'">
        <Alert v-if="benchOptions.length || consentNeeded" theme="amber" title="cwcli needs a decision" :description="record.choice.prompt" />
        <Alert v-else theme="amber" title="cwcli needs a decision" :description="record.choice?.prompt || 'Review the job and choose how to continue.'" :primary-action="{ label: record.choice?.param === 'auto_start' && record.verb !== 'init' ? 'Start instance' : 'Continue', onClick: () => continueChoice() }" />
        <div v-if="benchOptions.length" class="flex flex-wrap gap-2">
          <Button v-for="option in benchOptions" :key="option.value" icon-left="lucide-layers" :label="option.label" @click="continueChoice(option.value)" />
        </div>
        <div v-if="consentNeeded" class="flex flex-wrap items-end gap-2">
          <FormControl v-model="typedName" class="w-72" :label="`Type ${consentTarget} to confirm`" :placeholder="consentTarget" autocomplete="off" />
          <Button variant="solid" label="Continue" :disabled="!consentTyped" @click="continueChoice()" />
        </div>
      </template>
      <Alert v-else-if="failure" theme="red" :title="failure.title" :description="failure.description" />
      <div v-if="record.retryable" class="flex flex-wrap items-center gap-3">
        <Button variant="solid" icon-left="lucide-rotate-cw" label="Retry" :loading="retrying" @click="retry" />
        <p class="text-p-sm text-ink-gray-5">{{ record.verb === 'init' ? 'Starts a new job with the same settings and passwords. It continues from whatever this one left behind.' : 'Starts a new job with the same settings.' }}</p>
      </div>
      <ErrorMessage :message="actionError" />

      <section v-if="preflight" class="rounded-6 border border-outline-gray-1 p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div><h2 class="text-lg-semibold text-ink-gray-9">Update preflight</h2><p class="mt-1 text-p-sm text-ink-gray-5">{{ preflight.summary }}</p></div>
          <Badge :theme="preflight.blocked ? 'red' : preflight.findings.length ? 'amber' : 'green'" :label="preflight.blocked ? 'blocked' : `${preflight.findings.length} findings`" />
        </div>
        <div v-if="preflight.findings.length" class="mt-4 divide-y divide-outline-gray-1">
          <div v-for="finding in preflight.findings" :key="`${finding.code}-${finding.path}`" class="py-3">
            <div class="flex items-center gap-2"><Badge :theme="finding.severity === 'blocker' ? 'red' : finding.severity === 'data_loss' ? 'amber' : 'gray'" :label="finding.severity.replace('_', ' ')" /><span class="font-mono text-sm text-ink-gray-8">{{ finding.path || finding.app }}</span></div>
            <p class="mt-1 text-p-sm text-ink-gray-6">{{ finding.message }}</p>
          </div>
        </div>
      </section>

      <section v-if="warnings.length">
        <h2 class="text-lg-semibold text-ink-gray-9">Warnings</h2>
        <div class="mt-3 space-y-2"><Alert v-for="warning in warnings" :key="warning.code" theme="amber" :title="warning.code" :description="warning.text" /></div>
      </section>

      <section>
        <div class="flex items-baseline justify-between gap-3"><h2 class="text-lg-semibold text-ink-gray-9">Event log</h2><Button size="sm" variant="ghost" icon-left="lucide-refresh-cw" label="Refresh" :loading="loading" @click="refresh(record.events?.at(-1)?.seq || 0)" /></div>
        <div v-if="!items.length" class="mt-3 rounded-6 border border-outline-gray-1 bg-surface-gray-1 px-4 py-8 text-center text-p-sm text-ink-gray-5">Waiting for the first event...</div>
        <FollowPane v-else :items="items" class="mt-3 max-h-[60vh] rounded-6 border border-outline-gray-1 bg-surface-gray-1">
          <template #default="{ item, index }">
            <div v-if="item.event" :class="['grid grid-cols-[5rem_minmax(0,1fr)] gap-3 px-3 py-2.5 sm:grid-cols-[5.5rem_9rem_minmax(0,1fr)]', !isLast(index) && 'border-b border-outline-gray-1']">
              <span class="whitespace-nowrap font-mono text-xs leading-5 tabular-nums text-ink-gray-4">{{ time(item.event.at) }}</span>
              <span class="hidden truncate font-mono text-xs leading-5 text-ink-gray-5 sm:block">{{ item.event.phase || item.event.action || item.event.type }}</span>
              <div class="min-w-0"><p class="break-words font-mono text-xs leading-5 text-ink-gray-7">{{ eventTitle(item.event) }}</p><p v-if="eventItem(item.event)" class="mt-0.5 break-words text-xs leading-4 text-ink-gray-5">{{ eventItem(item.event) }}</p></div>
            </div>
            <div v-else :class="['grid grid-cols-[5rem_minmax(0,1fr)] gap-3 px-3 sm:grid-cols-[5.5rem_9rem_minmax(0,1fr)]', firstLine(item) && 'pt-2.5', lastLine(item) && 'pb-2.5', lastLine(item) && !isLast(index) && 'border-b border-outline-gray-1']">
              <span class="whitespace-nowrap font-mono text-xs leading-5 tabular-nums text-ink-gray-4">{{ firstLine(item) ? time(item.block.at) : '' }}</span>
              <span class="hidden font-mono text-xs leading-5 text-ink-gray-5 sm:block">{{ firstLine(item) ? 'output' : '' }}</span>
              <span class="min-h-5 min-w-0 whitespace-pre-wrap break-words font-mono text-xs leading-5 text-ink-gray-7">{{ lineText(item) }}</span>
            </div>
          </template>
        </FollowPane>
      </section>
    </template>
  </div>
</template>
