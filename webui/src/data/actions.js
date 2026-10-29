// The action catalog: lifecycle work becomes a detached job; only the quick
// cache refresh and label metadata write stay on POST /api/v1/actions.
//
// confirm: 'none'    runs on click, toast on completion
//          'confirm' a dialog with the consequence and the equivalent cwcli line
//          'typed'   the dialog's confirm stays disabled until the exact name of the
//                    target is typed back (confirmTarget, else site > bench > instance)
// fields:  inputs the dialog collects: { name, label, placeholder?, type: 'text' | 'password' | 'checkbox' }
// danger:  a destructive verb - the confirm button and the menu entry render red
// Every entry shows its cwcli command (the design system's "show the command" rule).
//
// The guarded destructive verbs (instance.remove, bench.remove, site.remove,
// app.checkout_reset) run the same core paths as the CLI with the verified
// backup gate intact; `consent: true` is sent only after the typed confirmation,
// and no request can express --no-backup or --force.

import { can, ROLE_LABEL, session } from './session'

const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`)
const benchFlag = (c) => (c.bench != null ? ` --bench ${q(c.bench)}` : '')
// `bench drop-site` needs the MariaDB root password; blank means the one cwcli
// remembered at init (else the compose default, 123). It travels in the job's
// secrets, never its spec.
const DB_ROOT_FIELD = { name: 'db_root_password', type: 'password', label: 'Database root password', placeholder: 'blank uses the instance\'s saved password' }
const secrets = (v) => (v?.db_root_password ? { secrets: { db_root_password: v.db_root_password } } : {})

export const ACTIONS = {
  // instance
  'instance.start': {
    job: true,
    label: 'Start',
    icon: 'lucide-play',
    confirm: 'none',
    cli: (c) => `cwcli start ${q(c.project)}`,
    says: (c) => `Starts ${c.project}'s containers, then every bench under cwcli's supervisor.`,
    request: (c) => ({ verb: 'start', target: { project: c.project }, args: {} }),
    done: (c) => `${c.project} started`,
  },
  'instance.stop': {
    job: true,
    needsRunning: true,
    label: 'Stop',
    icon: 'lucide-square',
    confirm: 'confirm',
    cli: (c) => `cwcli stop ${q(c.project)}`,
    says: (c) => `Stops every bench and container in ${c.project}. The database gets 60 seconds to shut down cleanly.`,
    request: (c) => ({ verb: 'stop', target: { project: c.project }, args: {} }),
    done: (c) => `${c.project} stopped`,
  },
  'instance.restart': {
    job: true,
    needsRunning: true,
    label: 'Restart',
    icon: 'lucide-rotate-cw',
    confirm: 'confirm',
    cli: (c) => `cwcli restart ${q(c.project)}`,
    says: (c) => `Stops and starts ${c.project}. Every bench goes down and comes back.`,
    request: (c) => ({ verb: 'restart', target: { project: c.project }, args: {} }),
    done: (c) => `${c.project} restarted`,
  },
  'instance.refresh': {
    needsRunning: true,
    label: 'Refresh health',
    icon: 'lucide-refresh-cw',
    confirm: 'none',
    cli: (c) => `cwcli status ${q(c.project)}`,
    says: () => 'Re-reads container state and probes every bench again.',
    request: (c) => ({ action: 'refresh_status', project: c.project }),
    done: (c) => `${c.project} health refreshed`,
  },
  'instance.remove': {
    job: true,
    danger: true,
    label: 'Remove instance',
    icon: 'lucide-trash-2',
    confirm: 'typed',
    fields: [
      { name: 'keep_volumes', type: 'checkbox', label: 'Keep the named volumes (databases, sites, files)' },
      { name: 'force', type: 'checkbox', label: 'Force: skip the backup. Every database, site and file is deleted with NO copy kept.' },
    ],
    // Force is the page's --no-backup. It exists on the captain's instruction (2026-10-27);
    // the dialog turns red and spells out what is lost before the typed confirmation.
    danger_when: (v) => !!v?.force && !v?.keep_volumes,
    cli: (c, v) => `cwcli rm ${q(c.project)}${v?.keep_volumes ? ' --no-volumes' : ''}${v?.force && !v?.keep_volumes ? ' --no-backup' : ''}`,
    says: (c, v) =>
      v?.keep_volumes
        ? `Removes ${c.project}'s containers, project network and project directory. Its named volumes (databases, sites, files) stay on Docker.`
        : v?.force
          ? `FORCE: deletes ${c.project}'s containers, named volumes (every database, every site, all public and private files), project network and project directory WITHOUT taking a backup. There is no copy to restore from afterwards. Untick Force to keep the verified backup, or keep the volumes instead.`
          : `Backs up every site on ${c.project} to the host archive, then deletes its containers, named volumes (databases, sites, files), project network and project directory. If any backup fails, nothing is deleted.`,
    request: (c, v) => ({ verb: 'rm', target: { project: c.project }, args: { keep_volumes: !!v.keep_volumes, no_backup: !!v.force && !v.keep_volumes }, consent: true }),
    done: (c) => `${c.project} removed`,
  },
  'instance.scale': {
    job: true,
    needsRunning: true,
    label: 'Widen port range',
    icon: 'lucide-arrow-left-right',
    confirm: 'confirm',
    cli: (c) => `cwcli scale ${q(c.project)}`,
    says: () =>
      'Recreates the frappe container with a wider published port range so every bench is reachable. Every serving bench restarts. The database is not touched.',
    request: (c) => ({ verb: 'scale', target: { project: c.project }, args: {} }),
    done: (c, out) => (out?.expanded ? `${c.project}'s port range widened` : `${c.project}'s port range already covers every bench`),
  },

  // bench
  'bench.start': {
    job: true,
    label: 'Start bench',
    icon: 'lucide-play',
    confirm: 'none',
    cli: (c) => `cwcli start ${q(c.project)}${benchFlag(c)}`,
    says: (c) => `Brings bench ${c.benchName} up under cwcli's supervisor.`,
    request: (c) => ({ verb: 'start', target: { project: c.project, bench: c.bench }, args: {} }),
    done: (c) => `bench ${c.benchName} started`,
  },
  'bench.stop': {
    job: true,
    needsRunning: true,
    label: 'Stop bench',
    icon: 'lucide-square',
    confirm: 'confirm',
    cli: (c) => `cwcli stop ${q(c.project)}${benchFlag(c)}`,
    says: (c) => `Stops bench ${c.benchName}'s processes. Other benches keep running.`,
    request: (c) => ({ verb: 'stop-bench', target: { project: c.project, bench: c.bench }, args: {} }),
    done: (c) => `bench ${c.benchName} stopped`,
  },
  'bench.restart': {
    job: true,
    needsRunning: true,
    label: 'Restart bench',
    icon: 'lucide-rotate-cw',
    confirm: 'confirm',
    cli: (c) => `cwcli restart ${q(c.project)}${benchFlag(c)}`,
    says: (c) => `Takes ${c.project}'s containers down and brings bench ${c.benchName} back up.`,
    request: (c) => ({ verb: 'restart', target: { project: c.project, bench: c.bench }, args: {} }),
    done: (c) => `bench ${c.benchName} restarted`,
  },
  'bench.remove': {
    job: true,
    danger: true,
    label: 'Remove bench',
    icon: 'lucide-trash-2',
    confirm: 'typed',
    fields: [DB_ROOT_FIELD],
    cli: (c) => `cwcli rm-bench ${q(c.project)}${benchFlag(c)}`,
    says: (c) =>
      `Backs up every site on bench ${c.benchName} to the host archive, drops each one (deleting its database and files), then deletes the bench directory. Nothing is dropped unless every backup succeeded. Other benches keep running.`,
    request: (c, v) => ({ verb: 'rm-bench', target: { project: c.project, bench: c.bench }, args: {}, consent: true, ...secrets(v) }),
    done: (c) => `bench ${c.benchName} removed`,
  },
  'bench.label': {
    needsRunning: true,
    label: 'Set label',
    icon: 'lucide-tag',
    confirm: 'confirm',
    fields: [{ name: 'label', label: 'Label', placeholder: 'v15-main' }],
    cli: (c, v) => `cwcli label ${q(c.project)} ${q(c.bench)} ${q(v?.label || '<label>')}`,
    says: () => 'A durable name usable anywhere a bench selector is. Stored in the cache and in a marker file inside the bench.',
    request: (c, v) => ({ action: 'set_label', project: c.project, bench: c.bench, label: v.label }),
    done: (c, out) => `bench labelled ${out?.label}`,
  },

  // site
  'site.open': {
    needsRunning: true,
    label: 'Open site',
    icon: 'lucide-external-link',
    confirm: 'none',
    read: true,
    cli: (c) => `cwcli axi url ${q(c.project)}${benchFlag(c)} --site ${q(c.site)}`,
    says: () => 'Resolves the host URL from the published ports and probes it fresh.',
  },
  'site.unlock': {
    job: true,
    needsRunning: true,
    label: 'Unlock',
    icon: 'lucide-lock-open',
    confirm: 'confirm',
    cli: (c) => `cwcli unlock ${q(c.project)}${benchFlag(c)} --site ${q(c.site)}`,
    says: () => "Removes the site's locks folder; it regenerates on the next lock.",
    request: (c) => ({ verb: 'unlock', target: { project: c.project, bench: c.bench, site: c.site }, args: {} }),
    done: (c, out) => (out?.already_unlocked ? `${c.site} had no locks` : `${c.site} unlocked`),
  },
  'site.dev_mode_on': {
    job: true,
    needsRunning: true,
    label: 'Turn developer mode on',
    icon: 'lucide-code',
    confirm: 'confirm',
    fields: [{ name: 'common', type: 'checkbox', label: 'Bench-wide: set it in common_site_config.json for every site on this bench' }],
    cli: (c, v) => `cwcli dev-mode ${q(c.project)} ${q(c.site)} on${benchFlag(c)}${v?.common ? ' --common' : ''}`,
    says: () => "Customisations save to the app's source files, errors show full tracebacks in the browser, assets rebuild on change; the site's cache is cleared afterwards. Never leave it on where real users work.",
    request: (c, v) => ({ verb: 'dev-mode', target: { project: c.project, bench: c.bench, site: c.site }, args: { on: true, scope: v?.common ? 'common' : 'site' }, consent: true }),
    done: (c) => `developer mode on for ${c.site}`,
  },
  'site.dev_mode_off': {
    job: true,
    needsRunning: true,
    label: 'Turn developer mode off',
    icon: 'lucide-code',
    confirm: 'confirm',
    fields: [{ name: 'common', type: 'checkbox', label: 'Bench-wide: unset it in common_site_config.json for every site on this bench' }],
    cli: (c, v) => `cwcli dev-mode ${q(c.project)} ${q(c.site)} off${benchFlag(c)}${v?.common ? ' --common' : ''}`,
    says: () => "Customisations stop writing to source files and tracebacks are hidden again; the site's cache is cleared afterwards.",
    request: (c, v) => ({ verb: 'dev-mode', target: { project: c.project, bench: c.bench, site: c.site }, args: { on: false, scope: v?.common ? 'common' : 'site' }, consent: true }),
    done: (c) => `developer mode off for ${c.site}`,
  },
  'site.remove': {
    job: true,
    danger: true,
    needsRunning: true,
    label: 'Drop site',
    icon: 'lucide-trash-2',
    confirm: 'typed',
    fields: [DB_ROOT_FIELD],
    cli: (c) => `cwcli rm-site ${q(c.project)} ${q(c.site)}${benchFlag(c)}`,
    says: (c) =>
      `Backs up ${c.site} (database and files) into cwcli's backup store, then drops it: its database and files are deleted. If the backup fails, nothing is dropped. The instance and its other sites keep running.`,
    request: (c, v) => ({ verb: 'rm-site', target: { project: c.project, bench: c.bench, site: c.site }, args: {}, consent: true, ...secrets(v) }),
    done: (c) => `${c.site} dropped`,
  },
  delete_backup: {
    job: true,
    danger: true,
    label: 'Delete backup',
    icon: 'lucide-trash-2',
    confirm: 'typed',
    cli: (c) => `cwcli backups delete ${q(c.set?.key || '')}`,
    says: (c) => `Deletes backup ${c.set?.id} of ${c.site} (${c.set ? (c.set.size / 1048576).toFixed(1) : '?'} MB) from cwcli's store. A site's last good backup is never deleted; this one is ${c.set?.verified ? 'verified' : 'not verified'}.`,
    request: (c) => ({ verb: 'delete-backup', target: { project: c.project, site: c.site }, args: { set_key: c.set?.key }, consent: true }),
    done: (c) => `backup ${c.set?.id} deleted`,
  },
  'site.maintenance_off': {
    job: true,
    needsRunning: true,
    label: 'Take site out of maintenance',
    icon: 'lucide-wrench',
    confirm: 'confirm',
    cli: (c) => `cwcli run ${q(c.project)}${benchFlag(c)} --site ${q(c.site)} set-maintenance-mode off`,
    says: (c) => `Turns maintenance mode off for ${c.site} so it answers again (an interrupted migrate can leave it at HTTP 503). Nothing else changes.`,
    request: (c) => ({ verb: 'clear-maintenance', target: { project: c.project, bench: c.bench, site: c.site }, args: {} }),
    done: (c) => `${c.site} is out of maintenance mode`,
  },

  // app
  'app.checkout': {
    job: true,
    needsRunning: true,
    label: 'Check out ref',
    icon: 'lucide-git-branch',
    confirm: 'confirm',
    fields: [{ name: 'ref', label: 'Branch, tag or commit', placeholder: 'develop' }],
    cli: (c, v) => `cwcli apps checkout ${q(c.project)} ${q(c.app)} ${q(v?.ref || '<ref>')}${benchFlag(c)}`,
    says: (c) =>
      `Fetches and checks out a ref into ${c.app}, then restarts the bench's processes. A dirty working tree is refused; nothing is reset.`,
    request: (c, v) => ({ verb: 'apps-checkout', target: { project: c.project, bench: c.bench, app: c.app }, args: { ref: v.ref } }),
    done: (c, out, v) => `${c.app} is on ${v.ref}`,
  },
  'app.checkout_reset': {
    job: true,
    danger: true,
    needsRunning: true,
    label: 'Check out ref, discarding local changes',
    icon: 'lucide-git-branch',
    confirm: 'typed',
    confirmTarget: (c) => c.app,
    fields: [{ name: 'ref', label: 'Branch, tag or commit', placeholder: 'develop' }],
    cli: (c, v) => `cwcli apps checkout ${q(c.project)} ${q(c.app)} ${q(v?.ref || '<ref>')} --reset${benchFlag(c)}`,
    says: (c) =>
      `Fetches the ref, then resets ${c.app}'s working tree to it: uncommitted changes to tracked files are discarded. Untracked files are left in place (nothing runs git clean). The bench's processes restart afterwards.`,
    request: (c, v) => ({ verb: 'apps-checkout', target: { project: c.project, bench: c.bench, app: c.app }, args: { ref: v.ref, reset: true } }),
    done: (c, out, v) => `${c.app} reset to ${v.ref}`,
  },

  // process
  'process.restart': {
    job: true,
    needsRunning: true,
    label: 'Restart process',
    icon: 'lucide-rotate-cw',
    confirm: 'none',
    cli: (c) => `cwcli restart ${q(c.project)}${benchFlag(c)} --process ${q(c.process)}`,
    says: (c) => `Restarts ${c.process} only; the other processes keep running.`,
    request: (c) => ({ verb: 'restart-process', target: { project: c.project, bench: c.bench, process: c.process }, args: {} }),
    done: (c) => `${c.process} restarted`,
  },
}

// Whether an action can run in the instance's current state, and if not, why.
// cwcli's own verbs refuse these on a stopped container (resolve_container_state
// with offer_choice=False); the GUI says so up front instead of failing later.
// `values` are the dialog's current inputs, for actions whose precondition
// depends on them (removing an instance needs a live backup unless the volumes stay).
export function availability(id, inst, bench, values) {
  const a = ACTIONS[id]
  const role = id.endsWith('.remove') || id === 'delete_backup' || id === 'app.checkout_reset' ? 'admin' : 'operator'
  if (!a?.read && !can(role)) return { ok: false, role, reason: `Needs the ${ROLE_LABEL[role]} role`, detail: `You are signed in as ${ROLE_LABEL[session.user?.role] === 'Admin' ? 'an' : 'a'} ${ROLE_LABEL[session.user?.role] || 'Viewer'}; ask an Admin if you need this.` }
  const benchUp = ['running', 'degraded'].includes(bench?.overall)
  if (id === 'instance.start' && inst?.container_running) return { ok: false, reason: 'Already running' }
  if (id === 'bench.start' && benchUp) return { ok: false, reason: 'Already running' }
  if (id === 'instance.remove') {
    if (values?.keep_volumes || values?.force || inst?.container_running) return { ok: true, reason: null }
    // The verified backup needs the instance up: the page asks to start it first, as `cwcli rm` does.
    return { ok: true, reason: null, startFirst: true }
  }
  if (id === 'bench.remove' && inst?.container_running && benchUp) {
    return { ok: false, reason: 'Stop the bench first', detail: 'A bench that is still serving is never removed. Stop it, then retry.' }
  }
  if (!a?.needsRunning || inst?.container_running) return { ok: true, reason: null }
  if (id.endsWith('.stop')) return { ok: false, reason: 'Already stopped' }
  // Not a refusal: the page asks to start the instance first, as the CLI does (note 24).
  return { ok: true, reason: null, startFirst: true }
}
