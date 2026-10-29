// One backup set's actions, shared by the Backups page and a site's Backups tab: only
// what this role may do, and an off-site copy only while a backup target is on. A set
// on a remote host offers what runs there through the hub (verify, download, delete);
// restore, sending and off-site copies are this machine's dialogs and stay local.
import { toast } from 'frappe-ui'
import { backups, downloadSet } from './backups'
import { openAction } from './dialog'
import { createJob } from './jobs'
import { trackJob } from './job-toasts'
import { openRestoreWizard } from './restore-wizard'
import { can } from './session'
import { copyOffsite, sendSet } from './transfer'

async function verify(s, host) {
  trackJob((await createJob({ verb: 'verify-backup', target: { project: s.project, site: s.site }, args: { set_key: s.key } }, host)).id, host)
}

export function setMenu(s, host = null) {
  const op = can('operator')
  return [{ group: 'Set', options: [
    op && !host && { label: 'Restore…', icon: 'lucide-rotate-ccw', onClick: () => openRestoreWizard(s) },
    op && { label: 'Verify again', icon: 'lucide-shield-check', onClick: () => verify(s, host) },
    op && !host && { label: 'Send to another machine…', icon: 'lucide-send', onClick: () => sendSet(s) },
    op && !host && backups.data?.offsite_target && { label: s.offsite ? 'Copy off-site again' : 'Copy off-site', icon: 'lucide-cloud-upload', onClick: () => copyOffsite(s) },
    op && { label: 'Download (.tar)', icon: 'lucide-download', onClick: () => downloadSet(s, host).catch((e) => toast.error(e.message)) },
    can('admin') && { label: 'Delete', icon: 'lucide-trash-2', theme: 'red', onClick: () => openAction('delete_backup', { project: s.project, site: s.site, set: s, host }) },
  ].filter(Boolean) }]
}
