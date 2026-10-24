import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { ACTIONS, availability } from './actions'
import { instance, loadDetail, getJsonUrl } from './fleet'
import { postJson, ApiError } from './api'
import { createJob } from './jobs'
import { router } from '../router'

// One action dialog for the whole app (components/ActionDialog.vue renders it).
export const dialogState = reactive({
  open: false,
  id: null,
  ctx: {},
  values: {},
  error: null, // ApiError from the last attempt
  typedConfirm: null, // a core NEEDS_CHOICE confirm (e.g. confirm_scale) -> type the name
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
  if (a.confirm === 'none' && !a.field) return runAction(id, full, {})
  dialogState.id = id
  dialogState.ctx = full
  dialogState.values = {}
  dialogState.error = null
  dialogState.typedConfirm = null
  dialogState.open = true
  return null
}

// POST the action; on scale's core NEEDS_CHOICE confirm, re-open as a typed confirm.
// Every other needs_choice (confirm_start on a stopped instance) is a plain error.
export async function runAction(id, ctx, values, { consent = false } = {}) {
  const a = ACTIONS[id]
  if (a.read) return openSite(ctx)
  const payload = a.request(ctx, values)
  if (consent || a.consent) payload.consent = true
  dialogState.running = true
  try {
    const data = a.job ? await createJob(payload) : await postJson('/api/v1/actions', payload)
    if (a.job) {
      dialogState.open = false
      await router.push(`/jobs/${data.id}`)
      return data
    }
    toast.success(a.done ? a.done(ctx, data?.outcome, values) : a.label, { description: a.cli(ctx, values) })
    for (const w of data?.warnings || []) toast.warning(w.text || w.code)
    loadDetail(ctx.project, { force: true })
    dialogState.open = false
    return data
  } catch (e) {
    if (e instanceof ApiError && e.type === 'needs_choice' && e.code === 'confirm_scale') {
      // Consent is the core's decision: it refused before any mutation and
      // explains why; the user types the instance name to grant it.
      dialogState.id = id
      dialogState.ctx = ctx
      dialogState.values = values
      dialogState.error = null
      dialogState.typedConfirm = { message: e.message, target: ctx.project }
      dialogState.open = true
      return null
    }
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
// instance's state rules it out.
export function menuOption(id, ctx, bench) {
  const a = ACTIONS[id]
  const { ok, reason } = availability(id, instance(ctx.project), bench)
  return {
    label: ok ? a.label : `${a.label} (${reason.toLowerCase()})`,
    icon: a.icon,
    disabled: !ok,
    onClick: () => openAction(id, ctx),
  }
}
