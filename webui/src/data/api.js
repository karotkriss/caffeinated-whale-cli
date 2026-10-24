// The daemon speaks the frappe-ui envelope: `{data}` on success, `{errors:[...]}`
// on failure (see commands/serve.py). These two helpers are the whole transport.
import { session } from './session'

export class ApiError extends Error {
  constructor(status, error) {
    super(error?.message || `HTTP ${status}`)
    this.status = status
    this.type = error?.type
    this.code = error?.title
    this.hint = error?.hint
    this.choice = error?.choice
    this.outcome = error?.outcome
  }
}

async function request(url, init = {}) {
  const res = await fetch(url, { headers: { Accept: 'application/json' }, ...init })
  const body = await res.json().catch(() => ({}))
  if (res.status === 401) session.signedOut = true
  if (!res.ok) throw new ApiError(res.status, body?.errors?.[0])
  return body.data
}

export const getJson = (url) => request(url)

export const postJson = (url, payload) =>
  request(url, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
