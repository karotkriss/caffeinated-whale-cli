import { createRouter, createWebHistory } from 'vue-router'
import { page } from './pages'

// A remote host's instance, bench, site and job pages live under /hosts/:host/...:
// the same page components, reading the host from the route and sending every
// request to that host's daemon through the hub's forward.
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
    { path: '/hosts/:host/instances/:project', name: 'host-instance', component: page('Instance') },
    { path: '/hosts/:host/instances/:project/benches/:bench', name: 'host-bench', component: page('Bench') },
    { path: '/hosts/:host/instances/:project/benches/:bench/sites/:site', name: 'host-site', component: page('Site') },
    { path: '/jobs', name: 'jobs', component: page('Jobs') },
    { path: '/jobs/:id', name: 'job', component: page('Job') },
    { path: '/hosts/:host/jobs/:id', name: 'host-job', component: page('Job') },
    { path: '/advisories', name: 'advisories', component: page('Advisories') },
    { path: '/backups', name: 'backups', component: page('Backups') },
    { path: '/schedules', name: 'schedules', component: page('Schedules') },
    { path: '/settings/:section?', name: 'settings', component: { render: () => null } },
    { path: '/:pathMatch(.*)*', redirect: '/instances' },
  ],
})
