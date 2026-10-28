import { computed, reactive } from 'vue'
import { getJson, postJson } from './api'

// The backup store outside the instances (core.backup_store): one snapshot of every set
// (newest first), the last good set per site with its age, the disk budget and each
// instance's settings. Refreshed by the `backups` SSE frame after any job that changes it.
export const backups = reactive({ data: null, loading: false, error: null })

export async function loadBackups({ project = null } = {}) {
  backups.loading = true
  try {
    backups.data = await getJson(`/api/v1/backups${project ? `?project=${encodeURIComponent(project)}` : ''}`)
    backups.error = null
  } catch (e) {
    backups.error = e
  } finally {
    backups.loading = false
  }
}
export function backupsLanded(frame) {
  backups.data = frame
}
export const backupSettingsDialog = reactive({ open: false, project: null })
export function openBackupSettings(project = null) { backupSettingsDialog.project = project; backupSettingsDialog.open = true }
export async function loadRetention(project) {
  return getJson(`/api/v1/backups/retention?project=${encodeURIComponent(project)}`)
}
export async function configureS3(project, values, secretKey) {
  return postJson('/api/v1/backups/s3', { project, values, ...(secretKey ? { secret_key: secretKey } : {}) })
}
export async function loadOffsite(project) {
  return getJson(`/api/v1/backups/offsite?project=${encodeURIComponent(project)}`)
}
// Download rides a plain fetch (the session cookie authenticates) and saves the tar.
export async function downloadSet(set) {
  const res = await fetch(`/api/v1/backups/download?key=${encodeURIComponent(set.key)}`)
  if (!res.ok) throw new Error(`download failed (HTTP ${res.status})`)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${set.project}__${set.site}__${set.id}.tar`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(a.href), 10000)
}
export async function uploadSet(file, { project = null, site = null } = {}) {
  const q = new URLSearchParams()
  if (project) q.set('project', project)
  if (site) q.set('site', site)
  const res = await fetch(`/api/v1/backups/upload${q.toString() ? '?' + q : ''}`, { method: 'POST', headers: { 'Content-Type': 'application/x-tar' }, body: file })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body?.errors?.[0]?.message || `upload failed (HTTP ${res.status})`)
  await loadBackups()
  return body.data
}
export function lastDrill(project, site) { return backups.data?.drills?.[`${project}/${site}`] || null }
export async function saveBackupSettings(project, values) {
  const data = await postJson('/api/v1/backups/settings', { project, values })
  if (backups.data?.settings) backups.data.settings[project] = data.settings
  return data.settings
}

export const allSets = computed(() => backups.data?.sets || [])
export function setsFor(project, site = null) {
  return allSets.value.filter((s) => s.project === project && (!site || s.site === site))
}
export function lastGood(project, site) {
  return backups.data?.last_good?.[`${project}/${site}`] || null
}
export function staleAfterDays(project) {
  return backups.data?.settings?.[project]?.stale_after_days ?? 7
}
// amber once the last good set is older than the instance's stale_after_days; red with none.
export function freshness(project, site) {
  const lg = lastGood(project, site)
  if (!lg) return { tone: 'red', label: 'no good backup', ageDays: null }
  const days = lg.age_days == null ? null : Math.floor(lg.age_days)
  const stale = days != null && days > staleAfterDays(project)
  return { tone: stale ? 'amber' : 'green', label: days == null ? 'backed up' : days === 0 ? 'backed up today' : `last good ${days}d ago`, ageDays: days, key: lg.key }
}
// Captain note 52: freshness is a dot and plain text, never a pill.
export const FRESH_DOT = { green: 'bg-surface-green-5', amber: 'bg-surface-amber-5', red: 'bg-surface-red-5' }
export const FRESH_TEXT = { green: 'text-ink-gray-7', amber: 'text-ink-amber-6', red: 'text-ink-red-6' }
export const REASON_LABEL = {
  manual: 'Manual',
  scheduled: 'Scheduled',
  'before-core-update': 'Before core update',
  'before-removal': 'Before removal',
  'before-migrate': 'Before migrate',
  'before-install': 'Before app install',
  'before-app-remove': 'Before app removal',
  drill: 'Restore drill',
  clone: 'For a clone',
  move: 'For a move',
  received: 'Received from another machine',
}
export const PART_LABEL = { database: 'database', public_files: 'public files', private_files: 'private files', site_config: 'config' }
export function sizeText(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`
  return `${(n / 1073741824).toFixed(2)} GB`
}
export function whenText(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins < 60) return `${Math.max(mins, 0)} min ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours} h ago`
  const days = Math.round(hours / 24)
  return days < 30 ? `${days} days ago` : d.toISOString().slice(0, 10)
}
