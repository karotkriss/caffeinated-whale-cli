// Jobs started from THIS page, shown as running cards instead of a navigation to
// the job page: the action, its target and the current step, live until it ends.
// A job on a remote host is tracked with its host, so the card reads it through
// the hub's forward.
import { reactive } from 'vue'
import { jkey } from './jobs'

export const tracked = reactive({ keys: [], meta: {}, startedAt: {} })

export function trackJob(id, host = null) {
  if (!id) return
  const key = jkey(host, id)
  if (tracked.keys.includes(key)) return
  tracked.keys.push(key)
  tracked.meta[key] = { id, host: host || null }
  tracked.startedAt[key] = Date.now()
}

export function untrackJob(key) {
  tracked.keys = tracked.keys.filter((x) => x !== key)
  delete tracked.meta[key]
  delete tracked.startedAt[key]
}
