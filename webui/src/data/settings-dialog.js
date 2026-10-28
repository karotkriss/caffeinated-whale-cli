// The one Settings modal (captain note 02:13Z: a modal over the page, never a page
// that looks like one). Opened from the sidebar, Ctrl+, (Cmd+, on a Mac), or a
// /settings/<section> deep link; the page underneath stays where it is.
import { reactive } from 'vue'

export const SECTIONS = ['account', 'paths', 'cache', 'daemons', 'sources', 'agents', 'health', 'about']
export const settingsDialog = reactive({ open: false, tab: 'paths' })

export function openSettings(section = null) {
  if (section && SECTIONS.includes(section)) settingsDialog.tab = section
  settingsDialog.open = true
}
