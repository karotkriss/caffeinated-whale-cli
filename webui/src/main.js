import { createApp, nextTick } from 'vue'
import { boot } from './data/session'
import { router } from './router'
import './style.css'
import './premium/premium.css'
import { motion } from './premium/effects' // boots the colour scheme and the data-effects flag before the first paint
import { installPageTransition } from './premium/page-transition'
import App from './App.vue'
import { startFleet } from './data/fleet'
import { isChunkLoadError, shouldReload } from './data/chunk-recovery'
import { prefetchPages, preloadErrorReloads } from './data/prefetch'

// A tab opened before a rebuild lazy-loads route chunks that build replaced; one
// reload of the requested page recovers it (chunk-recovery.js owns the rule). The idle
// prefetch of route chunks never reloads for its own failure (data/prefetch.js).
function recover(path) {
  if (shouldReload(window.sessionStorage, path)) window.location.assign(path)
}
window.addEventListener('vite:preloadError', (event) => {
  if (preloadErrorReloads(event)) recover(window.location.pathname + window.location.search)
})
router.onError((error, to) => {
  if (isChunkLoadError(error)) recover(to?.fullPath || window.location.pathname)
})

// Page navigation holds the old page until the next one has rendered, then cross-fades.
installPageTransition(router, { motion: () => motion.value, settle: nextTick })

// Ask who is signed in first; startFleet then opens the stream whenever the session is usable,
// and the other pages' chunks are warmed while the browser is idle.
boot().then(() => {
  startFleet()
  prefetchPages(router)
})
createApp(App).use(router).mount('#app')
