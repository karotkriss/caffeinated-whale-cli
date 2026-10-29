import { reactive } from 'vue'
import { getJson } from './api'

// Rolling performance history from the daemon (core.metrics): fetched once per
// instance, then appended from the `metrics` SSE frame every probe tick. Only
// real measurements: a stopped instance has no points for the time it was down.
export const metrics = reactive({ host: [], projects: {}, loaded: {}, running: {} })
const MAX = 1440

export async function loadMetrics(project) {
  const data = await getJson(`/api/v1/metrics${project ? `?project=${encodeURIComponent(project)}` : ''}`)
  metrics.host = data.host || []
  if (project) {
    metrics.projects[project] = data.points || []
    metrics.running[project] = !!data.running
    metrics.loaded[project] = true
  } else {
    for (const [p, points] of Object.entries(data.projects || {})) {
      metrics.projects[p] = points
      metrics.loaded[p] = true
    }
  }
  return data
}

export function applyMetricsFrame(frame) {
  if (frame.host) push(metrics.host, frame.host)
  for (const [p, point] of Object.entries(frame.projects || {})) {
    if (!metrics.projects[p]) metrics.projects[p] = []
    push(metrics.projects[p], point)
    metrics.running[p] = true
  }
}

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
    memAvailable: [{ name: 'available', points: points.map((p) => [p.ts * 1000, p.mem_available ?? null]) }],
    // The host chart shows memory USED: total minus available, from the same /proc point.
    memUsed: [{ name: 'used', points: points.map((p) => [p.ts * 1000, (p.mem_total != null && p.mem_available != null ? p.mem_total - p.mem_available : null) ?? null]) }],
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
