// The action catalog: one entry per GUI verb, every one a POST /api/v1/actions
// the daemon already serves (its synchronous safe set). Verbs that wait on the
// job backend or on their own decision (init, backup, migrate, rm, install,
// update, restore, uninstall, run) are deliberately ABSENT - not greyed out:
// a disabled button for an excluded verb still builds its surface.
//
// confirm: 'none'    runs on click, toast on completion
//          'confirm' a dialog with the consequence and the equivalent cwcli line
//          'typed'   the dialog's confirm stays disabled until the target's name is typed
// field:   an input the dialog collects (label, ref, ...)
// Every entry shows its cwcli command (the design system's "show the command" rule).

const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`)
const benchFlag = (c) => (c.bench != null ? ` --bench ${q(c.bench)}` : '')

export const ACTIONS = {
  // instance
  'instance.start': {
    label: 'Start',
    icon: 'lucide-play',
    confirm: 'none',
    cli: (c) => `cwcli start ${q(c.project)}`,
    says: (c) => `Starts ${c.project}'s containers, then every bench under cwcli's supervisor.`,
    request: (c) => ({ action: 'start_instance', project: c.project }),
    done: (c) => `${c.project} started`,
  },
  'instance.stop': {
    needsRunning: true,
    label: 'Stop',
    icon: 'lucide-square',
    confirm: 'confirm',
    cli: (c) => `cwcli stop ${q(c.project)}`,
    says: (c) => `Stops every bench and container in ${c.project}. The database gets 60 seconds to shut down cleanly.`,
    request: (c) => ({ action: 'stop_instance', project: c.project }),
    done: (c) => `${c.project} stopped`,
  },
  'instance.restart': {
    needsRunning: true,
    label: 'Restart',
    icon: 'lucide-rotate-cw',
    confirm: 'confirm',
    cli: (c) => `cwcli restart ${q(c.project)}`,
    says: (c) => `Stops and starts ${c.project}. Every bench goes down and comes back.`,
    request: (c) => ({ action: 'restart_instance', project: c.project }),
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
  'instance.scale': {
    needsRunning: true,
    label: 'Widen port range',
    icon: 'lucide-arrow-left-right',
    confirm: 'confirm',
    cli: (c) => `cwcli scale ${q(c.project)}`,
    says: () =>
      'Recreates the frappe container with a wider published port range so every bench is reachable. Every serving bench restarts. The database is not touched.',
    request: (c) => ({ action: 'scale_instance', project: c.project }),
    done: (c, out) => (out?.expanded ? `${c.project}'s port range widened` : `${c.project}'s port range already covers every bench`),
  },

  // bench
  'bench.start': {
    label: 'Start bench',
    icon: 'lucide-play',
    confirm: 'none',
    cli: (c) => `cwcli start ${q(c.project)}${benchFlag(c)}`,
    says: (c) => `Brings bench ${c.benchName} up under cwcli's supervisor.`,
    request: (c) => ({ action: 'start_instance', project: c.project, bench: c.bench }),
    done: (c) => `bench ${c.benchName} started`,
  },
  'bench.stop': {
    needsRunning: true,
    label: 'Stop bench',
    icon: 'lucide-square',
    confirm: 'confirm',
    cli: (c) => `cwcli stop ${q(c.project)}${benchFlag(c)}`,
    says: (c) => `Stops bench ${c.benchName}'s processes. Other benches keep running.`,
    request: (c) => ({ action: 'stop_bench', project: c.project, bench: c.bench }),
    done: (c) => `bench ${c.benchName} stopped`,
  },
  'bench.restart': {
    needsRunning: true,
    label: 'Restart bench',
    icon: 'lucide-rotate-cw',
    confirm: 'confirm',
    cli: (c) => `cwcli restart ${q(c.project)}${benchFlag(c)}`,
    says: (c) => `Takes ${c.project}'s containers down and brings bench ${c.benchName} back up.`,
    request: (c) => ({ action: 'restart_instance', project: c.project, bench: c.bench }),
    done: (c) => `bench ${c.benchName} restarted`,
  },
  'bench.label': {
    needsRunning: true,
    label: 'Set label',
    icon: 'lucide-tag',
    confirm: 'confirm',
    field: { name: 'label', label: 'Label', placeholder: 'v15-main' },
    cli: (c, v) => `cwcli label ${q(c.project)} ${q(v?.label || '<label>')}${benchFlag(c)}`,
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
    needsRunning: true,
    label: 'Unlock',
    icon: 'lucide-lock-open',
    confirm: 'confirm',
    cli: (c) => `cwcli unlock ${q(c.project)}${benchFlag(c)} --site ${q(c.site)}`,
    says: () => "Removes the site's locks folder; it regenerates on the next lock.",
    request: (c) => ({ action: 'unlock_site', project: c.project, bench: c.bench, site: c.site }),
    done: (c, out) => (out?.already_unlocked ? `${c.site} had no locks` : `${c.site} unlocked`),
  },
  'site.maintenance_off': {
    needsRunning: true,
    label: 'Take site out of maintenance',
    icon: 'lucide-wrench',
    confirm: 'confirm',
    cli: (c) => `cwcli run ${q(c.project)}${benchFlag(c)} --site ${q(c.site)} set-maintenance-mode off`,
    says: (c) => `Turns maintenance mode off for ${c.site} so it answers again (an interrupted migrate can leave it at HTTP 503). Nothing else changes.`,
    request: (c) => ({ action: 'clear_maintenance', project: c.project, bench: c.bench, site: c.site }),
    done: (c) => `${c.site} is out of maintenance mode`,
  },

  // app
  'app.checkout': {
    needsRunning: true,
    label: 'Check out ref',
    icon: 'lucide-git-branch',
    confirm: 'confirm',
    field: { name: 'ref', label: 'Branch, tag or commit', placeholder: 'develop' },
    cli: (c, v) => `cwcli apps checkout ${q(c.project)} ${q(c.app)} ${q(v?.ref || '<ref>')}${benchFlag(c)}`,
    says: (c) =>
      `Fetches and checks out a ref into ${c.app}, then restarts the bench's processes. A dirty working tree is refused; nothing is reset.`,
    request: (c, v) => ({ action: 'checkout_app', project: c.project, bench: c.bench, app: c.app, ref: v.ref }),
    done: (c, out, v) => `${c.app} is on ${v.ref}`,
  },

  // process
  'process.restart': {
    needsRunning: true,
    label: 'Restart process',
    icon: 'lucide-rotate-cw',
    confirm: 'none',
    cli: (c) => `cwcli restart ${q(c.project)}${benchFlag(c)} --process ${q(c.process)}`,
    says: (c) => `Restarts ${c.process} only; the other processes keep running.`,
    request: (c) => ({ action: 'restart_process', project: c.project, bench: c.bench, process: c.process }),
    done: (c) => `${c.process} restarted`,
  },
}

// Whether an action can run in the instance's current state, and if not, why.
// cwcli's own verbs refuse these on a stopped container (resolve_container_state
// with offer_choice=False); the GUI says so up front instead of failing later.
export function availability(id, inst, bench) {
  const a = ACTIONS[id]
  if (id === 'instance.start' && inst?.container_running) return { ok: false, reason: 'Already running' }
  if (id === 'bench.start' && ['running', 'degraded'].includes(bench?.overall)) return { ok: false, reason: 'Already running' }
  if (!a?.needsRunning || inst?.container_running) return { ok: true, reason: null }
  return { ok: false, reason: `Needs ${inst?.project || 'the instance'} running` }
}
