// Per-chart settings remembered per browser (localStorage, one key per chart),
// with the defaults in one place so Reset means the same thing everywhere.
import { reactive, watch } from 'vue'

export const PREFIX = 'cwcli:chart:'
export const RANGES = [
  { label: '5 min', value: 300 },
  { label: '15 min', value: 900 },
  { label: '1 h', value: 3600 },
]

export function defaultSettings(kind, overrides = {}) {
  return {
    range: 900, // seconds shown
    hidden: [], // series names switched off
    stacked: !!overrides.stacked,
    unit: kind === 'percent' ? 'percent' : kind === 'bytes' ? 'auto' : 'auto', // percent | cores | auto | mb | gb
    yFixed: false, // percent charts: 0-100 instead of auto
    paused: false,
  }
}

function read(key) {
  try {
    return JSON.parse(localStorage.getItem(PREFIX + key) || 'null')
  } catch {
    return null
  }
}

// A reactive settings object for `key`, merged over the defaults and written back
// on every change. `paused` is deliberately not remembered: a reload resumes.
export function useChartSettings(key, kind, overrides = {}) {
  const defaults = defaultSettings(kind, overrides)
  const stored = key ? read(key) : null
  const settings = reactive({ ...defaults, ...(stored || {}), paused: false })
  if (key) {
    watch(
      settings,
      (s) => {
        const { paused, ...rest } = s
        try {
          localStorage.setItem(PREFIX + key, JSON.stringify(rest))
        } catch {
          // storage full or unavailable: the setting still applies for this page
        }
      },
      { deep: true },
    )
  }
  function reset() {
    Object.assign(settings, defaults, { paused: settings.paused })
    if (key) {
      try {
        localStorage.removeItem(PREFIX + key)
      } catch {
        // ignore
      }
    }
  }
  function isDefault() {
    const { paused, ...rest } = settings
    return JSON.stringify(rest) === JSON.stringify(defaults)
  }
  return { settings, reset, isDefault }
}

// CSV of the visible series: one row per timestamp, one column per series.
export function toCsv(series) {
  const names = series.map((s) => s.name)
  const rows = new Map()
  for (const s of series) for (const [t, v] of s.points) {
    if (!rows.has(t)) rows.set(t, {})
    rows.get(t)[s.name] = v
  }
  const esc = (x) => (/[",\n]/.test(String(x)) ? `"${String(x).replace(/"/g, '""')}"` : String(x))
  const lines = [['time', ...names].map(esc).join(',')]
  for (const t of [...rows.keys()].sort((a, b) => a - b)) {
    const r = rows.get(t)
    lines.push([new Date(t).toISOString(), ...names.map((n) => (r[n] == null ? '' : r[n]))].map(esc).join(','))
  }
  return lines.join('\n') + '\n'
}
