import { reactive } from 'vue'
import { refuseBelow } from './session'

// The one New bench dialog for the whole app (components/NewBenchDialog.vue).
export const newBenchDialog = reactive({ open: false, project: null })

export function openNewBench({ project = null } = {}) {
  if (refuseBelow()) return
  newBenchDialog.project = project
  newBenchDialog.open = true
}
