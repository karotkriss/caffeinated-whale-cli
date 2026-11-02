import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { ACTIONS, availability } from './actions'
import { detailOf, instance, loadDetail, getJsonUrl } from './fleet'
import { apiBase, getJson, postJson } from './api'
import { createJob } from './jobs'
import { trackJob } from './job-toasts'
import { ensureRunning } from './start-gate'
import { publicationAt, publications } from './publish'
import { actionsAllowed, host as hostOf, hostName } from './hosts'

// One action dialog for the whole app (components/ActionDialog.vue renders it).
// A context's `host` names the remote machine the action runs on (null: this one);
// the request goes to that machine's daemon through the hub's forward.
export const dialogState = reactive({
  open: false,
  id: null,
  ctx: {},
  values: {},
  error: null, // ApiError from the last attempt
  running: false,
})

// Open site on an address that carries the bench's own web port on a name that is not
// a localhost one (the probe's `internal_port`): what Frappe itself reports while
// webserver_port is set, and an address that fails behind a reverse proxy, where that
// port is internal. The page asks first (components/PortWarningDialog.vue) and explains
// how to remove the key; it never rewrites the address.
export const portWarning = reactive({ open: false, probe: null, host: null })
let settlePortWarning = null

function askPortWarning(probe, host) {
  return new Promise((resolve) => {
    settlePortWarning?.(false)
    settlePortWarning = resolve
    portWarning.probe = probe
    portWarning.host = host
    portWarning.open = true
  })
}

// `open`: the dialog's Open button, whose click is the one that may open the tab.
export function answerPortWarning(open) {
  portWarning.open = false
  const done = settlePortWarning
  settlePortWarning = null
  done?.(open)
}

// frappe-ui renders a toast description as sanitized HTML, which strips an
// unknown tag such as a command's `<address>` placeholder, so server text is
// escaped to reach the toast as written.
const text = (s) => s?.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

function hint(e) {
  return text(e?.hint ? `${e.message} ${e.hint}` : e?.message)
}

const PUBLICATION_DIALOGS = new Set(['site.remove', 'site.publish', 'site.unpublish'])

// Why an action on a remote host cannot run right now, or null.
export function hostRefusal(hostId) {
  if (!hostId || actionsAllowed(hostId)) return null
  const h = hostOf(hostId)
  const state = h?.state || 'unknown'
  return {
    reason: state === 'version_mismatch' ? `${hostName(hostId)} runs a different cwcli version` : `${hostName(hostId)} is ${state.replaceAll('_', ' ')}`,
    detail: h?.reason || 'Actions on a remote host run only while its link is connected on a matching cwcli version.',
  }
}

// Run `id` for `ctx`: straight away for confirm:'none', otherwise through the dialog.
export async function openAction(id, ctx = {}) {
  const a = ACTIONS[id]
  if (!a) throw new Error(`unknown action ${id}`)
  const allowed = availability(id, null, null, {})
  if (allowed.role) {
    toast.error(allowed.reason, { description: allowed.detail })
    return null
  }
  const refused = a.read ? null : hostRefusal(ctx.host)
  if (refused) {
    toast.error(refused.reason, { description: refused.detail })
    return null
  }
  // Publications are each machine's own store. A remote site's drop, publish and
  // unpublish dialogs read its host's once, through the hub's forward, as they open (the
  // hub never keeps it), so the dialog names that host's own integrations; a read that
  // fails, or (for a drop) a bench whose path is not known yet, leaves `published` null,
  // and the dialog says it could not tell.
  const bench = detailOf(ctx.project, ctx.host)?.data?.benches?.find((b) => String(b.index) === String(ctx.bench))
  let store = ctx.host ? {} : publications
  if (ctx.host && PUBLICATION_DIALOGS.has(id)) store = bench?.path || id !== 'site.remove' ? await getJson(`${apiBase(ctx.host)}/publications`).catch(() => null) : null
  const full = {
    benchName: ctx.bench,
    host: null,
    published: store ? !!(ctx.site && publicationAt(ctx.site, ctx.project, bench?.path, store)) : null,
    publishVia: { dns: store?.dns?.name, proxy: store?.proxy?.name },
    ...ctx,
  }
  if (a.confirm === 'none' && !a.fields) return runAction(id, full, {})
  dialogState.id = id
  dialogState.ctx = full
  dialogState.values = Object.fromEntries((a.fields || []).filter((f) => 'default' in f && (!f.show || f.show(full))).map((f) => [f.name, typeof f.default === 'function' ? f.default(full) : f.default]))
  dialogState.error = null
  dialogState.open = true
  return null
}

// Start the job (or POST the quick action). A job's own needs_choice, such as
// scale's confirm_scale, is answered on its job page.
export async function runAction(id, ctx, values) {
  const a = ACTIONS[id]
  const { startFirst } = availability(id, instance(ctx.project, ctx.host), null, values)
  if (startFirst && !(await ensureRunning(ctx.project, a.label, { bench: ctx.bench ?? null, benchLabel: ctx.benchName ?? null, host: ctx.host ?? null }))) return null
  if (a.read) return openSite(ctx)
  const payload = a.request(ctx, values)
  dialogState.running = true
  try {
    const data = a.job ? await createJob(payload, ctx.host) : await postJson(`${apiBase(ctx.host)}/actions`, payload)
    if (a.job) {
      // A running card follows the job from here; the job page is one click away.
      dialogState.open = false
      trackJob(data.id, ctx.host)
      return data
    }
    toast.success(a.done ? a.done(ctx, data?.outcome, values) : a.label, { description: ctx.host ? `On ${hostName(ctx.host)}: ${a.cli(ctx, values)}` : a.cli(ctx, values) })
    for (const w of data?.warnings || []) toast.warning(w.text || w.code)
    loadDetail(ctx.project, { force: true, host: ctx.host })
    dialogState.open = false
    return data
  } catch (e) {
    dialogState.error = e
    if (!dialogState.open) toast.error(a.label + ' failed', { description: hint(e) })
    return null
  } finally {
    dialogState.running = false
  }
}

async function openSite(ctx) {
  const query = new URLSearchParams()
  if (ctx.bench != null) query.set('bench', ctx.bench)
  if (ctx.site) query.set('site', ctx.site)
  try {
    const probe = await getJsonUrl(ctx.project, query, ctx.host)
    if (!/^https?:\/\/[^/]/i.test(probe.url || '')) {
      toast.warning(`${ctx.site} has no address to open`, { description: text(probe.reason) || 'cwcli could not resolve an address for this site; nothing was guessed.' })
      return null
    }
    const open = () => window.open(probe.url, '_blank', 'noopener')
    const where = ctx.host ? ` (an address on ${hostName(ctx.host)}; it must resolve from this browser)` : ''
    // Warn before an address that carries the bench's internal port; the dialog's own
    // Open click is a live one, so the tab opens straight from it.
    if (probe.internal_port) {
      if (!(await askPortWarning(probe, ctx.host ?? null))) return null
      open()
      return probe
    }
    // A browser opens a tab only while the click that asked for it is still live. After
    // the start prompt's wait it is long gone and the tab would be blocked without a
    // word, so the tab is offered on a button, whose own click opens it.
    if (navigator.userActivation?.isActive === false) {
      const offer = { duration: Infinity, action: { label: 'Open', onClick: open } }
      if (probe.reachable) toast.success(`${ctx.site} is ready`, { ...offer, description: probe.url + where })
      else toast.warning(`${probe.url} did not answer yet`, { ...offer, description: 'The bench may still be starting.' + where })
      return probe
    }
    open()
    if (!probe.reachable) toast.warning(`${probe.url} did not answer`, { description: 'Opened anyway; the bench may still be starting.' + where })
    return probe
  } catch (e) {
    toast.error('Could not resolve the site URL', { description: hint(e) })
    return null
  }
}

// A Dropdown option for an action, disabled (with its reason) when the
// instance's state rules it out even with every dialog checkbox ticked, so a
// dialog option that can arm the action keeps its entry open. A destructive
// action's entry is red. On a remote host whose link is not connected every
// action is disabled with the link's reason.
export function menuOption(id, ctx, bench) {
  const a = ACTIONS[id]
  const reachable = Object.fromEntries(
    (a.fields || []).filter((f) => f.type === 'checkbox').map((f) => [f.name, true]),
  )
  let { ok, reason } = availability(id, instance(ctx.project, ctx.host), bench, reachable)
  const refused = a.read ? null : hostRefusal(ctx.host)
  if (ok && refused) [ok, reason] = [false, refused.reason]
  return {
    label: ok ? a.label : `${a.label} (${reason.toLowerCase()})`,
    icon: a.icon,
    disabled: !ok,
    ...(a.danger ? { theme: 'red' } : {}),
    onClick: () => openAction(id, ctx),
  }
}

// The "Danger zone" group every page's More menu ends with.
export function dangerGroup(ids, ctx, bench) {
  return { group: 'Danger zone', options: ids.map((id) => menuOption(id, ctx, bench)) }
}
