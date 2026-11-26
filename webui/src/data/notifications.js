// Notifications with read state (captain note 02:14Z): failed jobs, jobs waiting on a
// decision and non-passing health checks, each with a stable id; what was read is
// remembered in this browser, the panel shows unread only, the badge counts unread.
import { computed, reactive } from 'vue'
import { advisories } from './advisories'
import { doctor } from './fleet'
import { jobList, jobLabel, jobPath, jobWhere } from './jobs'
import { schedules } from './schedules'

const KEY = 'cwcli.notifications.read'
const state = reactive({ read: new Set(JSON.parse(localStorage.getItem(KEY) || '[]')) })
const persist = () => localStorage.setItem(KEY, JSON.stringify([...state.read]))

export const notifications = computed(() => {
  const out = []
  for (const j of jobList.value.slice(0, 50)) {
    if (j.state === 'failed') out.push({ id: `job:${j.key || j.id}:failed`, kind: 'job', theme: 'red', title: `${jobLabel(j)} failed`, detail: jobWhere(j), to: jobPath(j), at: j.finished_at || j.created_at })
    if (j.state === 'needs_choice') out.push({ id: `job:${j.key || j.id}:needs_choice`, kind: 'job', theme: 'amber', title: `${jobLabel(j)} needs a decision`, detail: jobWhere(j), to: jobPath(j), at: j.finished_at || j.created_at })
  }
  // Advisories that newly affect this machine (first seen after the daemon's first
  // pass, still unread): one notification each, highest severity first.
  const newIds = new Set(advisories.data?.new_affecting || [])
  for (const a of (advisories.data?.affects || []).filter((x) => newIds.has(x.id) && x.state === 'new').slice(0, 30)) {
    const hit = (a.matches || []).find((m) => m.status === 'affected')
    out.push({ id: `advisory:${a.id}`, kind: 'advisory', theme: ['critical', 'high'].includes(a.severity) ? 'red' : 'amber', title: `${a.severity} advisory: ${a.title}`, detail: hit ? `${hit.project || ''}${hit.bench_index != null ? ' bench ' + hit.bench_index : ''} · ${hit.name} ${hit.installed || ''}${hit.fix?.short ? ' · ' + hit.fix.short : ''}` : a.id, to: '/advisories', at: a.first_seen ? new Date(a.first_seen * 1000).toISOString() : null })
  }
  // The scheduled-runs digest (Schedules > Windows and guards): one line a day or a week.
  const digest = schedules.data?.digest
  if (digest && digest !== 'off') {
    const span = digest === 'daily' ? 86400e3 : 7 * 86400e3
    const since = Date.now() - span
    const runs = jobList.value.filter((j) => j.origin?.schedule && new Date(j.finished_at || j.created_at) >= since)
    if (runs.length) {
      const failed = runs.filter((j) => j.state === 'failed').length
      const ok = runs.filter((j) => j.state === 'succeeded').length
      const stamp = digest === 'daily' ? new Date().toDateString() : `w${Math.floor(Date.now() / (7 * 86400e3))}`
      out.push({ id: `digest:${digest}:${stamp}`, kind: 'digest', theme: failed ? 'amber' : 'gray', title: `Scheduled runs, last ${digest === 'daily' ? 'day' : 'week'}: ${ok} ok, ${failed} failed`, detail: `${runs.length} runs`, to: '/schedules', at: null })
    }
  }
  for (const c of doctor.data?.checks || []) {
    if (c.status !== 'pass') out.push({ id: `health:${c.id}:${c.status}`, kind: 'health', theme: c.status === 'fail' ? 'red' : 'amber', title: c.title, detail: c.detail, to: '/settings/health', at: null })
  }
  return out
})
export const unread = computed(() => notifications.value.filter((n) => !state.read.has(n.id)))
export const isRead = (n) => state.read.has(n.id)

export function markRead(n) {
  state.read.add(n.id)
  persist()
}
export function markAllRead() {
  for (const n of notifications.value) state.read.add(n.id)
  persist()
}
