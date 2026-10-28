// Phase 2 of the backups spec: clone and move (single or batch) and the sendme leg.
import { reactive } from 'vue'
import { createJob } from './jobs'
import { trackJob } from './job-toasts'

export const transferDialog = reactive({ open: false, mode: 'clone', project: null, bench: null, sites: [], pick: false })
export const receiveDialog = reactive({ open: false })

export function openTransfer({ mode = 'clone', project, bench, sites = [], pick = false }) {
  Object.assign(transferDialog, { open: true, mode, project, bench: bench != null ? String(bench) : null, sites: [...sites], pick })
}
export function openReceive() { receiveDialog.open = true }

// Serving a set is a job: the ticket lands on its page (the running toast links to it)
// as soon as sendme prints it, and Interrupt stops serving.
export async function sendSet(set) {
  const record = await createJob({ verb: 'send-backup', target: {}, args: { set_keys: [set.key] } })
  trackJob(record.id)
  return record
}
export async function copyOffsite(set) {
  const record = await createJob({ verb: 's3-copy', target: { project: set.project }, args: { set_keys: [set.key] } })
  trackJob(record.id)
  return record
}
