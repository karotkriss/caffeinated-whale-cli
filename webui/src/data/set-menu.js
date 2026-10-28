// One backup set's actions, shared by the Backups page and a site's Backups tab: only
// what this role may do, and an off-site copy only while a backup target is on.
import { toast } from 'frappe-ui'
import { backups, downloadSet } from './backups'
import { openAction } from './dialog'
import { createJob } from './jobs'
import { trackJob } from './job-toasts'
import { openRestoreWizard } from './restore-wizard'
import { can } from './session'
import { copyOffsite, sendSet } from './transfer'

async function verify(s) {
  trackJob((await createJob({ verb: 'verify-backup', target: { project: s.project, site: s.site }, args: { set_key: s.key } })).id)
}

export function setMenu(s) {
  const op = can('operator')
  return [{ group: 'Set', options: [
    op && { label: 'Restore…', icon: 'rotate-ccw', onClick: () => openRestoreWizard(s) },
    op && { label: 'Verify again', icon: 'shield-check', onClick: () => verify(s) },
    op && { label: 'Send to another machine…', icon: 'send', onClick: () => sendSet(s) },
    op && backups.data?.offsite_target && { label: s.offsite ? 'Copy off-site again' : 'Copy off-site', icon: 'cloud-upload', onClick: () => copyOffsite(s) },
    op && { label: 'Download (.tar)', icon: 'download', onClick: () => downloadSet(s).catch((e) => toast.error(e.message)) },
    can('admin') && { label: 'Delete', icon: 'trash-2', theme: 'red', onClick: () => openAction('delete_backup', { project: s.project, site: s.site, set: s }) },
  ].filter(Boolean) }]
}
