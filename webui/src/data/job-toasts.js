// Jobs started from THIS page, shown as running cards instead of a navigation to
// the job page: the action, its target and the current step, live until it ends.
import { reactive } from 'vue'

export const tracked = reactive({ ids: [] , startedAt: {} })

export function trackJob(id) {
  if (!id || tracked.ids.includes(id)) return
  tracked.ids.push(id)
  tracked.startedAt[id] = Date.now()
}

export function untrackJob(id) {
  tracked.ids = tracked.ids.filter((x) => x !== id)
  delete tracked.startedAt[id]
}
