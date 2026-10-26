// The live fleet model and the cache-backed detail per instance.
//
//   GET /api/v1/stream?focus=<p>   SSE: one `snapshot`, then `delta` events
//   GET /api/v1/instances/<p>      cache-backed core.inspect (benches, sites, apps)
//
// One stream per tab. HTTP/1.1 browsers allow 6 connections per host and an
// open SSE stream holds one for as long as it lives, so a stream a hidden page
// keeps open stalls every later request: close on pagehide, reopen on pageshow.
import { reactive, computed } from 'vue'
import { getJson } from './api'
import { session, recheckMeta, daemonUpdating } from './session'
import { applyJob, seedJobs } from './jobs'

export const fleet = reactive({
  instances: {},
  connection: 'connecting', // connecting | live | reconnecting
  lastEventAt: null,
  focus: null,
})

let source = null

export function startFleet() {
  if (session.signedOut) return
  openStream()
  window.addEventListener('pagehide', () => source?.close())
  window.addEventListener('pageshow', (e) => e.persisted && openStream())
}

// Which instance the open page is looking at: the daemon turns that instance's
// web probe on while a stream carries it (core.fleet.Fleet.set_focus).
export function setFocus(project) {
  const next = project || null
  if (next === fleet.focus && source) return
  fleet.focus = next
  if (source) openStream()
}

function openStream() {
  source?.close()
  const url = fleet.focus ? `/api/v1/stream?focus=${encodeURIComponent(fleet.focus)}` : '/api/v1/stream'
  source = new EventSource(url)
  source.addEventListener('snapshot', (e) => {
    const body = JSON.parse(e.data)
    if (fleet.connection === 'reconnecting') recheckMeta()
    fleet.instances = Object.fromEntries(body.instances.map((i) => [i.project, i]))
    seedJobs(body.jobs || [])
    fleet.connection = 'live'
    fleet.lastEventAt = Date.now()
    body.instances.forEach((i) => loadDetail(i.project))
  })
  source.addEventListener('delta', (e) => {
    const body = JSON.parse(e.data)
    if (body.removed) delete fleet.instances[body.project]
    else fleet.instances[body.project] = body.instance
    fleet.lastEventAt = Date.now()
  })
  source.addEventListener('job', (e) => applyJob(JSON.parse(e.data)))
  source.addEventListener('updating', () => daemonUpdating())
  // EventSource reconnects by itself; the daemon answers a reconnect with a
  // fresh snapshot, which is the resume. A 401 closes it for good.
  source.onerror = () => {
    if (session.signedOut) source?.close()
    else fleet.connection = 'reconnecting'
    // A 401 closes the stream without saying why; one authenticated read
    // surfaces it (api.js flips session.signedOut on a 401).
    if (source?.readyState === EventSource.CLOSED) getJson('/api/v1/instances').catch(() => {})
  }
}

// --- instances ------------------------------------------------------------------

export const instances = computed(() =>
  Object.values(fleet.instances).sort((a, b) => a.project.localeCompare(b.project)),
)

export function instance(project) {
  return fleet.instances[project] || null
}

// --- lazy detail (core.inspect, cache-backed) ------------------------------------

export const details = reactive({})

export function loadDetail(project, { force = false } = {}) {
  if (!project) return null
  if (!details[project]) details[project] = { loading: false, data: null, error: null }
  const entry = details[project] // the reactive proxy, not the raw object just assigned
  if ((entry.data || entry.loading) && !force) return entry
  entry.loading = true
  getJson(`/api/v1/instances/${encodeURIComponent(project)}`)
    .then((d) => {
      entry.data = d
      entry.error = null
    })
    .catch((e) => (entry.error = e))
    .finally(() => (entry.loading = false))
  return entry
}

// --- one-shot reads ---------------------------------------------------------------

function onceStore(url) {
  const store = reactive({ loading: false, data: null, error: null, at: null })
  store.load = (force = false) => {
    if ((store.data || store.loading) && !force) return store
    store.loading = true
    getJson(url)
      .then((d) => {
        store.data = d
        store.error = null
        store.at = Date.now()
      })
      .catch((e) => (store.error = e))
      .finally(() => (store.loading = false))
    return store
  }
  return store
}

export const doctor = onceStore('/api/v1/doctor')
export const config = onceStore('/api/v1/config')
export const cacheProjects = onceStore('/api/v1/cache')

// The route answers the action envelope; callers want the probe itself ({url, reachable, http_code}).
export async function getJsonUrl(project, query) {
  return (await getJson(`/api/v1/instances/${encodeURIComponent(project)}/url?${query}`)).outcome
}

export function search(term) {
  return getJson(`/api/v1/search?q=${encodeURIComponent(term)}`)
}

// --- presentation helpers ----------------------------------------------------------

export const HEALTH_THEME = {
  running: 'green',
  degraded: 'amber',
  online: 'blue',
  offline: 'gray',
  unknown: 'gray',
}

export const PROCESS_THEME = {
  RUNNING: 'green',
  STARTING: 'blue',
  BACKOFF: 'amber',
  FATAL: 'red',
  EXITED: 'gray',
  STOPPED: 'gray',
}

export function benchName(bench) {
  const path = bench.path || bench.bench_path || ''
  return bench.label || path.split('/').filter(Boolean).pop() || path
}

// "frappe        15.116.1 version-15" -> { name, version, branch }
export function parseApp(entry) {
  const [name, version, ...rest] = String(entry).trim().split(/\s+/)
  return { name, version: version || null, branch: rest.join(' ') || null }
}

// Web and socketio are two consecutive runs (P.. and P+1000..), whatever base
// `cwcli init --port` chose: split on the gap, the lower run is web.
export function publishedRange(ports) {
  const nums = [...new Set((ports || []).map(Number).filter((n) => !Number.isNaN(n)))].sort((a, b) => a - b)
  const runs = []
  for (const n of nums) {
    const run = runs[runs.length - 1]
    if (run && n === run[run.length - 1] + 1) run.push(n)
    else runs.push([n])
  }
  const [web = [], sock = []] = runs
  const span = (xs) => (xs.length ? `${Math.min(...xs)}-${Math.max(...xs)}` : null)
  return { web: span(web), socketio: span(sock), count: web.length }
}

export function countSites(detail) {
  return (detail?.benches || []).reduce((n, b) => n + (b.sites?.length || 0), 0)
}

export function ago(ts) {
  if (!ts) return 'never'
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

export function duration(seconds) {
  if (seconds == null) return 'unknown'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  const h = Math.floor(s / 3600)
  return `${h}h ${Math.floor((s % 3600) / 60)}m`
}
