import { computed, reactive } from 'vue'
import { getJson, postJson } from './api'

// Updates available for installed apps (core.updates): detection only. One item per
// (instance, bench, app) on a running instance; core apps (frappe, erpnext) first.
export const updates = reactive({ data: null, loading: false, error: null })

export async function loadUpdates() {
  updates.loading = true
  try {
    updates.data = await getJson('/api/v1/updates')
    updates.error = null
  } catch (e) {
    updates.error = e
  } finally {
    updates.loading = false
  }
}
export async function checkUpdates() {
  await postJson('/api/v1/updates/check', {})
  if (updates.data) updates.data.checking = true
}
export function updatesLanded(frame) {
  updates.data = frame
}

export const updateItems = computed(() => (updates.data?.items || []).filter((it) => it.update_available))
export const updateCount = computed(() => updates.data?.summary?.total || 0)
export const updateAppNames = computed(() => new Set(updateItems.value.map((it) => it.app)))
export function updatesFor(project, benchIndex = null) {
  return updateItems.value.filter((it) => it.project === project && (benchIndex == null || String(it.bench_index) === String(benchIndex)))
}
export function updateItem(project, benchIndex, app) {
  return (updates.data?.items || []).find((it) => it.project === project && String(it.bench_index) === String(benchIndex) && it.app === app) || null
}
// By NAME alone every catalog entry called frappe (forks, other benches) would light up;
// an app object is matched on the places it is actually installed.
export function updatesForApp(app) {
  if (typeof app === 'string') return updateItems.value.filter((it) => it.app === app)
  const places = new Set((app.installs || []).map((i) => `${i.project}|${i.bench_index}`))
  return updateItems.value.filter((it) => it.app === app.name && places.has(`${it.project}|${it.bench_index}`))
}
// One line a human reads: "16.53.0 → v16.55.0 · 12 commits behind".
export function updateText(it) {
  const parts = []
  if (it.release?.release_available) parts.push(`${it.installed_version || '?'} → ${it.release.latest_same_major}`)
  if (it.behind) parts.push(it.commits_behind == null ? `behind ${it.branch || 'its branch'}` : `${it.commits_behind} ${it.commits_behind === 1 ? 'commit' : 'commits'} behind`)
  return parts.join(' · ')
}
export const short = (sha) => (sha ? sha.slice(0, 7) : '')
