import { createRouter, createWebHistory } from 'vue-router'

const page = (name) => () => import(`./pages/${name}.vue`)

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/instances' },
    { path: '/instances', name: 'instances', component: page('Instances') },
    { path: '/instances/:project', name: 'instance', component: page('Instance') },
    { path: '/instances/:project/benches/:bench', name: 'bench', component: page('Bench') },
    { path: '/instances/:project/benches/:bench/sites/:site', name: 'site', component: page('Site') },
    { path: '/jobs', name: 'jobs', component: page('Jobs') },
    { path: '/jobs/:id', name: 'job', component: page('Job') },
    { path: '/health', name: 'health', component: page('Health') },
    { path: '/settings', name: 'settings', component: page('Settings') },
    { path: '/:pathMatch(.*)*', redirect: '/instances' },
  ],
})
