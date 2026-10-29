// Settings > Integrations (core/integrations.py through /api/v1/integrations).
// Secrets are write-only: the daemon answers which are set, never their values, and
// an empty secret in a request keeps the stored one.
import { reactive } from 'vue'
import { getJson, postJson } from './api'

export const integrations = reactive({ list: [], loading: false, error: null })

export async function loadIntegrations() {
  integrations.loading = true
  try {
    integrations.list = (await getJson('/api/v1/integrations')).integrations
    integrations.error = null
  } catch (e) {
    integrations.error = e
  } finally {
    integrations.loading = false
  }
}

const post = (payload) => postJson('/api/v1/integrations', payload)
export const testIntegration = (id, settings, secrets) => post({ op: 'test', id, settings, secrets })
// takeOver: the Admin chose to switch a hook point another integration serves (409 integration.hook_taken otherwise).
export const configureIntegration = (id, settings, secrets, enable, takeOver = false) => post({ op: 'configure', id, settings, secrets, enable, take_over: takeOver })
export const disableIntegration = (id) => post({ op: 'disable', id })
export const integrationAction = (id, action, args, settings, secrets) => post({ op: 'action', id, action, args, settings, secrets })

// Community integrations (core/community.py): every install and update names the
// exact commit the Admin reviewed on the trust screen.
export const previewCommunity = (url, ref) => post({ op: 'community_preview', url, ref })
export const addCommunity = (url, ref, commit) => post({ op: 'community_add', url, ref, commit })
export const checkCommunity = (id) => post({ op: 'community_check', id })
export const updateCommunity = (id, commit) => post({ op: 'community_update', id, commit })
export const removeCommunity = (id) => post({ op: 'community_remove', id })

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
