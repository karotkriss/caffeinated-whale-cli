// The one app detail panel and the one install sheet for the whole app.
import { reactive } from 'vue'
import { catalogApp } from './catalog'
import { refuseBelow } from './session'

export const appPanel = reactive({ open: false, app: null })
export const installSheet = reactive({ open: false, app: null })

export function openApp(app) {
  appPanel.app = app
  appPanel.open = true
}

// The panel is a view over THIS machine's catalog, so a remote host's app row gets no opener
// (no pointer, no panel); the row's own menu actions still carry the host.
export const appOpener = (host, name) => (host ? undefined : () => openApp(catalogApp(name)))

export function openInstall(app) {
  if (refuseBelow()) return
  installSheet.app = app
  installSheet.open = true
}
