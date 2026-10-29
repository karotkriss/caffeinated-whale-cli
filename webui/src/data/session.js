// Sign-in and the protocol check.
//
// The page signs in with a username and password (POST /api/v1/session); the
// daemon answers with an HttpOnly session cookie and whether the account still
// has to pick its first password. Nothing about the session is ever in a URL.
import { reactive } from 'vue'
import { toast } from 'frappe-ui'

export const PROTOCOL = 9 // the daemon's PROTOCOL_VERSION this page was built against

export const session = reactive({
  booted: false,
  signedOut: false,
  user: null, // { username, mustChange, role, source, name }
  meta: null, // { version, protocol, build }
  protocolMismatch: false,
})

// Resolves once boot has asked who is signed in: anything that calls the API on
// mount awaits this, or its first request races the check and comes back 401.
let markReady
export const ready = new Promise((resolve) => (markReady = resolve))

export async function boot() {
  try {
    await whoami()
    await recheckMeta()
  } finally {
    session.booted = true
    markReady()
  }
}

function setUser(data) {
  session.user = data
    ? { username: data.username, mustChange: !!data.must_change_password, role: data.role || 'admin', source: data.source || 'local', name: data.name || '' }
    : null
  session.signedOut = !data
}

// Roles (see core/accounts.py): a Viewer reads, an Operator also runs the fleet, an
// Admin also changes settings and integrations, removes things and updates cwcli.
// The daemon enforces every one of these; the page only stops offering what would be refused.
export const ROLE_LABEL = { admin: 'Admin', operator: 'Operator', viewer: 'Viewer' }
const RANK = { viewer: 0, operator: 1, admin: 2 }
export const can = (role) => RANK[session.user?.role || 'admin'] >= RANK[role]

// The guard every "start a change" dialog opener runs first: a Viewer is told why
// instead of filling in a form the daemon would refuse.
export function refuseBelow(role = 'operator') {
  if (can(role)) return false
  toast.error(`Needs the ${ROLE_LABEL[role]} role`, { description: `You are signed in as a ${ROLE_LABEL[session.user?.role] || 'Viewer'}; ask an Admin if you need this.` })
  return true
}

export async function whoami() {
  try {
    const res = await fetch('/api/v1/session', { cache: 'no-store' })
    if (res.status === 401) return setUser(null)
    const body = await res.json()
    setUser(body.data)
  } catch {
    setUser(null)
  }
}

async function post(url, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const e = body?.errors?.[0] || {}
    const err = new Error(e.message || `HTTP ${res.status}`)
    err.code = e.title
    err.hint = e.hint || undefined
    err.retryAfter = e.retry_after
    throw err
  }
  return body.data
}

export async function signIn(username, password, remember) {
  const data = await post('/api/v1/session', { username, password, remember: !!remember })
  setUser(data)
  return data
}

export async function changePassword(current, next) {
  await post('/api/v1/session/password', { current, new: next })
  if (session.user) session.user.mustChange = false
}

export async function signOut({ others = false } = {}) {
  const data = await post('/api/v1/session/signout', { others })
  if (!others) setUser(null)
  return data
}

export const listSessions = async () => (await (await fetch('/api/v1/session/sessions', { cache: 'no-store' })).json()).data

// Called at boot and whenever the event stream reconnects. A self-update
// replaces the daemon, which drops every stream; the reconnect that follows
// sees a new version or protocol and reloads the page, with no polling between.
export async function recheckMeta() {
  try {
    const res = await fetch('/api/v1/meta', { cache: 'no-store' })
    const meta = await res.json()
    const prior = session.meta
    if (
      prior &&
      (meta.protocol !== prior.protocol || meta.version !== prior.version || meta.build !== prior.build)
    ) {
      location.reload()
      return
    }
    session.meta = meta
    session.protocolMismatch = meta.protocol !== PROTOCOL
  } catch {
    session.meta = null
  }
}
