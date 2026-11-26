import { computed, reactive, watch } from 'vue'
import { getJson } from './api'
import { jobList } from './jobs'
import { sizeText } from './backups'

// Log storage around the instances (core.log_storage): every instance's Docker
// container logs (driver, rotation, size), its bench log files (current vs rotated)
// and the host journal's usage. Read on demand (the page, Refresh) and again when one
// of the storage jobs finishes; nothing here streams.
export const logStorage = reactive({ data: null, loading: false, error: null })

export async function loadLogStorage() {
  logStorage.loading = true
  try {
    logStorage.data = await getJson('/api/v1/storage/logs')
    logStorage.error = null
  } catch (e) {
    logStorage.error = e
  } finally {
    logStorage.loading = false
  }
}

export const STORAGE_VERBS = new Set(['truncate-docker-logs', 'set-docker-log-rotation', 'prune-bench-logs', 'vacuum-journal'])
const TERMINAL = new Set(['succeeded', 'failed', 'interrupted', 'unknown', 'needs_choice'])
const seen = new Set()
// A storage job that just ended changes what the page shows; re-read once per job.
watch(
  () => jobList.value.filter((j) => !j.host && STORAGE_VERBS.has(j.verb) && TERMINAL.has(j.state)).map((j) => j.id),
  (ids) => {
    const fresh = ids.filter((id) => !seen.has(id))
    for (const id of ids) seen.add(id)
    if (fresh.length && logStorage.data) loadLogStorage()
  },
)

export const instanceRows = computed(() => logStorage.data?.instances || [])
// The size of a container's logs, or why it is unknown.
export function dockerSizeText(row) {
  if (row.driver === 'journald') return 'managed by the system journal'
  if (row.driver !== 'json-file') return `${row.driver || 'driver'}-managed`
  return row.size == null ? 'not measured' : sizeText(row.size)
}
export function rotationText(row) {
  if (row.driver === 'journald') return 'system journal'
  if (row.driver !== 'json-file') return row.driver || 'other driver'
  return row.rotation ? `${row.max_size}${row.max_file ? ` × ${row.max_file}` : ''}` : 'unbounded'
}
