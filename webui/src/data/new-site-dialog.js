import { reactive } from 'vue'

// The one New site dialog for the whole app (components/NewSiteDialog.vue).
export const newSiteDialog = reactive({ open: false, project: null, bench: null })

export function openNewSite({ project = null, bench = null } = {}) {
  newSiteDialog.project = project
  newSiteDialog.bench = bench != null ? String(bench) : null
  newSiteDialog.open = true
}
