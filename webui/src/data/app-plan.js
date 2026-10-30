// The dependency plan the Install, New site and New bench dialogs preview
// (GET /api/v1/apps/plan, core/app_deps.py). A read: nothing is fetched into a bench
// and nothing is installed; the job re-plans before it acts.
import { ref, watch } from 'vue'
import { apiBase, getJson } from './api'

export async function fetchPlan(params, host = null) {
  const q = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '' || value === false) continue
    if (Array.isArray(value)) for (const v of value) q.append(key, v)
    else q.append(key, value === true ? '1' : String(value))
  }
  return getJson(`${apiBase(host)}/apps/plan?${q}`)
}

// A live plan for a reactive set of params: re-read (debounced) whenever they change,
// the stale answer dropped when a newer request has been made. `params()` returns
// null while the dialog cannot plan yet (no instance, no app).
export function usePlan(params, host = () => null) {
  const plan = ref(null)
  const loading = ref(false)
  const error = ref(null)
  let seq = 0
  let timer = null
  watch([params, host], ([p, h]) => {
    clearTimeout(timer)
    const mine = ++seq
    if (!p) {
      plan.value = null
      loading.value = false
      error.value = null
      return
    }
    loading.value = true
    timer = setTimeout(async () => {
      try {
        const data = await fetchPlan(p, h)
        if (mine === seq) { plan.value = data; error.value = null }
      } catch (e) {
        if (mine === seq) { plan.value = null; error.value = e.hint ? `${e.message} ${e.hint}` : e.message }
      } finally {
        if (mine === seq) loading.value = false
      }
    }, 400)
  }, { immediate: true, deep: true })
  return { plan, loading, error }
}

// What a plan means for the Start button and the job's args.
export const planBlockers = (plan) => (plan?.problems || []).filter((p) => p.severity === 'blocker')
export const planChoices = (plan) => (plan?.problems || []).filter((p) => p.severity === 'choice')
// Every source pick, made or still open: a made one stays shown so it can be changed.
export const sourcePicks = (plan) => (plan?.problems || []).filter((p) => (p.options || []).length > 1)
// A plan whose only blockers are Frappe ranges a person may override.
export const onlyOverridable = (plan) => planBlockers(plan).length > 0 && planBlockers(plan).every((p) => p.overridable)
export const requiredNodes = (plan) => (plan?.nodes || []).filter((n) => !n.requested && n.action !== 'none')
