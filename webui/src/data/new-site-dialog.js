import { reactive } from 'vue'
import { refuseBelow } from './session'

// The one New site dialog for the whole app (components/NewSiteDialog.vue). `host`
// preselects a remote host's instance; the job then runs on that host. `retry` reopens
// a job whose MariaDB root password was rejected: its site, args, and the rejection to show
// (its remote apps were fetched before the check, so the retry names them as bench apps).
export const newSiteDialog = reactive({ open: false, project: null, bench: null, host: null, retry: null })

export function openNewSite({ project = null, bench = null, host = null, retry = null } = {}) {
  if (refuseBelow()) return
  newSiteDialog.project = project
  newSiteDialog.bench = bench != null ? String(bench) : null
  newSiteDialog.host = host
  newSiteDialog.retry = retry
  newSiteDialog.open = true
}
