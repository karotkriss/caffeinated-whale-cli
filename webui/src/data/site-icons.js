import { reactive } from 'vue'
import { getJson, postJson } from './api'

// Site favicons (core.site_icons): keyed `<project>/<site>`, served same-origin.
export const siteIcons = reactive({ byKey: {}, loaded: false, refreshing: false })

export async function loadSiteIcons() {
  const data = await getJson('/api/v1/sites/icons')
  siteIcons.byKey = data.icons || {}
  siteIcons.loaded = true
}

// Ask the daemon to (re)fetch the icons of these sites in the background; the
// `site_icons` stream event delivers the result.
export async function refreshSiteIcons(sites, { force = false } = {}) {
  if (!sites.length) return
  siteIcons.refreshing = true
  try {
    await postJson('/api/v1/sites/icons', { sites, force })
  } finally {
    setTimeout(() => (siteIcons.refreshing = false), 1500)
  }
}

export function siteIconsLanded(frame) {
  if (frame.icons) siteIcons.byKey = frame.icons
  siteIcons.refreshing = false
}

export function siteIcon(project, site) {
  return siteIcons.byKey[`${project}/${site}`]?.logo || null
}
