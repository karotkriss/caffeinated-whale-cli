import { reactive } from 'vue'

// The one New bench dialog for the whole app (components/NewBenchDialog.vue).
export const newBenchDialog = reactive({ open: false, project: null })

export function openNewBench({ project = null } = {}) {
  newBenchDialog.project = project
  newBenchDialog.open = true
}
