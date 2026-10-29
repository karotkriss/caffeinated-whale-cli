import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { ACTIONS, availability } from './actions'
import { details, instance, loadDetail, getJsonUrl } from './fleet'
import { apiBase, postJson } from './api'
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

function hint(e) {
  return e?.hint ? `${e.message} ${e.hint}` : e?.message
}

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
export function openAction(id, ctx = {}) {
  const a = ACTIONS[id]
  if (!a) throw new Error(`unknown action ${id}`)
  // Publications are this machine's store, so a remote host's site is never `published`
  // here; this machine's instances are keyed in `details` by the bare project.
  const full = {
    benchName: ctx.bench,
    host: null,
    published: !!(!ctx.host && ctx.site && publicationAt(ctx.site, ctx.project, details[ctx.project]?.data?.benches?.find((b) => String(b.index) === String(ctx.bench))?.path)),
    publishVia: { dns: publications.dns?.name, proxy: publications.proxy?.name },
    ...ctx,
  }
  const allowed = availability(id, null, null, {})
  if (allowed.role) {
    toast.error(allowed.reason, { description: allowed.detail })
    return null
  }
  const refused = a.read ? null : hostRefusal(full.host)
  if (refused) {
    toast.error(refused.reason, { description: refused.detail })
    return null
  }
  if (a.confirm === 'none' && !a.fields) return runAction(id, full, {})
  dialogState.id = id
  dialogState.ctx = full
  dialogState.values = Object.fromEntries((a.fields || []).filter((f) => 'default' in f && (!f.show || f.show(full))).map((f) => [f.name, f.default]))
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
    if (!probe.url) {
      toast.warning(`${ctx.site} is not serving`, { description: 'No published port resolves for this bench; nothing was guessed.' })
      return null
    }
    // A browser opens a tab only while the click that asked for it is still live. After
    // the start prompt's wait it is long gone and the tab would be blocked without a
    // word, so the tab is offered on a button, whose own click opens it.
    const open = () => window.open(probe.url, '_blank', 'noopener')
    const where = ctx.host ? ` (an address on ${hostName(ctx.host)}; it must resolve from this browser)` : ''
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
