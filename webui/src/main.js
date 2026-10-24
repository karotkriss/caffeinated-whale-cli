import { createApp } from 'vue'
import { router } from './router'
import './style.css'
import App from './App.vue'
import { boot } from './data/session'
import { startFleet } from './data/fleet'

// Sign in first (the one-time #login= code), then open the fleet stream.
boot().then(() => startFleet())
createApp(App).use(router).mount('#app')
