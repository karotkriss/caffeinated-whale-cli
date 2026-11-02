// Settings > Integrations (core/integrations.py through /api/v1/integrations).
// Secrets are write-only: the daemon answers which are set, never their values, and
// an empty secret in a request keeps the stored one.
//
// `host` is the machine whose integrations the panel shows: null for this machine,
// else a remote host's id. A remote's list, tests, saves and actions go to that
// host's own daemon through the hub's forward (`apiBase(host)`), so its secrets are
// sealed there and never pass through this machine's store; Detect runs there too.
import { reactive } from 'vue'
import { apiBase, getJson, postJson } from './api'

export const integrations = reactive({ list: [], loading: false, error: null, host: null })

// Show (and act on) one machine's integrations: null is this machine.
export function pickIntegrationsHost(host) {
  if ((host || null) === integrations.host) return
  integrations.host = host || null
  integrations.list = []
  integrations.error = null
}

export async function loadIntegrations() {
  const host = integrations.host
  integrations.loading = true
  try {
    const list = (await getJson(`${apiBase(host)}/integrations`)).integrations
    if (host !== integrations.host) return // the picker moved on while this read ran
    integrations.list = list
    integrations.error = null
  } catch (e) {
    if (host === integrations.host) integrations.error = e
  } finally {
    if (host === integrations.host) integrations.loading = false
  }
}

const post = (payload) => postJson(`${apiBase(integrations.host)}/integrations`, payload)
export const testIntegration = (id, settings, secrets) => post({ op: 'test', id, settings, secrets })
// takeOver: the Admin chose to switch a hook point another integration serves (409 integration.hook_taken otherwise).
export const configureIntegration = (id, settings, secrets, enable, takeOver = false) => post({ op: 'configure', id, settings, secrets, enable, take_over: takeOver })
export const disableIntegration = (id) => post({ op: 'disable', id })
export const integrationAction = (id, action, args, settings, secrets) => post({ op: 'action', id, action, args, settings, secrets })

// Community integrations (core/community.py): every install and update names the
// exact commit the Admin reviewed on the trust screen. Always this machine's: a
// remote installs code only through its own Admin (its bridge refuses a hub's).
const postLocal = (payload) => postJson('/api/v1/integrations', payload)
export const previewCommunity = (url, ref) => postLocal({ op: 'community_preview', url, ref })
export const addCommunity = (url, ref, commit) => postLocal({ op: 'community_add', url, ref, commit })
export const checkCommunity = (id) => postLocal({ op: 'community_check', id })
export const updateCommunity = (id, commit) => postLocal({ op: 'community_update', id, commit })
export const removeCommunity = (id) => postLocal({ op: 'community_remove', id })

// mode: 'add' | 'update' | 'remove'; id names the installed one for update and remove.
export const communityDialog = reactive({ open: false, mode: 'add', id: null })
export function openCommunity(mode, id = null) {
  communityDialog.mode = mode
  communityDialog.id = id
  communityDialog.open = true
}

export const HOOK_POINT = {
  sign_in: 'Sign-in provider',
  backup_target: 'Backup target',
  dns_record: 'DNS records',
  proxy_host: 'Proxy',
}
// A session signed in through a sign-in integration (LDAP or a community one).
export const isDirectorySource = (source) => !!source && source !== 'local' && source !== 'api'

// The dialog that sets one up; Settings > Integrations opens it.
export const integrationDialog = reactive({ open: false, id: null })
export function openIntegration(id) {
  integrationDialog.id = id
  integrationDialog.open = true
}

export const STATE = {
  off: { label: 'Off', theme: 'gray' },
  needs_setup: { label: 'Needs setup', theme: 'amber' },
  on: { label: 'On', theme: 'green' },
  error: { label: 'Error', theme: 'red' },
}
export const PUBLISHER = {
  official: { label: 'Official', theme: 'blue' },
  community: { label: 'Community, not reviewed', theme: 'amber' },
}
