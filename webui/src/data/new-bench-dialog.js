import { reactive } from 'vue'
import { refuseBelow } from './session'

// The one New bench dialog for the whole app (components/NewBenchDialog.vue). `host`
// preselects a remote host's instance; the job then runs on that host.
export const newBenchDialog = reactive({ open: false, project: null, host: null })

export function openNewBench({ project = null, host = null } = {}) {
  if (refuseBelow()) return
  newBenchDialog.project = project
  newBenchDialog.host = host
  newBenchDialog.open = true
}
