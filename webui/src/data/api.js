// The daemon speaks the frappe-ui envelope: `{data}` on success, `{errors:[...]}`
// on failure (see commands/serve.py). These two helpers are the whole transport.
import { session } from './session'

// The page asked for something this daemon's code does not know: the installed
// cwcli is newer than the UI server still running, which `cwcli ui` restarts.
const OUTDATED = new Set(['setting.unsupported', 'action.unsupported', 'job.verb_unsupported', 'integration.op_invalid'])

export class ApiError extends Error {
  constructor(status, error) {
    const outdated = OUTDATED.has(error?.title)
    super(outdated ? 'The cwcli UI server is out of date' : error?.message || `HTTP ${status}`)
    this.status = status
    this.type = error?.type
    this.code = error?.title
    this.hint = outdated ? 'Run `cwcli ui` in a terminal to restart it, then reload this page.' : error?.hint
    this.choice = error?.choice
    this.outcome = error?.outcome
    this.detail = error?.detail
  }
}

async function request(url, init = {}) {
  const res = await fetch(url, { headers: { Accept: 'application/json' }, ...init })
  const body = await res.json().catch(() => ({}))
  if (res.status === 401) session.signedOut = true
  if (res.status === 403 && body?.errors?.[0]?.title === 'auth.password_change_required' && session.user) session.user.mustChange = true
  if (!res.ok) throw new ApiError(res.status, body?.errors?.[0])
  return body.data
}

// Every per-instance route exists on this daemon and, through the hub, on each remote
// host: `${apiBase(host)}/instances/<p>` is this machine's when host is null, else the
// hub's forward to that host (the daemon relays the answer as data only).
export const apiBase = (host) => (host ? `/api/v1/hosts/${encodeURIComponent(host)}` : '/api/v1')

export const getJson = (url) => request(url)

export const postJson = (url, payload) =>
  request(url, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
