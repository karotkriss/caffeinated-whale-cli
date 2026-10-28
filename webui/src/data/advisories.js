import { computed, reactive } from 'vue'
import { getJson, postJson } from './api'

// Advisories matched against what runs here (core.advisories). One snapshot: affects
// (version-confirmed hits), possible (not stated for the release line, free-text
// ranges, keyword alerts), global (exploited / national alerts that match nothing
// here), others, fix groups, per-source freshness.
export const advisories = reactive({ data: null, loading: false, error: null })

export async function loadAdvisories() {
  advisories.loading = true
  try {
    advisories.data = await getJson('/api/v1/advisories')
    advisories.error = null
  } catch (e) {
    advisories.error = e
  } finally {
    advisories.loading = false
  }
}
export async function checkAdvisories({ rematchOnly = false } = {}) {
  await postJson('/api/v1/advisories/check', { rematch_only: rematchOnly })
  if (advisories.data) advisories.data.checking = true
}
export async function setAdvisoryState(id, state, note = null, target = null) {
  advisories.data = await postJson('/api/v1/advisories/state', { id, state, note, target })
}
export function advisoriesLanded(frame) {
  advisories.data = frame
}
export const affectsCount = computed(() => advisories.data?.counts?.affects || 0)
export const affectsNew = computed(() => advisories.data?.counts?.affects_new || 0)
export function advisoriesFor(project, benchIndex = null) {
  return (advisories.data?.affects || []).filter((a) => (a.matches || []).some((m) => m.status === 'affected' && m.project === project && (benchIndex == null || String(m.bench_index) === String(benchIndex))))
}
export function advisoriesForApp(project, benchIndex, app) {
  return (advisories.data?.affects || []).filter((a) => (a.matches || []).some((m) => m.status === 'affected' && m.project === project && String(m.bench_index) === String(benchIndex) && m.kind === 'app' && m.name === app))
}
export const SEV_THEME = { critical: 'red', high: 'red', medium: 'amber', moderate: 'amber', low: 'gray', unknown: 'gray' }
export const SEV_RANK = { critical: 0, high: 1, medium: 2, moderate: 2, low: 3, unknown: 4 }
export function primaryLink(a) {
  return (a.sources || []).find((s) => s.url)?.url || null
}
export function idsOf(a) {
  return [a.id, ...(a.aliases || [])].filter((i) => /^(CVE|GHSA|PYSEC|AL\d|ADV\d)/i.test(i)).slice(0, 4)
}
export function whenText(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const days = Math.round((Date.now() - d.getTime()) / 86400000)
  return days <= 0 ? 'today' : days === 1 ? 'yesterday' : days < 30 ? `${days} days ago` : d.toISOString().slice(0, 10)
}
