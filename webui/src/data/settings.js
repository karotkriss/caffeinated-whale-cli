// Settings writes: one POST /api/v1/settings per change, over the same core
// functions `cwcli config` and `cwcli axi setup` call (see commands/serve.py).
// The daemon keeps the core's own consent (clearing the whole cache answers
// 409 confirm_clear until `consent: true`), so the page confirms first and
// then sends consent, never the other way round.
import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { postJson } from './api'
import { cacheProjects, config } from './fleet'

export const settings = reactive({
  busy: null, // the setting in flight, so its control shows the spinner
  error: null, // ApiError from the last attempt
})

export async function applySetting(setting, payload = {}, { done, refresh = true } = {}) {
  settings.busy = setting
  settings.error = null
  try {
    const data = await postJson('/api/v1/settings', { setting, ...payload })
    for (const w of data?.warnings || []) toast.warning(w.text || w.code)
    if (done) toast.success(typeof done === 'function' ? done(data?.outcome) : done, { id: setting })
    if (refresh) {
      config.load(true)
      cacheProjects.load(true)
      // A daemon this write started (auto-inspect, the credential bridge) writes its
      // pid file a moment after the call returns; one later re-read shows it honest.
      window.setTimeout(() => config.load(true), 2000)
    }
    return data
  } catch (e) {
    settings.error = e
    return null
  } finally {
    settings.busy = null
  }
}
