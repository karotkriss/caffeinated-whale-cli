import { createRouter, createWebHistory } from 'vue-router'

const page = (name) => () => import(`./pages/${name}.vue`)

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/instances' },
    { path: '/instances', name: 'instances', component: page('Instances') },
    { path: '/apps', name: 'apps', component: page('Apps') },
    { path: '/sites', name: 'sites', component: page('Sites') },
    { path: '/instances/:project', name: 'instance', component: page('Instance') },
    { path: '/instances/:project/benches/:bench', name: 'bench', component: page('Bench') },
    { path: '/instances/:project/benches/:bench/sites/:site', name: 'site', component: page('Site') },
    { path: '/jobs', name: 'jobs', component: page('Jobs') },
    { path: '/jobs/:id', name: 'job', component: page('Job') },
    { path: '/advisories', name: 'advisories', component: page('Advisories') },
    { path: '/backups', name: 'backups', component: page('Backups') },
    { path: '/schedules', name: 'schedules', component: page('Schedules') },
    { path: '/settings/:section?', name: 'settings', component: { render: () => null } },
    { path: '/:pathMatch(.*)*', redirect: '/instances' },
  ],
})
