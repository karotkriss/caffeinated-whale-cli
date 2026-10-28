import { createApp } from 'vue'
import { boot } from './data/session' // first: strips the #login= code before the router reads the URL
import { router } from './router'
import './style.css'
import App from './App.vue'
import { startFleet } from './data/fleet'
import { isChunkLoadError, shouldReload } from './data/chunk-recovery'

// A tab opened before a rebuild lazy-loads route chunks that build replaced; one
// reload of the requested page recovers it (chunk-recovery.js owns the rule).
function recover(path) {
  if (shouldReload(window.sessionStorage, path)) window.location.assign(path)
}
window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault()
  recover(window.location.pathname + window.location.search)
})
router.onError((error, to) => {
  if (isChunkLoadError(error)) recover(to?.fullPath || window.location.pathname)
})

// Ask who is signed in first; startFleet then opens the stream whenever the session is usable.
boot().then(() => startFleet())
createApp(App).use(router).mount('#app')
