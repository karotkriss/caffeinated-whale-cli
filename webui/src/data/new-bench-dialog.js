import { reactive } from 'vue'
import { refuseBelow } from './session'

// The one New bench dialog for the whole app (components/NewBenchDialog.vue). `host`
// preselects a remote host's instance; the job then runs on that host. `retry` reopens
// a job whose MariaDB root password was rejected: its args, and the rejection to show.
export const newBenchDialog = reactive({ open: false, project: null, host: null, retry: null })

export function openNewBench({ project = null, host = null, retry = null } = {}) {
  if (refuseBelow()) return
  newBenchDialog.project = project
  newBenchDialog.host = host
  newBenchDialog.retry = retry
  newBenchDialog.open = true
}
