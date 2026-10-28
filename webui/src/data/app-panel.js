// The one app detail panel and the one install sheet for the whole app.
import { reactive } from 'vue'
import { refuseBelow } from './session'

export const appPanel = reactive({ open: false, app: null })
export const installSheet = reactive({ open: false, app: null })

export function openApp(app) {
  appPanel.app = app
  appPanel.open = true
}

export function openInstall(app) {
  if (refuseBelow()) return
  installSheet.app = app
  installSheet.open = true
}
