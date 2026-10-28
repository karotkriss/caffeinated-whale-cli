import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { ACTIONS, availability } from './actions'
import { instance, loadDetail, getJsonUrl } from './fleet'
import { postJson } from './api'
import { createJob } from './jobs'
import { router } from '../router'
import { trackJob } from './job-toasts'
import { ensureRunning } from './start-gate'

// One action dialog for the whole app (components/ActionDialog.vue renders it).
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

// Run `id` for `ctx`: straight away for confirm:'none', otherwise through the dialog.
export function openAction(id, ctx = {}) {
  const a = ACTIONS[id]
  if (!a) throw new Error(`unknown action ${id}`)
  const full = { benchName: ctx.bench, ...ctx }
  if (a.confirm === 'none' && !a.fields) return runAction(id, full, {})
  dialogState.id = id
  dialogState.ctx = full
  dialogState.values = {}
  dialogState.error = null
  dialogState.open = true
  return null
}

// Start the job (or POST the quick action). A job's own needs_choice, such as
// scale's confirm_scale, is answered on its job page.
export async function runAction(id, ctx, values) {
  const a = ACTIONS[id]
  const { startFirst } = availability(id, instance(ctx.project), null, values)
  if (startFirst && !(await ensureRunning(ctx.project, a.label))) return null
  if (a.read) return openSite(ctx)
  const payload = a.request(ctx, values)
  dialogState.running = true
  try {
    const data = a.job ? await createJob(payload) : await postJson('/api/v1/actions', payload)
    if (a.job) {
      // A running card follows the job from here; the job page is one click away.
      dialogState.open = false
      trackJob(data.id)
      return data
    }
    toast.success(a.done ? a.done(ctx, data?.outcome, values) : a.label, { description: a.cli(ctx, values) })
    for (const w of data?.warnings || []) toast.warning(w.text || w.code)
    loadDetail(ctx.project, { force: true })
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
    const probe = await getJsonUrl(ctx.project, query)
    if (!probe.url) {
      toast.warning(`${ctx.site} is not serving`, { description: 'No published port resolves for this bench; nothing was guessed.' })
      return null
    }
    window.open(probe.url, '_blank', 'noopener')
    if (!probe.reachable) toast.warning(`${probe.url} did not answer`, { description: 'Opened anyway; the bench may still be starting.' })
    return probe
  } catch (e) {
    toast.error('Could not resolve the site URL', { description: hint(e) })
    return null
  }
}

// A Dropdown option for an action, disabled (with its reason) when the
// instance's state rules it out even with every dialog checkbox ticked, so a
// dialog option that can arm the action keeps its entry open. A destructive
// action's entry is red.
export function menuOption(id, ctx, bench) {
  const a = ACTIONS[id]
  const reachable = Object.fromEntries(
    (a.fields || []).filter((f) => f.type === 'checkbox').map((f) => [f.name, true]),
  )
  const { ok, reason } = availability(id, instance(ctx.project), bench, reachable)
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
