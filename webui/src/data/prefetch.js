// Route chunks are built per page and fetched on first visit. The router resolves a chunk
// before the route changes, so a cold page is never blank, only slower to arrive; this
// warms every page's chunk while the browser is idle after the first page is up, through
// the router's own lazy loaders, so the navigation finds the module already cached.
// A prefetch that fails (a tab older than a rebuild, whose chunks the build replaced) is
// swallowed and stops the sweep; it must never reload the page by itself. While the sweep
// runs, a failed preload is therefore handed back to the request that made it instead of
// reloading: the sweep swallows its own, and a real navigation's reaches router.onError,
// which keeps the once-only reload rule in chunk-recovery.js.
export const prefetch = { active: false }

// Called with Vite's `vite:preloadError` event; true when the listener should reload now.
export function preloadErrorReloads(event) {
  if (prefetch.active) return false
  event.preventDefault()
  return true
}

function idle(fn) {
  if (typeof requestIdleCallback === 'function') requestIdleCallback(fn, { timeout: 4000 })
  else setTimeout(fn, 1000)
}

// The loaders of every lazy route, in route order, each once.
export function routeLoaders(router) {
  const seen = new Set()
  const out = []
  for (const r of router.getRoutes()) {
    const loader = r.components?.default
    if (typeof loader !== 'function' || loader.length !== 0 || seen.has(loader)) continue
    seen.add(loader)
    out.push(loader)
  }
  return out
}

export function prefetchPages(router, { schedule = idle } = {}) {
  const loaders = routeLoaders(router)
  return new Promise((done) => {
    const next = async () => {
      const loader = loaders.shift()
      if (!loader) return done(true)
      prefetch.active = true
      try {
        await loader()
      } catch {
        return done(false)
      } finally {
        prefetch.active = false
      }
      schedule(next)
    }
    schedule(next)
  })
}
