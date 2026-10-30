// The live fleet model and the cache-backed detail per instance, for this machine
// and for every remote host the hub is linked to.
//
//   GET /api/v1/stream?focus=<p>     SSE: one `snapshot`, then `delta` events; the
//                                    remote hosts' frames ride the same stream as
//                                    host_snapshot / host_delta / host_job / host_link
//   GET /api/v1/instances/<p>        cache-backed core.inspect (benches, sites, apps)
//   GET /api/v1/hosts/<h>/instances/<p>   the same read on a remote host, forwarded
//
// Two machines can each have an instance called `test555`, so everything here is
// keyed by `ikey(host, project)`: the bare project for this machine, `<host>/<project>`
// for a remote one, and every instance object carries `host` (null for local).
//
// One stream per tab. HTTP/1.1 browsers allow 6 connections per host and an
// open SSE stream holds one for as long as it lives, so a stream a hidden page
// keeps open stalls every later request: close on pagehide, reopen on pageshow.
import { reactive, computed, watch } from 'vue'
import { apiBase, getJson } from './api'
import { session, recheckMeta } from './session'
import { applyJob, seedJobs, dropHostJobs, keepHostJobs } from './jobs'
import { catalogLanded } from './catalog'
import { applyMetricsFrame } from './metrics'
import { siteIconsLanded } from './site-icons'
import { updatesLanded } from './updates'
import { advisoriesLanded } from './advisories'
import { backupsLanded } from './backups'
import { loadPublications, publicationsLanded } from './publish'
import { replaceHosts, applyHostLink, dropHost, hostName } from './hosts'

export const fleet = reactive({
  instances: {},
  connection: 'connecting', // connecting | live | reconnecting
  lastEventAt: null,
  focus: null,
})

export const ikey = (host, project) => (host ? `${host}/${project}` : project)
const tag = (inst, host) => ({ ...inst, host: host || null, key: ikey(host, inst.project) })

let source = null

// The stream needs a usable session: signed in and past the forced first
// password change (the daemon refuses everything else with 401/403). Called once
// at boot; the watch opens it on a later sign-in or change and closes it on sign-out.
const usable = () => !session.signedOut && !!session.user && !session.user.mustChange

export function startFleet() {
  window.addEventListener('pagehide', () => source?.close())
  window.addEventListener('pageshow', (e) => e.persisted && usable() && openStream())
  watch(usable, (ok) => (ok ? openStream() : closeStream()), { immediate: true })
}

function closeStream() {
  source?.close()
  source = null
}

// Which instance the open page is looking at: the daemon turns that instance's
// web probe on while a stream carries it (core.fleet.Fleet.set_focus). Only this
// machine's instances are probed this way; a remote host's daemon runs its own.
export function setFocus(project) {
  const next = project || null
  if (next === fleet.focus && source) return
  fleet.focus = next
  if (source) openStream()
}

function replaceHost(host, instances) {
  for (const key of Object.keys(fleet.instances)) if (fleet.instances[key].host === host) delete fleet.instances[key]
  for (const i of instances || []) fleet.instances[ikey(host, i.project)] = tag(i, host)
}

function openStream() {
  source?.close()
  const url = fleet.focus ? `/api/v1/stream?focus=${encodeURIComponent(fleet.focus)}` : '/api/v1/stream'
  source = new EventSource(url)
  source.addEventListener('snapshot', (e) => {
    const body = JSON.parse(e.data)
    if (fleet.connection === 'reconnecting') recheckMeta()
    const next = Object.fromEntries(body.instances.map((i) => [i.project, tag(i, null)]))
    const listed = body.hosts || []
    replaceHosts(listed)
    keepHostJobs(new Set(listed.map((h) => h.id)))
    for (const h of listed) {
      for (const i of h.instances || []) next[ikey(h.id, i.project)] = tag(i, h.id)
      seedJobs(h.jobs || [], h.id)
    }
    fleet.instances = next
    seedJobs(body.jobs || [])
    fleet.connection = 'live'
    fleet.lastEventAt = Date.now()
    for (const i of Object.values(next)) loadDetail(i.project, { host: i.host })
    loadPublications() // the stream then carries each change (`publications`)
  })
  source.addEventListener('delta', (e) => {
    const body = JSON.parse(e.data)
    if (body.removed) delete fleet.instances[body.project]
    else fleet.instances[body.project] = tag(body.instance, null)
    fleet.lastEventAt = Date.now()
  })
  // --- remote hosts: the same frames, tagged with the host that produced them ---
  source.addEventListener('host_snapshot', (e) => {
    const body = JSON.parse(e.data)
    replaceHost(body.host, body.instances)
    if (body.jobs) seedJobs(body.jobs, body.host)
    if (!body.stale) for (const i of body.instances || []) loadDetail(i.project, { host: body.host })
  })
  source.addEventListener('host_delta', (e) => {
    const body = JSON.parse(e.data)
    const key = ikey(body.host, body.project)
    if (body.removed) delete fleet.instances[key]
    else fleet.instances[key] = tag(body.instance, body.host)
  })
  source.addEventListener('host_job', (e) => {
    const { host, ...record } = JSON.parse(e.data)
    applyJob(record, host)
  })
  source.addEventListener('host_link', (e) => applyHostLink(JSON.parse(e.data)))
  source.addEventListener('host_removed', (e) => {
    const { host } = JSON.parse(e.data)
    replaceHost(host, [])
    dropHostJobs(host)
    dropHost(host)
  })
  source.addEventListener('host_event', (e) => {
    const { host, event, data } = JSON.parse(e.data)
    if (event === 'metrics') applyMetricsFrame(data, host)
    else if (event === 'site_icons') siteIconsLanded(data, host)
    else if (event === 'backups') backupsLanded(data, host)
  })
  source.addEventListener('job', (e) => applyJob(JSON.parse(e.data)))
  source.addEventListener('catalog', (e) => catalogLanded(JSON.parse(e.data)))
  source.addEventListener('metrics', (e) => applyMetricsFrame(JSON.parse(e.data)))
  source.addEventListener('site_icons', (e) => siteIconsLanded(JSON.parse(e.data)))
  source.addEventListener('updates', (e) => updatesLanded(JSON.parse(e.data)))
  source.addEventListener('advisories', (e) => advisoriesLanded(JSON.parse(e.data)))
  source.addEventListener('backups', (e) => backupsLanded(JSON.parse(e.data)))
  // This machine's publications only: a remote host's are its own daemon's store.
  source.addEventListener('publications', (e) => publicationsLanded(JSON.parse(e.data)))
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

// This machine's instances first, then each remote host's, each group by name.
export const instances = computed(() =>
  Object.values(fleet.instances).sort((a, b) => {
    if (!!a.host !== !!b.host) return a.host ? 1 : -1
    if (a.host !== b.host) return hostName(a.host).localeCompare(hostName(b.host))
    return a.project.localeCompare(b.project)
  }),
)
export const localInstances = computed(() => instances.value.filter((i) => !i.host))

export function instance(project, host = null) {
  return fleet.instances[ikey(host, project)] || null
}

// --- routes -----------------------------------------------------------------------
// A remote instance's pages live under /hosts/<host>/..., so a deep link names the
// machine as well as the project.

const enc = encodeURIComponent
export const instancePath = ({ project, host = null }) => (host ? `/hosts/${enc(host)}/instances/${enc(project)}` : `/instances/${enc(project)}`)
export const benchPath = (i, bench) => `${instancePath(i)}/benches/${enc(bench)}`
export const sitePath = (i, bench, site) => `${benchPath(i, bench)}/sites/${enc(site)}`

// --- lazy detail (core.inspect, cache-backed) ------------------------------------

export const details = reactive({})

export const detailOf = (project, host = null) => details[ikey(host, project)] || null

export function loadDetail(project, { force = false, host = null } = {}) {
  if (!project) return null
  const key = ikey(host, project)
  if (!details[key]) details[key] = { loading: false, data: null, error: null }
  const entry = details[key] // the reactive proxy, not the raw object just assigned
  if ((entry.data || entry.loading) && !force) return entry
  entry.loading = true
  getJson(`${apiBase(host)}/instances/${encodeURIComponent(project)}`)
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
export async function getJsonUrl(project, query, host = null) {
  return (await getJson(`${apiBase(host)}/instances/${encodeURIComponent(project)}/url?${query}`)).outcome
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
