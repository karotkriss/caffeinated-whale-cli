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
export const configureIntegration = (id, settings, secrets, enable) => post({ op: 'configure', id, settings, secrets, enable })
export const disableIntegration = (id) => post({ op: 'disable', id })
export const integrationAction = (id, action, args, settings, secrets) => post({ op: 'action', id, action, args, settings, secrets })

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
