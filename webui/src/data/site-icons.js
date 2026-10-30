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

// A remote icon names the remote's own logo route (core.app_catalog serves every
// logo at /api/v1/apps/logo/<file>), which the hub forwards. A remote chooses these
// bytes, so any other URL shows no icon: a path that climbs out of the hub's forward
// would make this page fetch a route of the hub itself.
const LOGO = /^\/api\/v1\/apps\/logo\/([A-Za-z0-9._-]+)$/
function remoteLogo(logo, host) {
  const file = LOGO.exec(String(logo?.url || ''))?.[1]
  return file && file !== '.' && file !== '..' ? { ...logo, url: `${apiBase(host)}/apps/logo/${file}` } : null
}

export function siteIcon(project, site, host = null) {
  if (host) return remoteLogo(siteIcons.remote[host]?.[`${project}/${site}`]?.logo, host)
  return siteIcons.byKey[`${project}/${site}`]?.logo || null
}
