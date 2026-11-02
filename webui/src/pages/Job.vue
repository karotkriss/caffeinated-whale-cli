<script setup>
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Badge, Breadcrumbs, Button, ErrorMessage, FormControl, PageHeader, LoadingText } from 'frappe-ui'
import { openSchedule, scheduleFromJob } from '../data/schedule-dialog'
import { VERBS as SCHEDULE_VERBS } from '../data/schedules'
const SCHEDULABLE = new Set(SCHEDULE_VERBS.map((v) => v.value))
import { ACTIVE_STATES, continueJob, createJob, detachJob, elapsed, interruptJob, jobAccount, jobLabel, jobPath, jobRecord, jobTarget, jobs, loadJob, retryJob } from '../data/jobs'
import { hostName } from '../data/hosts'
import RemoteHostBadge from '../components/RemoteHostBadge.vue'
import { jobDialog, openJob } from '../data/job-dialog'
import { setFocus } from '../data/fleet'
import FollowPane from '../components/FollowPane.vue'
import JobSteps from '../components/JobSteps.vue'
import JobState from '../components/JobState.vue'
import DependencyTree from '../components/DependencyTree.vue'
import { backups, loadBackups } from '../data/backups'
import { openRestoreWizard } from '../data/restore-wizard'
import { openNewBench } from '../data/new-bench-dialog'
import { openNewSite } from '../data/new-site-dialog'
import { TabButtons } from 'frappe-ui'
import { feed, openLine } from '../data/follow'

const route = useRoute()
const router = useRouter()
const id = String(route.params.id)
// A job on a remote host: read and controlled through the hub's forward.
const host = route.params.host || null
const loading = ref(false)
const view = ref('steps')
const actionError = ref(null)
const record = computed(() => jobRecord(id, host))
// A publish runs with the integrations of the machine the site is on, never the hub's.
const publishJob = computed(() => ['publish-site', 'unpublish-site'].includes(record.value?.verb) || !!record.value?.args?.publish)
// Captain decision 5: a core update's safety sets, with 'Roll back to before this'.
const safetyKeys = computed(() => (record.value?.events || []).filter((e) => e.type === 'safety_backup_done' || e.type === 'backup_first_done').flatMap((e) => e.data?.sets || []))
const safetySets = computed(() => safetyKeys.value.map((k) => (backups.data?.sets || []).find((s) => s.key === k)).filter(Boolean))
// Phase 2: x of N for a batch clone/move, the sendme ticket, and the sets a receive filed.
const progress = computed(() => (record.value?.events || []).filter((e) => e.type === 'transfer_progress').at(-1)?.data || null)
const ticket = computed(() => (record.value?.events || []).find((e) => e.type === 'ticket')?.data?.ticket || null)
const receivedKeys = computed(() => (record.value?.events || []).find((e) => e.type === 'received')?.data?.sets || [])
const receivedSets = computed(() => receivedKeys.value.map((k) => (backups.data?.sets || []).find((s) => s.key === k)).filter(Boolean))
watch(receivedKeys, (keys) => { if (keys.length) loadBackups() }, { immediate: true })
// A failed update names the stage, the site and the bench state (a staging-server lesson).
const benchState = computed(() => {
  const r = record.value?.result
  if (!r || !Array.isArray(r.affected_sites)) return null
  const failed = r.failed_migrations || [], unknown = r.unknown_migrations || [], noMaint = r.failed_maintenance_enable || [], unreadable = r.unreadable_sites || []
  const notReached = (r.affected_sites || []).filter((s) => !(r.migrated_sites || []).includes(s))
  if (!failed.length && !unknown.length && !noMaint.length && !unreadable.length && !notReached.length && !r.aborted) return null
  const why = {}
  for (const s of failed) why[s] = 'migration failed'
  for (const s of unknown) why[s] = 'outcome unknown, may still be running'
  for (const s of noMaint) why[s] = 'could not enter maintenance mode'
  for (const s of unreadable) why[s] = 'could not be opened, so it was never migrated'
  for (const s of notReached) why[s] ||= 'not reached'
  const done = (r.migrated_sites || []).filter((s) => !failed.includes(s) && !unknown.includes(s))
  const stuck = r.failed_maintenance_disable || []
  return { done, notDone: Object.entries(why).sort(), stuck }
})
// Steering 041: a site whose database user is scoped to an address the container no
// longer has - name it and offer the one repair.
const dbRefused = computed(() => (record.value?.warnings || []).find((w) => w.code === 'db.login_refused') || null)
const repairing = ref(false)
async function repairDbAccess() {
  repairing.value = true
  try {
    const sites = (dbRefused.value?.detail?.sites || []).map((s) => s.site)
    const next = await createJob({ verb: 'repair-db-access', target: { project: record.value.target.project, bench: record.value.target.bench }, args: { sites } }, host)
    router.push(jobPath(next))
  } catch (e) { actionError.value = e } finally { repairing.value = false }
}
const copied = ref(false)
async function copyTicket() { try { await navigator.clipboard.writeText(ticket.value); copied.value = true; setTimeout(() => (copied.value = false), 1500) } catch {} }
watch(safetyKeys, (keys) => { if (keys.length && !backups.data) loadBackups() }, { immediate: true })

const active = computed(() => ACTIVE_STATES.has(record.value?.state))
// Steering 034: any schedulable verb's page offers the same job on a timetable (this
// machine's schedules; a remote host's are its own).
const schedulable = computed(() => !host && !!record.value && SCHEDULABLE.has(record.value.verb))
const preflight = computed(() => record.value?.events?.find((event) => event.type === 'update_preflight')?.data)
// A dependency mismatch the preflight can fix: the same job again with fix_branches set.
const fixable = computed(() => (preflight.value?.findings || []).filter((f) => f.code === 'app.frappe_dependency_unsatisfied'))
// A required app the new code needs and the bench or a site lacks: the same job again
// with with_deps set, which fetches and installs it first.
const requiredFindings = computed(() => (preflight.value?.findings || []).filter((f) => f.code === 'app.required_app_missing' || f.code === 'app.required_app_not_installed'))
const requiredApps = computed(() => [...new Set(requiredFindings.value.map((f) => f.message.match(/requires (\S+?) \(required_apps\)|but not (\S+?), which/)?.slice(1).find(Boolean)).filter(Boolean))])
const fetchingDeps = ref(false)
async function fetchDepsAndRetry() {
  fetchingDeps.value = true
  try {
    const r = record.value
    const next = await createJob({ verb: r.verb, target: r.target, args: { ...(r.args || {}), with_deps: true }, consent: !!r.consent }, host)
    router.push(jobPath(next))
  } catch (e) { actionError.value = e } finally { fetchingDeps.value = false }
}
// A refused install or new site carries its dependency plan: show it as the tree.
const refusedPlan = computed(() => (record.value?.state === 'failed' ? record.value?.error?.detail?.plan : null) || null)
const switching = ref(false)
async function switchAndRetry() {
  switching.value = true
  try {
    const r = record.value
    const next = await createJob({ verb: r.verb, target: r.target, args: { ...(r.args || {}), fix_branches: true }, consent: !!r.consent }, host)
    router.push(jobPath(next))
  } catch (e) { actionError.value = e } finally { switching.value = false }
}
// db.login_refused has its own alert with the repair button above; do not list it twice.
const warnings = computed(() => (record.value?.warnings || []).filter((w) => w.code !== 'db.login_refused'))
// A failed job leads with its plain cause (MariaDB rejected the password, git could
// not authenticate, ...) when cwcli recognized one in the failing step's own output;
// otherwise it says so and points at that output rather than guessing.
const PUBLISH_PART = { dns: 'DNS record', proxy: 'proxy host', certificate: 'certificate', verify: 'check that the site answers' }
const failure = computed(() => {
  const job = record.value
  if (job?.state !== 'failed') return null
  if (job.result?.preflight_blocked) return { title: 'The preflight refused this update', description: 'Nothing ran. The findings below name what to fix; a switch-and-retry button appears when cwcli can do it for you.' }
  if (job.result?.data_loss_refused) return { title: 'Refused: local work would be discarded', description: `Nothing ran. ${(job.result.data_loss_apps || []).join(', ')} holds uncommitted or unpushed work the reset would discard; tick Force to accept that loss.` }
  // A publish fails by its report, part by part: name the part and its own reason.
  const failedPart = job.verb === 'publish-site' && Object.entries(job.result?.parts || {}).find(([, p]) => p.state === 'failed')
  if (failedPart) return { title: `Publishing stopped at the ${PUBLISH_PART[failedPart[0]]}`, description: `${failedPart[1].message} The site itself was not touched; Retry repeats only what is missing.` }
  if (job.verb === 'unpublish-site' && job.result?.failed?.length) return { title: 'Part of the public address was not removed', description: `${job.result.failed.join(' ')} It stays recorded, so Retry finishes it.` }
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
// MariaDB rejected the root password a New bench or New site used: reopen its dialog
// with the same inputs and the password field, whether or not cwcli saved one.
const passwordRejected = computed(() => record.value?.state === 'failed' && record.value.error?.code === 'db.root_password_wrong' && ['new-bench', 'new-site'].includes(record.value.verb))
function enterRootPassword() {
  const r = record.value
  const retry = { args: r.args || {}, site: r.target?.site, rejected: [r.error.message, r.error.hint].filter(Boolean).join(' ') }
  if (r.verb === 'new-bench') openNewBench({ project: r.target.project, host, retry })
  else openNewSite({ project: r.target.project, bench: r.target.bench, host, retry })
}
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

// A long job opens with its last 400 events (an init writes thousands, 20 MB as
// JSON); the earlier ones load on request.
const TAIL = 400
async function refresh(after = 0, limit = null) {
  loading.value = true
  try { await loadJob(id, after, limit, host) } finally { loading.value = false }
}
async function loadAll() {
  seen = 0
  block = null
  items.value = []
  await refresh(0)
}

onMounted(() => {
  setFocus(record.value?.target?.project || null, host)
  refresh(0, TAIL)
})
watch(() => record.value?.event_count, (count, previous) => {
  if (previous != null && count > previous) refresh(record.value?.events?.at(-1)?.seq || 0)
})

async function interrupt() {
  actionError.value = null
  try { await interruptJob(id, host) } catch (error) { actionError.value = error }
}

async function detach() {
  actionError.value = null
  try {
    await detachJob(id, host)
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
    const next = await retryJob(id, host)
    await router.push(jobPath(next))
  } catch (error) {
    if (error.code === 'job.retry_secrets_missing' && job.verb === 'init') {
      openJob('init', { ...job.target, args: job.args, host })
      jobDialog.step = 2
    } else actionError.value = error
  } finally { retrying.value = false }
}

const benchOptions = computed(() => record.value?.choice?.param === 'bench' ? record.value.choice.options || [] : [])
const consentNeeded = computed(() => record.value?.choice?.param === 'consent')
// The dependency questions: consent to the required apps, or a pick between sources.
const depRows = computed(() => (record.value?.choice?.kind === 'confirm_required_apps' ? record.value.choice.options || [] : []))
const sourceOptions = computed(() => (record.value?.choice?.kind === 'select_app_source' ? record.value.choice.options || [] : []))
// The name typed back is the most specific thing the job acts on: a dropped
// site's name, else the instance's (scale, rm, rm-bench).
const consentTarget = computed(() => record.value?.target?.site || record.value?.target?.project)
const typedName = ref('')
const consentTyped = computed(() => !!consentTarget.value && typedName.value.trim() === consentTarget.value)

async function continueChoice(value) {
  const job = record.value
  const param = job.choice?.param
  // An answered choice continues on the daemon, which supplies the passwords it
  // still holds for this job; the page never sees them.
  let spec = { verb: job.verb, target: job.target, args: job.args, consent: job.consent }
  let start = (s) => continueJob(id, s, host)
  if (job.verb === 'init') {
    if (param !== 'reuse_bench' && param !== 'auto_start') {
      openJob('init', { ...job.target, args: job.args, host })
      return
    }
    spec.args = { ...job.args, [param]: true }
  }
  else if (param === 'consent') {
    if (!consentTyped.value) return
    spec.consent = true
  }
  else if (param === 'bench') spec.target = { ...job.target, bench: String(value) }
  else if (param === 'with_deps') spec.args = { ...job.args, with_deps: true }
  else if (param === 'dep_sources') {
    const app = sourceOptions.value[0]?.app
    spec.args = { ...job.args, dep_sources: { ...(job.args?.dep_sources || {}), [app]: String(value) } }
  }
  else if (param === 'auto_start') {
    spec = { verb: 'start', target: { project: job.target.project, ...(job.target.bench != null ? { bench: job.target.bench } : {}) }, args: {} }
    start = (s) => createJob(s, host)
  }
  else {
    openJob(job.verb, { ...job.target, args: job.args, host })
    return
  }
  actionError.value = null
  try {
    const next = await start(spec)
    await router.push(jobPath(next))
  } catch (error) {
    if (error.code === 'job.retry_secrets_missing' && job.verb === 'init') {
      openJob('init', { ...job.target, args: spec.args, host })
      jobDialog.step = 2
    } else actionError.value = error
  }
}
</script>

<template>
  <PageHeader class="sticky top-0">
    <Breadcrumbs :items="[{ label: 'Jobs', route: '/jobs' }, ...(host ? [{ label: hostName(host), route: '/jobs' }] : []), { label: record ? jobLabel(record) : id, route: route.path }]" />
    <div v-if="record" class="flex items-center gap-2">
      <RemoteHostBadge :host="host" size="md" />
      <JobState :state="record.state" size="md" />
      <Button v-if="schedulable" icon-left="lucide-calendar-clock" label="Schedule this…" @click="openSchedule(scheduleFromJob(record))" />
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
        <div class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Started by</div><div class="mt-1.5 truncate text-base text-ink-gray-8">{{ jobAccount(record) }}</div></div>
        <div v-if="host" class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Machine</div><div class="mt-1.5 truncate text-base text-ink-gray-8">{{ hostName(host) }}</div></div>
        <div v-if="publishJob" class="bg-surface-base px-4 py-3"><div class="text-sm text-ink-gray-5">Integration settings</div><div class="mt-1.5 truncate text-base text-ink-gray-8">{{ host ? `${hostName(host)}'s own` : "this machine's" }}</div></div>
      </div>

      <div v-if="progress" class="rounded-6 border border-outline-gray-1 bg-surface-elevation-1 px-4 py-3">
        <div class="flex items-center justify-between text-sm"><span class="text-ink-gray-8">{{ progress.done }} of {{ progress.total }} {{ progress.total === 1 ? 'site' : 'sites' }} done</span><span class="truncate text-xs text-ink-gray-5">{{ progress.done < progress.total && active ? `${progress.site} → ${progress.target}` : '' }}</span></div>
        <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-gray-2"><div class="h-full rounded-full bg-surface-gray-7 transition-[width] duration-300" :style="{ width: `${Math.round((progress.done / Math.max(progress.total, 1)) * 100)}%` }" /></div>
      </div>
      <Alert v-if="ticket" class="mb-3" :theme="active ? 'blue' : 'gray'" :title="active ? 'Serving over sendme; paste this ticket on the other machine' : 'This ticket is no longer served'" description="There: Backups › Receive…, or cwcli backups receive <ticket>. Interrupt this job when the transfer is done.">
        <template #actions><span class="flex flex-wrap items-center gap-2"><code class="max-w-full break-all rounded-4 bg-surface-gray-1 px-2 py-1 font-mono text-xs text-ink-gray-8">{{ ticket }}</code><Button size="sm" variant="subtle" :icon-left="copied ? 'lucide-check' : 'lucide-copy'" :label="copied ? 'Copied' : 'Copy'" @click="copyTicket" /></span></template>
      </Alert>
      <Alert v-if="!host && receivedKeys.length" class="mb-3" theme="blue" :title="`${receivedKeys.length} ${receivedKeys.length === 1 ? 'set was' : 'sets were'} filed into the store`" description="Restore each one into a site here.">
        <template #actions><span class="flex flex-wrap gap-2"><Button v-for="s in receivedSets" :key="s.key" size="sm" variant="subtle" icon-left="lucide-rotate-ccw" :label="`Restore ${s.site}…`" @click="openRestoreWizard(s)" /><span v-if="!receivedSets.length" class="text-xs text-ink-gray-5">loading the sets…</span></span></template>
      </Alert>
      <Alert v-if="dbRefused" class="mb-3" theme="red" :title="`Database login refused for ${(dbRefused.detail?.sites || []).map((s) => s.site).join(', ') || 'a site'}`" :description="dbRefused.text">
        <template #actions><Button size="sm" variant="solid" theme="red" icon-left="lucide-wrench" label="Repair database access" :loading="repairing" @click="repairDbAccess" /></template>
      </Alert>
      <Alert v-if="!host && safetyKeys.length" class="mb-3" theme="blue" :title="`${safetyKeys.length} safety ${safetyKeys.length === 1 ? 'backup was' : 'backups were'} taken before this`" description="Each one restores its site to the state before this job ran.">
        <template #actions><span class="flex flex-wrap gap-2"><Button v-for="s in safetySets" :key="s.key" size="sm" variant="subtle" icon-left="lucide-rotate-ccw" :label="`Roll back ${s.site} to before this`" @click="openRestoreWizard(s)" /><span v-if="!safetySets.length" class="text-xs text-ink-gray-5">loading the sets…</span></span></template>
      </Alert>
      <Alert v-if="record.origin?.follows" theme="gray" :title="`Part ${record.origin.part} of ${record.origin.of}`" description="cwcli started this job when the one before it succeeded, because it was asked to." :primary-action="{ label: 'Open part 1', onClick: () => $router.push(jobPath({ id: record.origin.follows, host })) }" />
      <Alert v-if="record.followed_by?.length" theme="gray" :title="record.followed_by.length === 1 ? 'Continued as part 2' : `Continued as ${record.followed_by.length} more jobs`" :description="record.followed_by.map((id) => jobLabel(jobRecord(id, host) || { verb: 'job' }) + ' ' + jobTarget(jobRecord(id, host) || {})).join(', ')">
        <template #actions><span class="flex flex-wrap gap-2"><Button v-for="(id, i) in record.followed_by" :key="id" size="sm" variant="subtle" :label="`Open part ${i + 2}`" @click="$router.push(jobPath({ id, host }))" /></span></template>
      </Alert>
      <Alert v-if="record.follow_up_errors?.length" theme="amber" title="A follow-up job did not start" :description="record.follow_up_errors.join(' ')" />
      <Alert v-if="record.state === 'unknown'" theme="gray" title="The job process vanished" :description="[record.error?.message, record.error?.hint].filter(Boolean).join(' ')" />
      <Alert v-else-if="record.state === 'answered'" theme="gray" title="Decision taken" :description="`This job stopped for a decision; you answered it and the work continued as another job.`" :primary-action="record.answered_by ? { label: 'Open the follow-up job', onClick: () => $router.push(jobPath({ id: record.answered_by, host })) } : undefined" />
      <template v-else-if="record.state === 'needs_choice'">
        <template v-if="depRows.length">
          <Alert theme="amber" title="This app requires apps the bench or site does not have" :description="record.choice.prompt.split(' Fetch and install')[0] + ' Nothing was fetched or installed yet.'" />
          <div class="overflow-hidden rounded-6 border border-outline-gray-1">
            <div v-for="row in depRows" :key="row.app" class="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-outline-gray-1 px-3 py-2 last:border-b-0" :class="row.required_by && 'pl-7'">
              <span class="tabular-nums text-xs text-ink-gray-4">{{ row.order }}.</span>
              <span class="font-mono text-sm text-ink-gray-9">{{ row.app }}</span>
              <Badge size="sm" :theme="row.action === 'none' ? 'gray' : 'blue'" :label="row.action.replace('+', ' + ')" />
              <span class="min-w-0 break-all text-xs text-ink-gray-5">{{ [row.required_by ? `required by ${row.required_by}` : '', row.source, row.ref].filter(Boolean).join(' · ') }}</span>
            </div>
          </div>
          <div class="flex flex-wrap items-center gap-3">
            <Button variant="solid" icon-left="lucide-package-plus" label="Fetch and install the required apps, then continue" @click="continueChoice()" />
            <p class="text-p-sm text-ink-gray-5">Starts this job again with consent: the required apps first, in the order above.</p>
          </div>
        </template>
        <template v-else-if="sourceOptions.length">
          <Alert theme="amber" title="Pick where to fetch the required app from" :description="record.choice.prompt" />
          <div class="flex flex-wrap gap-2">
            <Button v-for="o in sourceOptions" :key="o.value" :variant="o.value === record.choice.default ? 'solid' : 'subtle'" icon-left="lucide-git-branch" :label="o.label + (o.ok ? '' : ' (range does not fit)')" @click="continueChoice(o.value)" />
          </div>
        </template>
        <Alert v-else-if="benchOptions.length || consentNeeded" theme="amber" title="cwcli needs a decision" :description="record.choice.prompt" />
        <Alert v-if="!depRows.length && !sourceOptions.length && !benchOptions.length && !consentNeeded" theme="amber" title="cwcli needs a decision" :description="record.choice?.prompt || 'Review the job and choose how to continue.'" :primary-action="{ label: record.choice?.param === 'auto_start' && record.verb !== 'init' ? 'Start instance' : 'Continue', onClick: () => continueChoice() }" />
        <div v-if="benchOptions.length" class="flex flex-wrap gap-2">
          <Button v-for="option in benchOptions" :key="option.value" icon-left="lucide-layers" :label="option.label" @click="continueChoice(option.value)" />
        </div>
        <div v-if="consentNeeded" class="flex flex-wrap items-end gap-2">
          <FormControl v-model="typedName" class="w-72" :label="`Type ${consentTarget} to confirm`" :placeholder="consentTarget" autocomplete="off" />
          <Button variant="solid" label="Continue" :disabled="!consentTyped" @click="continueChoice()" />
        </div>
      </template>
      <Alert v-else-if="failure" theme="red" :title="failure.title" :description="failure.description" :primary-action="passwordRejected ? { label: 'Enter the root password', onClick: enterRootPassword } : undefined" />
      <DependencyTree v-if="refusedPlan" :plan="refusedPlan" />
      <div v-if="benchState" class="rounded-6 border border-outline-red-2 bg-surface-red-1 px-4 py-3 text-sm">
        <div class="font-medium text-ink-red-6">Bench state after this run</div>
        <dl class="mt-1 grid grid-cols-[8rem_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-ink-gray-8">
          <dt class="text-ink-gray-5">migrated</dt><dd>{{ benchState.done.length ? benchState.done.join(', ') : 'none' }}</dd>
          <dt class="text-ink-gray-5">not migrated</dt><dd>{{ benchState.notDone.length ? benchState.notDone.map(([s, w]) => `${s} (${w})`).join(', ') : 'none' }}</dd>
          <dt class="text-ink-gray-5">maintenance</dt><dd>{{ benchState.stuck.length ? `still ON for ${benchState.stuck.join(', ')}` : 'cleared on every site cwcli touched' }}</dd>
          <dt class="text-ink-gray-5">scheduler</dt><dd>never paused bench-wide</dd>
        </dl>
      </div>
      <div v-if="record.retryable" class="flex flex-wrap items-center gap-3">
        <Button variant="solid" icon-left="lucide-rotate-cw" label="Retry" :loading="retrying" @click="retry" />
        <p class="text-p-sm text-ink-gray-5">{{ record.verb === 'init' ? 'Starts a new job with the same settings and passwords. It continues from whatever this one left behind.' : 'Starts a new job with the same settings.' }}</p>
      </div>
      <p v-else-if="record.retry_refusal" class="text-p-sm text-ink-gray-5">{{ record.retry_refusal }}</p>
      <ErrorMessage :message="actionError" />

      <section v-if="preflight" class="rounded-6 border border-outline-gray-1 p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div><h2 class="text-lg-semibold text-ink-gray-9">Update preflight</h2><p class="mt-1 text-p-sm text-ink-gray-5">{{ preflight.summary }}</p></div>
          <Badge :theme="preflight.blocked ? 'red' : preflight.findings.length ? 'amber' : 'green'" :label="preflight.blocked ? 'blocked' : `${preflight.findings.length} findings`" />
        </div>
        <div v-if="preflight.blocked && fixable.length && !active" class="mt-3 flex flex-wrap items-center gap-3">
          <Button variant="solid" icon-left="lucide-git-branch" :label="`Switch ${[...new Set(fixable.map((f) => f.remedy?.match(/checkout \S+ (\S+) (version-\d+)/)?.[1] || f.app))].join(', ')} to the matching branch and retry`" :loading="switching" @click="switchAndRetry" />
          <p class="text-p-sm text-ink-gray-5">Checks out the matching version-N branch in each app named below (a dirty tree still refuses), then runs this update again.</p>
        </div>
        <div v-if="preflight.blocked && requiredFindings.length && !active" class="mt-3 flex flex-wrap items-center gap-3">
          <Button variant="solid" icon-left="lucide-package-plus" :label="`Fetch and install ${requiredApps.join(', ') || 'the required apps'}, then retry`" :loading="fetchingDeps" @click="fetchDepsAndRetry" />
          <p class="text-p-sm text-ink-gray-5">Fetches each from the apps catalog, installs it on the sites that have the updated app, then runs this update again.</p>
        </div>
        <div v-if="preflight.findings.length" class="mt-4 divide-y divide-outline-gray-1">
          <div v-for="finding in preflight.findings" :key="`${finding.code}-${finding.path}`" class="py-3">
            <div class="flex items-center gap-2"><Badge :theme="finding.severity === 'blocker' ? 'red' : finding.severity === 'data_loss' ? 'amber' : 'gray'" :label="finding.severity.replace('_', ' ')" /><span class="font-mono text-sm text-ink-gray-8">{{ finding.path || finding.app }}</span></div>
            <p class="mt-1 text-p-sm text-ink-gray-6">{{ finding.message }}</p>
            <p v-if="finding.remedy" class="mt-1 text-p-sm text-ink-gray-5">{{ finding.remedy }}</p>
          </div>
        </div>
      </section>

      <section v-if="warnings.length">
        <h2 class="text-lg-semibold text-ink-gray-9">Warnings</h2>
        <div class="mt-3 space-y-2"><Alert v-for="warning in warnings" :key="warning.code" theme="amber" :title="warning.code" :description="warning.text" /></div>
      </section>

      <section>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h2 class="text-lg-semibold text-ink-gray-9">{{ view === 'steps' ? 'Steps' : 'Event log' }}</h2>
          <div class="flex items-center gap-2">
            <TabButtons v-model="view" :options="[{ label: 'Steps', value: 'steps' }, { label: 'Raw log', value: 'raw' }]" />
            <Button size="sm" variant="ghost" icon-left="lucide-refresh-cw" label="Refresh" :loading="loading" @click="refresh(record.events?.at(-1)?.seq || 0)" />
          </div>
        </div>
        <Alert v-if="record.events_skipped" class="mt-3" theme="gray" :title="`${record.events_skipped} earlier ${record.events_skipped === 1 ? 'event is' : 'events are'} not shown`" description="This job wrote more than the page loads at first; the steps below are its last 400 events." :primary-action="{ label: 'Load all events', onClick: loadAll }" />
        <JobSteps v-if="view === 'steps'" class="mt-3" :events="record.events || []" :state="record.state" />
        <div v-else-if="!items.length" class="mt-3 rounded-6 border border-outline-gray-1 bg-surface-gray-1 px-4 py-8 text-center text-p-sm text-ink-gray-5">Waiting for the first event...</div>
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
