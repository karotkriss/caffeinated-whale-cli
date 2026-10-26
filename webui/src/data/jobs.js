import { computed, reactive, ref } from 'vue'
import { getJson, postJson } from './api'

export const jobs = reactive({
  records: {},
  loaded: false,
  loading: false,
  error: null,
})

export const jobList = computed(() =>
  Object.values(jobs.records).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))),
)

export const activeJobs = computed(() =>
  jobList.value.filter((job) => ['queued', 'running', 'interrupting'].includes(job.state)),
)

export function applyJob(record) {
  if (!record?.id) return
  if (record.removed) delete jobs.records[record.id]
  else jobs.records[record.id] = { ...(jobs.records[record.id] || {}), ...record }
}

export function seedJobs(records = []) {
  const current = jobs.records
  jobs.records = Object.fromEntries(
    records.map((job) => [job.id, { ...(current[job.id] || {}), ...job }]),
  )
  jobs.loaded = true
}

export async function loadJobs({ force = false } = {}) {
  if ((jobs.loaded || jobs.loading) && !force) return jobList.value
  jobs.loading = true
  try {
    seedJobs(await getJson('/api/v1/jobs'))
    jobs.error = null
    return jobList.value
  } catch (error) {
    jobs.error = error
    throw error
  } finally {
    jobs.loading = false
  }
}

export async function loadJob(id, after = 0) {
  const record = await getJson(`/api/v1/jobs/${encodeURIComponent(id)}?after=${after}`)
  const prior = jobs.records[id]
  // Refreshes can overlap with the same cursor, so append only events newer than the last one held.
  const held = after && prior?.events ? prior.events : []
  const lastSeq = held.at(-1)?.seq || 0
  const events = [...held, ...(record.events || []).filter((event) => event.seq > lastSeq)]
  applyJob({ ...record, events })
  return jobs.records[id]
}

export async function createJob(spec) {
  const record = await postJson('/api/v1/jobs', spec)
  applyJob(record)
  return record
}

export async function interruptJob(id) {
  const record = await postJson(`/api/v1/jobs/${encodeURIComponent(id)}/interrupt`, {})
  applyJob(record)
  return record
}

export async function retryJob(id) {
  const record = await postJson(`/api/v1/jobs/${encodeURIComponent(id)}/retry`, {})
  applyJob(record)
  return record
}

export async function detachJob(id) {
  const record = await postJson(`/api/v1/jobs/${encodeURIComponent(id)}/detach`, {})
  applyJob(record)
  return record
}

export const ACTIVE_STATES = new Set(['queued', 'running', 'interrupting'])

export const JOB_THEME = {
  queued: 'gray',
  running: 'blue',
  interrupting: 'amber',
  succeeded: 'green',
  failed: 'red',
  interrupted: 'amber',
  unknown: 'gray',
  needs_choice: 'amber',
}

export const JOB_LABEL = {
  backup: 'Back up site',
  build: 'Build assets',
  'apps-checkout': 'Check out app ref',
  'apps-install': 'Install apps',
  'apps-update': 'Update apps',
  'clear-maintenance': 'Clear maintenance mode',
  init: 'Create instance',
  migrate: 'Migrate site',
  restart: 'Restart instance',
  'restart-process': 'Restart process',
  rm: 'Remove instance',
  'rm-bench': 'Remove bench',
  'rm-site': 'Drop site',
  'run-tests': 'Run tests',
  scale: 'Scale bench',
  'self-update': 'Update cwcli',
  'setup-wizard': 'Complete setup wizard',
  start: 'Start instance',
  stop: 'Stop instance',
  'stop-bench': 'Stop bench',
  unlock: 'Unlock site',
}

export function jobLabel(job) {
  if (job?.verb === 'apps-checkout' && job?.args?.reset === true) return 'Check out app ref, discarding local changes'
  return JOB_LABEL[job?.verb] || String(job?.verb || 'job').replaceAll('-', ' ')
}

export function jobTarget(job) {
  const target = job?.target || {}
  if (target.app || target.site) return target.app || target.site
  if (target.bench != null && target.project) return `${target.project} · bench ${target.bench}`
  return target.project || 'cwcli'
}

// One shared clock so a running job's Elapsed ticks instead of waiting for its next event.
const now = ref(Date.now())
window.setInterval(() => (now.value = Date.now()), 1000)

export function elapsed(job) {
  const start = Date.parse(job?.started_at || job?.created_at || '')
  const end = Date.parse(job?.finished_at || '') || now.value
  if (!Number.isFinite(start)) return 'unknown'
  const seconds = Math.max(0, Math.floor((end - start) / 1000))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}
