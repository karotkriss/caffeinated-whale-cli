// The apps catalog: every Frappe app installed on an instance or reachable on a
// signed-in forge, with same-origin logos.
//
//   GET  /api/v1/apps[?installed=fresh]  core.app_catalog (installed + remote)
//   POST /api/v1/apps/scan               rescan the forges in the background
//   SSE  event: catalog                  a scan landed; re-read
import { computed, reactive } from 'vue'
import { getJson, postJson } from './api'

export const catalog = reactive({
  data: null, // { scanned_at, scanning, sources, apps, installed_read_at, warnings }
  loading: false,
  error: null,
  scanning: false,
  progress: null, // { host, done, total } while a scan runs
})

export async function loadCatalog({ fresh = false } = {}) {
  if (catalog.loading) return catalog.data
  catalog.loading = true
  try {
    catalog.data = await getJson(`/api/v1/apps${fresh ? '?installed=fresh' : ''}`)
    catalog.scanning = catalog.data.scanning
    catalog.error = null
  } catch (e) {
    catalog.error = e
  } finally {
    catalog.loading = false
  }
  return catalog.data
}

export async function scanCatalog() {
  catalog.scanning = true
  try {
    await postJson('/api/v1/apps/scan', {})
  } catch (e) {
    catalog.scanning = false
    throw e
  }
}

// Called by the fleet stream on a `catalog` frame: a source the scan found to
// answer, a progress tick, or the landing. A source is listed the moment it
// answers (a first scan has no row on disk yet), and the tick rides on its row,
// so the sidebar and the Sources card show "100 of 442" while the scan runs.
export function catalogLanded(frame = {}) {
  if (frame.source) {
    catalog.scanning = true
    upsertSource(frame.source)
    return
  }
  if (frame.progress) {
    catalog.scanning = true
    catalog.progress = frame.progress
    const row = sources.value.find((s) => s.host === frame.progress.host)
    if (row) row.progress = { done: frame.progress.done, total: frame.progress.total }
    else loadCatalog() // the row is on the daemon already; a page that missed the source frame re-reads
    return
  }
  catalog.scanning = false
  catalog.progress = null
  loadCatalog({ fresh: true })
}

function upsertSource(source) {
  if (!catalog.data) return
  const rows = catalog.data.sources || (catalog.data.sources = [])
  const i = rows.findIndex((s) => s.host === source.host)
  if (i < 0) rows.push(source)
  else rows.splice(i, 1, { ...rows[i], ...source, progress: rows[i].progress ?? source.progress })
}

export const apps = computed(() => catalog.data?.apps || [])
// The catalog app an installed-app NAME most likely means: the one installed somewhere wins.
const RANK = { both: 0, installed: 1, remote: 2 }
export const appsByName = computed(() => {
  const out = {}
  // Installed-ness ranks first; among equals an entry WITH a logo wins (a list row
  // named by app name must not show a letter when a sibling entry has the logo).
  const better = (a, b) => !b || RANK[a.state] < RANK[b.state] || (RANK[a.state] === RANK[b.state] && !!a.logo && !b.logo)
  for (const a of apps.value) if (better(a, out[a.name])) out[a.name] = a
  return out
})
export const catalogApp = (name) => appsByName.value[name] || { name, title: name, logo: null }
// The catalog entry the copy installed on THIS instance and bench came from (captain
// note 53: a site's source column must name where that install came from, not the
// first entry of that name); the plain name lookup when the catalog knows no install.
export function catalogAppFor(name, project, benchIndex) {
  const hit = apps.value.find((a) => a.name === name && (a.installs || []).some((i) => i.project === project && (benchIndex == null || String(i.bench_index) === String(benchIndex))))
  return hit || catalogApp(name)
}
export const sources = computed(() => catalog.data?.sources || [])
// Sources that actually answer (captain note 02:23Z: unauthenticated ones are not shown
// outside Settings > App sources, which is where sign-in is explained).
export const activeSources = computed(() => sources.value.filter((s) => s.signed_in))

export const STATE_LABEL = { both: 'Installed · remote', installed: 'Installed', remote: 'Remote' }
export const STATE_THEME = { both: 'green', installed: 'blue', remote: 'gray' }

export function sourceLabel(app) {
  const s = app.source
  if (!s) return 'local'
  if (s.kind === 'marketplace') return 'Marketplace'
  if (s.kind === 'github') return 'GitHub'
  return s.host
}
// The GitLab group (or subgroup path) / the GitHub organisation or user that owns the repo.
export function namespaceOf(app) {
  const path = app.source?.path || ''
  const i = path.lastIndexOf('/')
  return i > 0 ? path.slice(0, i) : ''
}
export const hostLabel = (host) => (host === 'github.com' ? 'GitHub' : host === 'marketplace' ? 'Marketplace' : host)
// "100 of 442" while a source's scan runs, else null.
export const progressText = (s) => (s?.progress ? `${s.progress.done} of ${s.progress.total}` : null)
// Every category the registry names, for the grid's filter.
export const categories = computed(() => [...new Set(apps.value.flatMap((a) => a.source?.categories || []))].sort())

// The host's cached favicon URL, from the sources the daemon scanned.
export function hostIcon(host) {
  if (!host) return null
  return (sources.value.find((s) => s.host === host) || {}).icon?.url || null
}

export function sourceIcon(app) {
  const kind = app.source?.kind
  if (kind === 'github') return 'lucide-github'
  if (kind === 'gitlab') return 'lucide-gitlab'
  return 'lucide-hard-drive'
}

// Summaries of where an app is installed: distinct instances, benches, sites.
export function installSummary(app) {
  const installs = app.installs || []
  const instances = new Set(installs.map((i) => i.project))
  const sites = installs.reduce((n, i) => n + (i.sites?.length || 0), 0)
  return { instances: instances.size, benches: installs.length, sites }
}

export function installText(app) {
  const { instances, sites } = installSummary(app)
  if (!instances) return null
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`
  return `${plural(instances, 'instance')} · ${plural(sites, 'site')}`
}

// A stable letter + hue for an app without a logo (Pilot's AppIcon fallback).
export function letterTile(name) {
  let h = 0
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  const hues = ['bg-surface-blue-2 text-ink-blue-7', 'bg-surface-green-2 text-ink-green-7', 'bg-surface-amber-2 text-ink-amber-7', 'bg-surface-red-2 text-ink-red-7', 'bg-surface-violet-2 text-ink-violet-7', 'bg-surface-cyan-2 text-ink-cyan-7', 'bg-surface-pink-2 text-ink-pink-7', 'bg-surface-gray-3 text-ink-gray-7']
  return { letter: String(name || '?').charAt(0).toUpperCase(), cls: hues[h % hues.length] }
}

// The one filter function every grid shares.
export function filterApps(list, { query = '', source = 'all', state = 'all', project = 'all', category = 'all', official = false, updatesOnly = null } = {}) {
  const q = query.trim().toLowerCase()
  return list.filter((a) => {
    if (state !== 'all' && a.state !== state) return false
    if (official && !a.official) return false
    if (updatesOnly && !updatesOnly.has(a.key)) return false
    if (category !== 'all' && !(a.source?.categories || []).includes(category)) return false
    if (source !== 'all') {
      const host = a.source?.kind === 'marketplace' ? 'marketplace' : a.source?.host || 'local'
      if (source === 'installed' ? a.state === 'remote' : host !== source) return false
    }
    if (project !== 'all' && !(a.installs || []).some((i) => i.project === project)) return false
    if (q) {
      const hay = [a.name, a.title, a.description, a.source?.path, a.key, ...(a.source?.categories || []), ...(a.installs || []).map((i) => i.project)].join(' ').toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })
}

export function ago(ts) {
  if (!ts) return 'never'
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}
