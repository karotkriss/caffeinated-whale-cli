// Remote hosts (core/hosts.py through /api/v1/hosts): other machines whose cwcli
// daemons this hub shows and drives. Each host's instances arrive on the same stream
// as the local ones, tagged with the host's id; this store keeps the link state the
// page labels them with. Nothing here acts on a host by itself: a quiet host is shown
// as unreachable with its last snapshot kept, and every action stays with the person.
import { computed, reactive } from 'vue'
import { getJson, postJson } from './api'

export const hosts = reactive({ hub: null, byId: {}, loaded: false, loading: false, error: null })

export const hostList = computed(() => Object.values(hosts.byId).sort((a, b) => a.name.localeCompare(b.name)))
export const hasHosts = computed(() => hostList.value.length > 0)
export const host = (id) => (id ? hosts.byId[id] || null : null)
export const hostName = (id) => hosts.byId[id]?.name || id || 'this machine'
// Actions on a remote need its link `connected`, which includes an equal protocol.
export const actionsAllowed = (id) => !id || !!hosts.byId[id]?.actions_allowed
export const hostReadable = (id) => !id || !!hosts.byId[id]?.readable

export async function loadHosts() {
  hosts.loading = true
  try {
    const data = await getJson('/api/v1/hosts')
    hosts.hub = data.hub
    hosts.byId = Object.fromEntries((data.hosts || []).map((h) => [h.id, h]))
    hosts.loaded = true
    hosts.error = null
  } catch (e) {
    hosts.error = e
  } finally {
    hosts.loading = false
  }
}

// The stream's first frame carries every host (with its instances and jobs, which
// fleet.js files), so a host it does not list is gone; later `host_link` frames
// carry one host's new link state.
const linkOnly = ({ instances, jobs, ...rest }) => rest
export function replaceHosts(views) {
  hosts.byId = Object.fromEntries(views.map((view) => [view.id, linkOnly(view)]))
  hosts.loaded = true
}
export function applyHostLink(view) {
  hosts.byId[view.id] = { ...(hosts.byId[view.id] || {}), ...linkOnly(view) }
  hosts.loaded = true
}
export function dropHost(id) {
  delete hosts.byId[id]
}

const post = (payload) => postJson('/api/v1/hosts', payload)
export const addHost = (fields) => post({ op: 'add', ...fields })
export const removeHost = (id) => post({ op: 'remove', id })
export const checkHost = (id) => post({ op: 'check', id })
export const repinHost = (id, host_key) => post({ op: 'repin', id, host_key })

// The link states (core/hosts.py). Machine words keep their spelling in tooltips; the
// label is the short human form the page shows beside the host's name.
export const LINK = {
  connecting: { label: 'connecting', theme: 'gray', dot: 'border border-outline-gray-4' },
  connected: { label: 'connected', theme: 'green', dot: 'bg-surface-green-7' },
  unreachable: { label: 'unreachable', theme: 'amber', dot: 'bg-surface-amber-7' },
  auth_refused: { label: 'no longer accepts this hub', theme: 'red', dot: 'bg-surface-red-7' },
  host_key_changed: { label: 'host key changed', theme: 'red', dot: 'bg-surface-red-7' },
  daemon_down: { label: 'cwcli not running there', theme: 'amber', dot: 'bg-surface-amber-7' },
  version_mismatch: { label: 'different cwcli version', theme: 'amber', dot: 'bg-surface-amber-7' },
  transport_off: { label: 'integration off', theme: 'gray', dot: 'bg-surface-gray-5' },
  service_account_only: { label: 'service account only', theme: 'gray', dot: 'bg-surface-gray-5' },
}
export const linkLook = (state) => LINK[state] || LINK.connecting

// What a person can do about each state, in one sentence.
export function linkAdvice(h) {
  if (!h) return ''
  switch (h.state) {
    case 'connected': return `Live. Actions run on ${h.name} up to the ${h.cap || 'granted'} role it granted this hub.`
    case 'connecting': return 'Connecting now.'
    case 'unreachable': return `${h.name} stopped answering${h.reason ? ` (${h.reason})` : ''}. Its instances show their last-known state as unknown; nothing is done about it by itself.`
    case 'auth_refused': return `${h.name} no longer accepts this hub's key. Its owner can allow it again with cwcli remote allow there.`
    case 'host_key_changed': return `${h.name} presented a different SSH host key than the one pinned. Nothing reconnects by itself: confirm the change with its owner, then pin the new key.`
    case 'daemon_down': return `SSH works, but cwcli is not running on ${h.name}${h.reason ? ` (${h.reason})` : ''}.`
    case 'version_mismatch': return `${h.reason || 'The two machines run different cwcli protocols'}. Reads work; actions wait until both sides match.`
    case 'transport_off': return 'The Remote hosts over SSH integration is off; turn it on in Settings > Integrations.'
    default: return h.reason || ''
  }
}

// The one Add host dialog for the whole app (components/AddHostDialog.vue).
export const addHostDialog = reactive({ open: false })
export const openAddHost = () => (addHostDialog.open = true)

// A pairing string is base64url JSON behind a prefix; the page reads it only to
// prefill the form the Admin reviews. The daemon validates everything again.
export function readPairing(text) {
  const value = String(text || '').trim()
  if (!value.startsWith('cwcli-remote:v1:')) return null
  try {
    const b64 = value.slice('cwcli-remote:v1:'.length).replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(Array.from(atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4)), (c) => '%' + c.charCodeAt(0).toString(16).padStart(2, '0')).join(''))
    const data = JSON.parse(json)
    return data && typeof data === 'object' ? data : null
  } catch {
    return null
  }
}
