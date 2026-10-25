<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Badge, Breadcrumbs, Button, ErrorMessage, FormControl, PageHeader, LoadingText } from 'frappe-ui'
import { ACTIVE_STATES, JOB_THEME, createJob, detachJob, elapsed, interruptJob, jobLabel, jobTarget, jobs, loadJob } from '../data/jobs'
import { openJob } from '../data/job-dialog'
import { setFocus } from '../data/fleet'
import FollowPane from '../components/FollowPane.vue'
import { MAX_LINES, blockView, tailLines } from '../data/follow'

const route = useRoute()
const router = useRouter()
const id = String(route.params.id)
const loading = ref(false)
const actionError = ref(null)
const record = computed(() => jobs.records[id])
const active = computed(() => ACTIVE_STATES.has(record.value?.state))
const preflight = computed(() => record.value?.events?.find((event) => event.type === 'update_preflight')?.data)
const warnings = computed(() => record.value?.warnings || [])
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
// Consecutive output chunks (the command's own bytes) collapse into one
// terminal block, and a \r progress bar keeps only its last repaint per line.
const rows = computed(() => {
  const out = []
  for (const event of record.value?.events || []) {
    const text = event.type.endsWith('_output') ? event.data?.text ?? event.message ?? '' : null
    const last = out.at(-1)
    if (text === null) out.push(event)
    else if (last?.output) last.text += text
    else out.push({ seq: event.seq, at: event.at, output: true, text })
  }
  return out
})
const terminal = (text) =>
  text
    .replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '')
    .split('\n')
    .map((line) => line.slice(line.lastIndexOf('\r') + 1))
    .join('\n')
    .trim()
const following = ref(true)
const expanded = reactive(new Set())
const held = new Map()
const views = computed(() => {
  const out = {}
  for (const row of rows.value) {
    if (!row.output) continue
    if (following.value) held.delete(row.seq)
    else if (!held.has(row.seq)) held.set(row.seq, tailLines(row.text).hidden)
    const { hidden, text } = blockView(row.text, { all: expanded.has(row.seq), held: held.get(row.seq) })
    const shown = terminal(text)
    out[row.seq] = { hidden, total: hidden + shown.split('\n').length, text: shown }
  }
  return out
})
const toggleBlock = (seq) => (expanded.has(seq) ? expanded.delete(seq) : expanded.add(seq))

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
      <Alert v-else-if="record.error" theme="red" :title="record.error.code || 'Job failed'" :description="`${record.error.message}${record.error.hint ? ` ${record.error.hint}` : ''}`" />
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
        <FollowPane v-model:following="following" class="mt-3 max-h-[60vh] rounded-6 border border-outline-gray-1 bg-surface-gray-1">
          <div v-if="!record.events?.length" class="px-4 py-8 text-center text-p-sm text-ink-gray-5">Waiting for the first event...</div>
          <div v-for="event in rows" :key="event.seq" class="grid grid-cols-[5rem_minmax(0,1fr)] gap-3 border-b border-outline-gray-1 px-3 py-2.5 last:border-b-0 sm:grid-cols-[5.5rem_9rem_minmax(0,1fr)]">
            <span class="whitespace-nowrap font-mono text-xs tabular-nums text-ink-gray-4">{{ time(event.at) }}</span>
            <span class="hidden truncate font-mono text-xs text-ink-gray-5 sm:block">{{ event.output ? 'output' : event.phase || event.action || event.type }}</span>
            <div v-if="event.output" class="min-w-0">
              <Button v-if="views[event.seq].hidden || expanded.has(event.seq)" class="mb-1.5" size="sm" variant="outline" :label="expanded.has(event.seq) ? (following ? `Show last ${MAX_LINES.toLocaleString()} lines` : 'Show less') : `Show all ${views[event.seq].total.toLocaleString()} lines`" @click="toggleBlock(event.seq)" />
              <pre class="whitespace-pre-wrap break-words font-mono text-xs leading-5 text-ink-gray-7">{{ views[event.seq].text }}</pre>
            </div>
            <div v-else class="min-w-0"><p class="break-words font-mono text-xs leading-5 text-ink-gray-7">{{ eventTitle(event) }}</p><p v-if="event.item || event.data?.item" class="mt-0.5 truncate text-xs text-ink-gray-5">{{ event.item || event.data.item }}</p></div>
          </div>
        </FollowPane>
      </section>
    </template>
  </div>
</template>
