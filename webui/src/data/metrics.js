import { reactive } from 'vue'
import { apiBase, getJson } from './api'

// Rolling performance history from the daemon (core.metrics): fetched once per
// instance, then appended from the `metrics` SSE frame every probe tick. Only
// real measurements: a stopped instance has no points for the time it was down.
// Instances are keyed `<host>/<project>` for a remote host (its daemon's frames
// arrive tagged with the host), the bare project for this machine; `host` is this
// machine's own series.
export const metrics = reactive({ host: [], projects: {}, loaded: {}, running: {} })
const MAX = 1440
const mkey = (host, project) => (host ? `${host}/${project}` : project)

export async function loadMetrics(project, host = null) {
  const data = await getJson(`${apiBase(host)}/metrics${project ? `?project=${encodeURIComponent(project)}` : ''}`)
  if (!host) metrics.host = data.host || []
  if (project) {
    const key = mkey(host, project)
    metrics.projects[key] = data.points || []
    metrics.running[key] = !!data.running
    metrics.loaded[key] = true
  } else {
    for (const [p, points] of Object.entries(data.projects || {})) {
      metrics.projects[mkey(host, p)] = points
      metrics.loaded[mkey(host, p)] = true
    }
  }
  return data
}

export function applyMetricsFrame(frame, host = null) {
  if (frame.host && !host) push(metrics.host, frame.host)
  for (const [p, point] of Object.entries(frame.projects || {})) {
    const key = mkey(host, p)
    if (!metrics.projects[key]) metrics.projects[key] = []
    push(metrics.projects[key], point)
    metrics.running[key] = true
  }
}

export const metricsFor = (project, host = null) => metrics.projects[mkey(host, project)] || []

function push(list, point) {
  list.push(point)
  if (list.length > MAX) list.splice(0, list.length - MAX)
}

// --- series builders (each returns [{ name, points: [[ms, value], ...] }]) ---

export const CONTAINER_ORDER = ['frappe', 'mariadb', 'redis-cache', 'redis-queue']

export function containerSeries(points, field) {
  const names = new Set()
  for (const p of points) for (const n of Object.keys(p.containers || {})) names.add(n)
  return [...names].sort((a, b) => CONTAINER_ORDER.indexOf(a) - CONTAINER_ORDER.indexOf(b)).map((name) => ({
    name,
    points: points.map((p) => [p.ts * 1000, p.containers?.[name]?.[field] ?? null]),
  }))
}

export function processSeries(points, benchIndex, field) {
  const names = new Set()
  for (const p of points) for (const n of Object.keys(p.benches?.[benchIndex]?.processes || {})) names.add(n)
  return [...names].sort().map((name) => ({
    name,
    points: points.map((p) => [p.ts * 1000, p.benches?.[benchIndex]?.processes?.[name]?.[field] ?? null]),
  }))
}

export function queueSeries(points) {
  return ['default', 'short', 'long'].map((q) => ({ name: q, points: points.map((p) => [p.ts * 1000, p.queues?.[q] ?? null]) }))
}

export function sumSeries(points, field, name) {
  return [{ name, points: points.map((p) => [p.ts * 1000, Object.values(p.containers || {}).reduce((t, c) => (c[field] == null ? t : t + c[field]), null)]) }]
}

export function hostSeries(points) {
  return {
    // The host chart shows memory USED: total minus available, from the same /proc point.
    memUsed: [{ name: 'used', points: points.map((p) => [p.ts * 1000, p.mem_total != null && p.mem_available != null ? p.mem_total - p.mem_available : null]) }],
    load: [{ name: 'load (1m)', points: points.map((p) => [p.ts * 1000, p.load1 ?? null]) }],
    memTotal: points.length ? points[points.length - 1].mem_total : null,
    cpus: points.length ? points[points.length - 1].cpus : null,
  }
}

export function latest(points) {
  return points.length ? points[points.length - 1] : null
}

export function fmtBytes(v) {
  if (v == null) return '–'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v < 10 && i ? v.toFixed(1) : Math.round(v)} ${u[i]}`
}
export const fmtPct = (v) => (v == null ? '–' : `${v.toFixed(v < 10 ? 1 : 0)}%`)
export const fmtRate = (v) => (v == null ? '–' : `${fmtBytes(v)}/s`)
export const fmtCount = (v) => (v == null ? '–' : String(Math.round(v)))
