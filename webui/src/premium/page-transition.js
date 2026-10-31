// Page navigation as a same-document View Transition (the pattern Nuxt's view-transition
// plugin and Turbo use): a `beforeResolve` guard starts the transition, the route changes
// inside its update callback, and the callback's promise resolves once the next page has
// been rendered. The browser keeps the old page on screen until then and cross-fades the
// two snapshots on the compositor, so no frame between them is ever painted - the blank
// window the old `out-in` <Transition> had (old page faded out and removed, THEN the new
// one inserted at opacity 0) cannot exist. Without the API, with visual effects off, or
// under reduced motion there is no transition and the swap is atomic: Vue removes the old
// page and inserts the new one in one flush. Kept free of Vue and the DOM module graph so
// tests/test_webui_page_transition.py can drive it under node with a fake router/document.

// A transition plays only between two real pages at different paths: not on the first
// navigation (nothing to fade from), and not on a query-only change (a tab switch keeps
// the page mounted, see Shell.vue's `route.path` key).
export function shouldTransition({ to, from, motion, supported }) {
  return !!(supported && motion && from.matched.length && to.matched.length && to.path !== from.path)
}

// `motion` answers whether motion is wanted right now (effects on, no reduced-motion ask);
// `settle` returns a promise that resolves after the next page has been flushed to the DOM
// (Vue's nextTick). `data-vt='page'` on <html> selects the 160 ms cross-fade in premium.css;
// it is never cleared afterwards, as the scheme transition's value is not either.
export function installPageTransition(router, { motion, settle, doc = document }) {
  let finish = null
  const release = () => {
    const f = finish
    finish = null
    if (f) f()
  }
  router.beforeResolve((to, from) => {
    const supported = typeof doc.startViewTransition === 'function'
    if (!shouldTransition({ to, from, motion: motion(), supported })) return undefined
    release() // a navigation that outran the previous one's render
    return new Promise((proceed) => {
      doc.documentElement.dataset.vt = 'page'
      const rendered = new Promise((resolve) => (finish = resolve))
      const vt = doc.startViewTransition(() => {
        proceed()
        return rendered
      })
      vt.ready.catch(() => {}) // skipped by a newer transition: nothing to undo
      vt.finished.catch(() => {})
    })
  })
  // The route has changed (or the navigation failed): let the snapshot be taken once the
  // page is in the DOM. A failed navigation releases at once so the old page unfreezes.
  router.afterEach((to, from, failure) => {
    if (!finish) return
    if (failure) return release()
    settle().then(release, release)
  })
  router.onError(release)
}
