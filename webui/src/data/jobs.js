import { computed, reactive, ref } from 'vue'
import { apiBase, getJson, postJson } from './api'
import { isDirectorySource } from './integrations'
import { hostName } from './hosts'

// Jobs of this machine and of every linked remote host, keyed `jkey(host, id)`: a
// job id is unique per daemon, not across machines. A remote job runs on its own
// machine and is read and controlled through the hub's forward.
export const jobs = reactive({
  records: {},
  loaded: false,
  loading: false,
  error: null,
})

export const jkey = (host, id) => (host ? `${host}/${id}` : id)
const enc = encodeURIComponent
export const jobPath = (job) => (job?.host ? `/hosts/${enc(job.host)}/jobs/${enc(job.id)}` : `/jobs/${enc(job?.id || '')}`)
export const jobRecord = (id, host = null) => jobs.records[jkey(host, id)] || null

export const jobList = computed(() =>
  Object.values(jobs.records).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))),
)

export const activeJobs = computed(() =>
  jobList.value.filter((job) => ['queued', 'running', 'interrupting'].includes(job.state)),
)

export function applyJob(record, host = null) {
  if (!record?.id) return
  const key = jkey(host, record.id)
  if (record.removed) delete jobs.records[key]
  else jobs.records[key] = { ...(jobs.records[key] || {}), ...record, host: host || null, key }
}

// Replace one machine's jobs (this one's when host is null) and keep the others'.
export function seedJobs(records = [], host = null) {
  const kept = Object.fromEntries(Object.entries(jobs.records).filter(([, job]) => (job.host || null) !== (host || null)))
  for (const job of records) {
    const key = jkey(host, job.id)
    kept[key] = { ...(jobs.records[key] || {}), ...job, host: host || null, key }
  }
  jobs.records = kept
  jobs.loaded = true
}

export function dropHostJobs(host) {
  seedJobs([], host)
}

// Keep this machine's jobs and those of the listed hosts only (the stream's snapshot
// lists every host, so a host it leaves out was removed while the stream was down).
export function keepHostJobs(ids) {
  jobs.records = Object.fromEntries(Object.entries(jobs.records).filter(([, job]) => !job.host || ids.has(job.host)))
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

export async function loadJob(id, after = 0, limit = null, host = null) {
  const record = await getJson(`${apiBase(host)}/jobs/${enc(id)}?after=${after}${limit != null ? `&limit=${limit}` : ''}`)
  const prior = jobRecord(id, host)
  // Refreshes can overlap with the same cursor, so append only events newer than the last one held.
  const held = after && prior?.events ? prior.events : []
  const lastSeq = held.at(-1)?.seq || 0
  const events = [...held, ...(record.events || []).filter((event) => event.seq > lastSeq)]
  applyJob({ ...record, events }, host)
  return jobRecord(id, host)
}

export async function createJob(spec, host = null) {
  const record = await postJson(`${apiBase(host)}/jobs`, spec)
  applyJob(record, host)
  return { ...record, host: host || null }
}

export async function interruptJob(id, host = null) {
  const record = await postJson(`${apiBase(host)}/jobs/${enc(id)}/interrupt`, {})
  applyJob(record, host)
  return record
}

export async function retryJob(id, host = null) {
  const record = await postJson(`${apiBase(host)}/jobs/${enc(id)}/retry`, {})
  applyJob(record, host)
  return { ...record, host: host || null }
}

export async function continueJob(id, { target, args, consent }, host = null) {
  const record = await postJson(`${apiBase(host)}/jobs/${enc(id)}/continue`, { target, args, consent })
  applyJob(record, host)
  return { ...record, host: host || null }
}

export async function detachJob(id, host = null) {
  const record = await postJson(`${apiBase(host)}/jobs/${enc(id)}/detach`, {})
  applyJob(record, host)
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
  answered: 'gray',
}

export const JOB_LABEL = {
  backup: 'Back up site',
  build: 'Build assets',
  'apps-checkout': 'Check out app ref',
  'apps-install': 'Install apps',
  'apps-update': 'Update apps',
  'check-updates': 'Check for updates',
  'clear-maintenance': 'Clear maintenance mode',
  'dev-mode': 'Developer mode',
  'new-site': 'New site',
  'new-bench': 'New bench',
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
  'restore-set': 'Restore backup',
  'verify-backup': 'Verify backup',
  'delete-backup': 'Delete backup',
  'clone-sites': 'Clone sites',
  'move-sites': 'Move sites',
  'send-backup': 'Send backup',
  'receive-backup': 'Receive backup',
  'prune-backups': 'Prune backups',
  'publish-site': 'Publish site',
  'unpublish-site': 'Unpublish site',
  'backup-sites': 'Scheduled backup',
  's3-copy': 'Copy backup off-site',
  's3-fetch': 'Fetch backup from S3',
  's3-prune': 'Prune off-site copies',
  'restore-drill': 'Restore drill',
  'repair-db-access': 'Repair database access',
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

// "<target> on <host>" for a remote job; the bare target for this machine's.
export function jobWhere(job) {
  return job?.host ? `${jobTarget(job)} on ${hostName(job.host)}` : jobTarget(job)
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

// Who started a job: the signed-in account, "schedule" for the daemon's own
// scheduled runs, "api" for a bearer-token client, "cli" for the terminal, and
// `<user>@<hub>` when a hub's user started it on this machine.
export function jobAccount(job) {
  const o = job?.origin || {}
  if (o.account) return isDirectorySource(o.account_source) && o.account_source !== 'hub' ? `${o.account} (directory)` : o.account
  if (o.schedule) return 'schedule'
  if (o.via === 'web') return 'api'
  return o.via || 'cli'
}
