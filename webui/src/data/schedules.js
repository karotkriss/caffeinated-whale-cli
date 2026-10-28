// Scheduled jobs: the store's view (schedules, upcoming runs, windows, guards) and
// the few writes. Every scheduled run is an ordinary job (origin.schedule = id).
import { computed, reactive } from 'vue'
import { getJson, postJson } from './api'
import { JOB_LABEL } from './jobs'

export const schedules = reactive({ data: null, loading: false, error: null })

export async function loadSchedules() {
  schedules.loading = true
  try {
    schedules.data = await getJson('/api/v1/schedules')
    schedules.error = null
  } catch (e) {
    schedules.error = e
  } finally {
    schedules.loading = false
  }
}

async function post(payload) {
  const data = await postJson('/api/v1/schedules', payload)
  await loadSchedules()
  return data
}

export const saveSchedule = (schedule) => post({ op: 'save', schedule })
export const deleteSchedule = (id) => post({ op: 'delete', id })
export const enableSchedule = (id, enabled) => post({ op: 'enable', id, enabled })
export const runSchedule = (id) => post({ op: 'run', id })
export const saveScheduleSettings = (settings) => post({ op: 'settings', ...settings })

export const list = computed(() => schedules.data?.schedules || [])
export const upcoming = computed(() => schedules.data?.upcoming || [])
export const templates = computed(() => schedules.data?.templates || [])

// The verbs a schedule may run, with what each needs (mirrors core.schedules.SCHEDULABLE);
// `admin` ones (core.accounts.admin_only) only an Admin may schedule.
export const VERBS = [
  { value: 'backup-sites', label: 'Back up every site', need: 'project' },
  { value: 'backup', label: 'Back up a site', need: 'site' },
  { value: 'prune-backups', label: 'Prune backups (retention)', need: 'project', admin: true },
  { value: 's3-copy', label: 'Copy backups off-site', need: 'project' },
  { value: 's3-prune', label: 'Prune off-site backups', need: 'project', admin: true },
  { value: 'restore-drill', label: 'Restore drill', need: 'site' },
  { value: 'apps-update', label: 'Update apps (core or app)', need: 'bench', flag: true },
  { value: 'migrate', label: 'Migrate a site', need: 'site', flag: true },
  { value: 'restart', label: 'Restart the instance', need: 'project' },
  { value: 'restart-process', label: 'Restart one process', need: 'process' },
  { value: 'start', label: 'Start the instance', need: 'project' },
  { value: 'stop', label: 'Stop the instance', need: 'project' },
  { value: 'stop-bench', label: 'Stop a bench', need: 'bench' },
  { value: 'dev-mode', label: 'Developer mode on/off', need: 'site' },
  { value: 'clear-maintenance', label: 'Take a site out of maintenance', need: 'site' },
  { value: 'build', label: 'Build assets', need: 'bench' },
]
export const verbLabel = (v) => (v === 'recipe' ? 'Recipe' : VERBS.find((x) => x.value === v)?.label || JOB_LABEL[v] || v)
export const verbNeed = (v) => VERBS.find((x) => x.value === v)?.need || 'project'
export const verbNeedsFlag = (v) => !!VERBS.find((x) => x.value === v)?.flag

export const DAYS = [
  { value: 1, short: 'Mon' }, { value: 2, short: 'Tue' }, { value: 3, short: 'Wed' }, { value: 4, short: 'Thu' },
  { value: 5, short: 'Fri' }, { value: 6, short: 'Sat' }, { value: 0, short: 'Sun' },
]

// The friendly picker -> a cron expression.
export function toCron(p) {
  const [h, m] = (p.time || '02:00').split(':').map((x) => parseInt(x, 10) || 0)
  if (p.mode === 'daily') return `${m} ${h} * * *`
  if (p.mode === 'weekly') return `${m} ${h} * * ${(p.days?.length ? p.days : [0]).slice().sort((a, b) => a - b).join(',')}`
  if (p.mode === 'monthly') return `${m} ${h} ${p.day || 1} * *`
  if (p.mode === 'hourly') return `${m} * * * *`
  if (p.mode === 'every') return `0 */${p.hours || 6} * * *`
  return p.cron || ''
}

// A cron expression -> the picker, when it has one of the picker's shapes.
export function fromCron(cron) {
  const parts = (cron || '').trim().split(/\s+/)
  if (parts.length !== 5) return { mode: 'custom', cron }
  const [m, h, dom, mon, dow] = parts
  const time = /^\d+$/.test(h) && /^\d+$/.test(m) ? `${h.padStart(2, '0')}:${m.padStart(2, '0')}` : null
  if (time && dom === '*' && mon === '*' && dow === '*') return { mode: 'daily', time }
  if (time && dom === '*' && mon === '*' && /^[\d,]+$/.test(dow)) return { mode: 'weekly', time, days: dow.split(',').map(Number) }
  if (time && mon === '*' && dow === '*' && /^\d+$/.test(dom)) return { mode: 'monthly', time, day: Number(dom) }
  if (/^\d+$/.test(m) && h === '*' && dom === '*' && mon === '*' && dow === '*') return { mode: 'hourly', time: `00:${m.padStart(2, '0')}` }
  if (m === '0' && /^\*\/\d+$/.test(h) && dom === '*' && mon === '*' && dow === '*') return { mode: 'every', hours: Number(h.slice(2)) }
  return { mode: 'custom', cron }
}

export const whenAbs = (iso) => (iso ? new Date(iso).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '-')
export function whenRel(iso) {
  if (!iso) return 'not scheduled'
  const ms = new Date(iso) - Date.now()
  const abs = Math.abs(ms)
  const unit = abs < 3600e3 ? [Math.round(abs / 60e3), 'min'] : abs < 86400e3 ? [Math.round(abs / 3600e3), 'h'] : [Math.round(abs / 86400e3), 'd']
  return ms >= 0 ? `in ${unit[0]} ${unit[1]}` : `${unit[0]} ${unit[1]} ago`
}
