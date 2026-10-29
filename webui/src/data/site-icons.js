import { reactive } from 'vue'
import { apiBase, getJson, postJson } from './api'

// Site favicons (core.site_icons): keyed `<project>/<site>`, served same-origin. A
// remote host's icons come from its own daemon through the hub's forward (each
// image relayed under the logo route's sandboxing policy), kept per host.
export const siteIcons = reactive({ byKey: {}, remote: {}, loaded: false, refreshing: false })

export async function loadSiteIcons() {
  const data = await getJson('/api/v1/sites/icons')
  siteIcons.byKey = data.icons || {}
  siteIcons.loaded = true
}

// Ask the daemon to (re)fetch the icons of these sites in the background; the
// `site_icons` stream event delivers the result (tagged with the host for a remote).
export async function refreshSiteIcons(sites, { force = false, host = null } = {}) {
  if (!sites.length) return
  siteIcons.refreshing = true
  try {
    await postJson(`${apiBase(host)}/sites/icons`, { sites, force })
  } finally {
    setTimeout(() => (siteIcons.refreshing = false), 1500)
  }
}

export function siteIconsLanded(frame, host = null) {
  if (frame.icons) {
    if (host) siteIcons.remote[host] = frame.icons
    else siteIcons.byKey = frame.icons
  }
  siteIcons.refreshing = false
}

// A remote icon's URL names the remote's own logo route; the hub forwards it.
function rewrite(logo, host) {
  if (!logo || !host) return logo
  const url = String(logo.url || '')
  return url.startsWith('/api/v1/') ? { ...logo, url: `${apiBase(host)}/${url.slice('/api/v1/'.length)}` } : null
}

export function siteIcon(project, site, host = null) {
  if (host) return rewrite(siteIcons.remote[host]?.[`${project}/${site}`]?.logo, host) || null
  return siteIcons.byKey[`${project}/${site}`]?.logo || null
}
