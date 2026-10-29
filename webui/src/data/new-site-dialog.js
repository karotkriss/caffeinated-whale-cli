import { reactive } from 'vue'
import { refuseBelow } from './session'

// The one New site dialog for the whole app (components/NewSiteDialog.vue). `host`
// preselects a remote host's instance; the job then runs on that host.
export const newSiteDialog = reactive({ open: false, project: null, bench: null, host: null })

export function openNewSite({ project = null, bench = null, host = null } = {}) {
  if (refuseBelow()) return
  newSiteDialog.project = project
  newSiteDialog.bench = bench != null ? String(bench) : null
  newSiteDialog.host = host
  newSiteDialog.open = true
}
