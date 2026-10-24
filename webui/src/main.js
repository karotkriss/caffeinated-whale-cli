import { createApp } from 'vue'
import { boot } from './data/session' // first: strips the #login= code before the router reads the URL
import { router } from './router'
import './style.css'
import App from './App.vue'
import { startFleet } from './data/fleet'

// Sign in first (the one-time #login= code), then open the fleet stream.
boot().then(() => startFleet())
createApp(App).use(router).mount('#app')
