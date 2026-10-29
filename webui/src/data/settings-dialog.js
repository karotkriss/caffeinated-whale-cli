// The one Settings modal (captain note 02:13Z: a modal over the page, never a page
// that looks like one). Opened from the sidebar, Ctrl+, (Cmd+, on a Mac), or a
// /settings/<section> deep link; the page underneath stays where it is.
import { reactive } from 'vue'

import { can } from './session'

export const SECTIONS = ['account', 'appearance', 'paths', 'cache', 'daemons', 'sources', 'agents', 'integrations', 'hosts', 'health', 'about']
// What only an Admin may change; everyone else sees their account, health and about.
export const ADMIN_SECTIONS = new Set(['paths', 'cache', 'daemons', 'sources', 'agents', 'integrations', 'hosts'])
export const settingsDialog = reactive({ open: false, tab: 'paths' })

export function openSettings(section = null) {
  if (section && SECTIONS.includes(section)) settingsDialog.tab = section
  if (ADMIN_SECTIONS.has(settingsDialog.tab) && !can('admin')) settingsDialog.tab = 'account'
  settingsDialog.open = true
}
