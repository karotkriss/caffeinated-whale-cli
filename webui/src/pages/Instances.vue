<script setup>
// Direction A: Pilot's Instances list. Compact cards in a two-column grid, a meta
// line instead of columns, status quiet when healthy (a dot and a word only
// when an instance is not running), the toolbar only once the list is long,
// everything in a centred reading column. With remote hosts linked, one list
// holds every machine's instances, each remote one labelled by its host, and a
// strip above the grid shows each host's link; a host that stopped answering keeps
// its last-known instances, shown as unknown with the time it was last seen.
import { computed, onMounted, ref } from 'vue'
import { Button, Dropdown, Badge, Dialog, Select, TextInput, Tooltip, toast } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import FleetPerformance from '../components/FleetPerformance.vue'
import RemoteHostBadge from '../components/RemoteHostBadge.vue'
import { loadUpdates, updatesFor } from '../data/updates'
import { createJob, activeJobs } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { fleet, instances, details, publishedRange, countSites, cacheProjects, setFocus, instancePath, ago } from '../data/fleet'
import { applySetting, settings } from '../data/settings'
import { menuOption } from '../data/dialog'
import { openJob } from '../data/job-dialog'
import { can, session } from '../data/session'
import { hostList, hasHosts, hostName, hostReadable, linkLook, linkAdvice, openAddHost, checkHost, removeHost, applyHostLink, dropHost, loadHosts } from '../data/hosts'
import { openSettings } from '../data/settings-dialog'
// --- premium layer (see webui/src/premium/): the fleet hero ---
import { Motion } from 'motion-v'
import { useNow } from '@vueuse/core'
import ShaderGradient from '../premium/ShaderGradient.vue'
import { active, motion, stops, effects } from '../premium/effects'
const EASE = [0.23, 1, 0.32, 1]

onMounted(() => {
  loadUpdates()
  setFocus(null)
  cacheProjects.load()
  loadHosts()
})
const query = ref('')
const where = ref('all') // all | local | <host id>
const activeJobsLabel = computed(() => (activeJobs.value.length ? `${activeJobs.value.length} active ${activeJobs.value.length === 1 ? 'job' : 'jobs'}` : 'no active jobs'))
const up = computed(() => instances.value.filter((i) => i.container_running).length)
// The hero label: a time-of-day greeting by the browser's local clock with the signed-in name.
const now = useNow({ interval: 60000 })
const greeting = computed(() => {
  const h = now.value.getHours()
  const part = h >= 5 && h < 12 ? 'Good morning' : h >= 12 && h < 18 ? 'Good afternoon' : 'Good evening'
  const who = session.user?.name || session.user?.username
  return who ? `${part}, ${who}` : part
})
const benchCount = computed(() => instances.value.reduce((n, i) => n + (details[i.key]?.data?.benches.length || 0), 0))
const siteTotal = computed(() => instances.value.reduce((n, i) => n + countSites(details[i.key]?.data), 0))
const machines = computed(() => 1 + hostList.value.length)
const live = computed(() => new Set(instances.value.filter((i) => !i.host).map((i) => i.project)))
const remembered = computed(() => (cacheProjects.data || []).filter((p) => !live.value.has(p.name)).sort((a, b) => String(b.last_updated).localeCompare(String(a.last_updated))))
const showRemembered = ref(false)
const forgetting = ref('')
async function forget(name) {
  forgetting.value = name
  try { await applySetting('cache.clear', { project: name }, { done: `Forgot ${name}`, refresh: false }); await cacheProjects.load(true) } finally { forgetting.value = '' }
}
const rows = computed(() => {
  const q = query.value.trim().toLowerCase()
  return instances.value.filter((i) => {
    if (where.value === 'local' ? i.host : where.value !== 'all' && i.host !== where.value) return false
    return !q || i.project.toLowerCase().includes(q) || hostName(i.host).toLowerCase().includes(q)
  })
})
const whereOptions = computed(() => [
  { label: 'Every machine', value: 'all' },
  { label: 'This machine', value: 'local' },
  ...hostList.value.map((h) => ({ label: h.name, value: h.id })),
])
const meta = (i) => {
  const d = details[i.key]?.data
  const parts = []
  if (d) parts.push(`${d.benches.length} ${d.benches.length === 1 ? 'bench' : 'benches'}`, `${countSites(d)} ${countSites(d) === 1 ? 'site' : 'sites'}`)
  else parts.push(details[i.key]?.loading ? 'reading' : 'not cached')
  const web = publishedRange(i.ports).web
  if (web) parts.push(`ports ${web}`)
  return parts.join(' · ')
}
// A remote whose link is not readable shows its last snapshot: every instance unknown,
// with the host's last-seen time instead of a live word.
const stale = (i) => !!i.host && !hostReadable(i.host)
const lastSeen = (i) => {
  const h = hostList.value.find((x) => x.id === i.host)
  return h?.last_seen ? `last seen ${ago(h.last_seen * 1000)}` : 'not seen yet'
}
const QUIET = { running: null, online: 'started, not serving', degraded: 'degraded', offline: 'stopped', unknown: 'probing' }
const DOT = { running: 'bg-surface-green-7', online: 'bg-surface-blue-7', degraded: 'bg-surface-amber-7', offline: 'bg-surface-gray-5', unknown: 'border border-outline-gray-4' }
const menu = (i) => [
  menuOption(i.container_running ? 'instance.stop' : 'instance.start', { project: i.project, host: i.host }),
  menuOption('instance.restart', { project: i.project, host: i.host }),
  menuOption('instance.refresh', { project: i.project, host: i.host }),
  menuOption('instance.scale', { project: i.project, host: i.host }),
]

// --- the hosts strip ---
const checking = ref('')
async function check(h) {
  checking.value = h.id
  try { const data = await checkHost(h.id); if (data.host) applyHostLink(data.host) } catch (e) { toast.error(`Could not check ${h.name}`, { description: e.message }) } finally { checking.value = '' }
}
const removing = ref(null)
const removeBusy = ref(false)
async function confirmRemove() {
  removeBusy.value = true
  try { await removeHost(removing.value.id); dropHost(removing.value.id); removing.value = null } catch (e) { toast.error(`Could not remove ${removing.value.name}`, { description: e.message }) } finally { removeBusy.value = false }
}
const hostMenu = (h) => [
  ...(can('admin') ? [{ label: 'Check now', icon: 'lucide-refresh-cw', onClick: () => check(h) }] : []),
  ...(h.actions_allowed && can('operator') ? [{ label: `New instance on ${h.name}`, icon: 'lucide-plus', onClick: () => openJob('init', { host: h.id }) }] : []),
  ...(can('admin')
    ? [
        { label: 'Settings', icon: 'lucide-settings', onClick: () => openSettings('hosts') },
        { label: 'Remove host…', icon: 'lucide-trash-2', theme: 'red', onClick: () => (removing.value = h) },
      ]
    : []),
]
</script>

<template>
  <PageHead title="Instances" :count="instances.length || null">
    <template v-if="can('operator')" #actions>
      <Button variant="solid" icon-left="lucide-plus" label="New instance" @click="openJob('init')" />
      <Button v-if="can('admin')" class="max-sm:hidden" icon-left="lucide-server" label="Add host" @click="openAddHost" />
      <Button v-if="can('admin')" class="sm:hidden" icon="lucide-server" aria-label="Add host" @click="openAddHost" />
      <!-- Captain note 41: the updates check lives here, as a job; the card badge says the rest. -->
      <Dropdown :options="[{ label: 'Check for updates', icon: 'lucide-refresh-cw', onClick: async () => trackJob((await createJob({ verb: 'check-updates', target: {}, args: {} })).id) }]" align="end">
        <Button variant="ghost" icon="lucide-ellipsis" aria-label="More" />
      </Dropdown>
    </template>
  </PageHead>
  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <section v-if="active && instances.length" class="cw-c-hero relative overflow-hidden rounded-7 border border-outline-gray-1">
      <div class="absolute inset-0" aria-hidden="true"><ShaderGradient :stops="stops" :animate="motion" :scale="0.8" /></div>
      <div class="cw-c-scrim absolute inset-0" aria-hidden="true" />
      <Motion as="div" class="relative flex flex-wrap items-end justify-between gap-4 px-5 py-6 sm:px-7 sm:py-8" :initial="{ opacity: 0, transform: effects.reducedMotion ? 'none' : 'translateY(8px)' }" :animate="{ opacity: 1, transform: 'translateY(0px)' }" :transition="{ duration: 0.28, ease: EASE }">
        <div>
          <p class="text-xs font-medium uppercase tracking-wide text-ink-gray-7">{{ greeting }}</p>
          <h2 class="mt-1 text-3xl font-semibold leading-tight tracking-tight text-ink-gray-9 sm:text-4xl">{{ up }} of {{ instances.length }} {{ instances.length === 1 ? 'instance' : 'instances' }} running</h2>
          <p class="mt-1.5 text-base text-ink-gray-7">{{ benchCount }} {{ benchCount === 1 ? 'bench' : 'benches' }} · {{ siteTotal }} {{ siteTotal === 1 ? 'site' : 'sites' }}<template v-if="hasHosts"> · {{ machines }} machines</template> · {{ activeJobsLabel }}</p>
        </div>
      </Motion>
    </section>

    <!-- Remote hosts: one row per machine with its link state; this machine needs no row. -->
    <section v-if="hasHosts" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <div v-for="h in hostList" :key="h.id" class="flex items-center gap-3 rounded-6 border border-outline-gray-1 bg-surface-elevation-1 p-3">
        <span class="flex size-10 shrink-0 items-center justify-center rounded-lg border border-outline-gray-1 bg-surface-gray-1 text-ink-gray-6"><span class="lucide-server size-5" aria-hidden="true" /></span>
        <span class="min-w-0 flex-1">
          <span class="flex items-center gap-2">
            <span class="truncate text-base-medium text-ink-gray-9">{{ h.name }}</span>
            <Tooltip :text="linkAdvice(h)"><Badge size="sm" :theme="linkLook(h.state).theme" :variant="h.state === 'connected' ? 'subtle' : 'outline'" :label="linkLook(h.state).label" /></Tooltip>
          </span>
          <span class="mt-0.5 block truncate font-mono text-xs text-ink-gray-5">{{ h.user }}@{{ h.address }}{{ h.port !== 22 ? ':' + h.port : '' }}</span>
          <span class="block truncate text-xs text-ink-gray-5">
            <template v-if="h.cap">{{ h.cap }} cap</template><template v-else>cap not read yet</template>
            <template v-if="h.remote?.version"> · cwcli {{ h.remote.version }}</template>
            <template v-if="h.state !== 'connected' && h.last_seen"> · last seen {{ ago(h.last_seen * 1000) }}</template>
            <template v-if="h.drops_10m"> · {{ h.drops_10m }} {{ h.drops_10m === 1 ? 'drop' : 'drops' }} in 10 min</template>
          </span>
        </span>
        <Dropdown v-if="hostMenu(h).length" :options="hostMenu(h)" align="end">
          <Button size="sm" variant="ghost" icon="lucide-ellipsis" aria-label="Host actions" :loading="checking === h.id" />
        </Dropdown>
      </div>
    </section>

    <div v-if="instances.length > 10 || hasHosts" class="flex flex-wrap items-center gap-2">
      <TextInput v-model="query" class="w-64" type="search" placeholder="Search instances">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <Select v-if="hasHosts" v-model="where" :options="whereOptions" />
      <span class="text-sm text-ink-gray-5">{{ rows.length }} of {{ instances.length }}</span>
    </div>
    <div v-if="fleet.connection === 'connecting' && !instances.length" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      <div v-for="i in 4" :key="i" class="h-20 animate-pulse rounded-6 border border-outline-gray-1 bg-surface-gray-1" />
    </div>
    <EmptyState v-else-if="!instances.length" icon="lucide-boxes" :title="hasHosts ? 'No instances on any linked machine' : 'No instances on this Docker daemon'" description="Create one here. The detached job keeps going if you close this tab.">
      <Button variant="solid" icon-left="lucide-plus" label="Create instance" @click="openJob('init')" />
    </EmptyState>
    <EmptyState v-else-if="!rows.length" icon="lucide-boxes" title="No instance matches" description="Clear the search or pick another machine." compact />
    <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      <router-link v-for="i in rows" :key="i.key" :to="instancePath(i)" class="group flex items-center gap-3 rounded-6 border border-outline-gray-1 bg-surface-elevation-1 p-3 transition hover:border-outline-gray-3 hover:shadow-sm" :class="stale(i) && 'opacity-70'">
        <span class="flex size-10 shrink-0 items-center justify-center rounded-lg border border-outline-gray-1 bg-surface-gray-1 text-ink-gray-6"><span class="lucide-boxes size-5" aria-hidden="true" /></span>
        <span class="min-w-0 flex-1">
          <span class="flex items-center gap-2">
            <span class="truncate text-base-medium text-ink-gray-9">{{ i.project }}</span>
            <Badge v-if="updatesFor(i.project, null, i.host).length" size="sm" :theme="updatesFor(i.project, null, i.host).some((u) => u.core) ? 'orange' : 'blue'" variant="subtle" :label="`${updatesFor(i.project, null, i.host).length} ${updatesFor(i.project, null, i.host).length === 1 ? 'update' : 'updates'}`" />
            <Tooltip v-if="stale(i)" :text="linkAdvice(hostList.find((h) => h.id === i.host))">
              <span class="flex items-center gap-1 text-xs text-ink-gray-5"><span class="size-1.5 rounded-full" :class="DOT.unknown" />unknown · {{ lastSeen(i) }}</span>
            </Tooltip>
            <Tooltip v-else-if="QUIET[i.overall] !== null" :text="`container ${i.docker_status}`">
              <span class="flex items-center gap-1 text-xs text-ink-gray-5"><span class="size-1.5 rounded-full" :class="DOT[i.overall] || DOT.unknown" />{{ QUIET[i.overall] || i.overall }}</span>
            </Tooltip>
          </span>
          <span class="mt-0.5 flex items-center gap-1.5 text-sm text-ink-gray-5"><RemoteHostBadge :host="i.host" /><span class="truncate">{{ meta(i) }}</span></span>
        </span>
        <Dropdown :options="menu(i)" align="end">
          <Button size="sm" variant="ghost" icon="lucide-ellipsis" aria-label="More actions" @click.stop.prevent />
        </Dropdown>
      </router-link>
    </div>

    <FleetPerformance class="mt-6" />
    <!-- Captain note 50: remembered-but-gone instances as a quiet disclosure, not a wall of pills. -->
    <section v-if="remembered.length" class="rounded-6 border border-outline-gray-1 bg-surface-elevation-1">
      <button type="button" class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm" :aria-expanded="showRemembered" @click="showRemembered = !showRemembered">
        <span class="size-3.5 text-ink-gray-4" :class="showRemembered ? 'lucide-chevron-down' : 'lucide-chevron-right'" aria-hidden="true" />
        <span class="text-ink-gray-7">{{ remembered.length }} {{ remembered.length === 1 ? 'instance' : 'instances' }} remembered in the cache {{ remembered.length === 1 ? 'is' : 'are' }} not on Docker</span>
        <span class="ml-auto text-xs text-ink-gray-4">{{ showRemembered ? 'hide' : 'show' }}</span>
      </button>
      <div v-if="showRemembered" class="border-t border-outline-gray-1">
        <div class="grid grid-cols-[minmax(0,1fr)_10rem_5rem] items-center gap-3 px-3 py-1.5 text-xs text-ink-gray-5"><span>Name</span><span>Cached</span><span /></div>
        <div v-for="p in remembered" :key="p.name" class="grid grid-cols-[minmax(0,1fr)_10rem_5rem] items-center gap-3 border-t border-outline-gray-1 px-3 py-1.5 text-sm">
          <span class="truncate font-mono text-xs text-ink-gray-7">{{ p.name }}</span>
          <span class="text-xs tabular-nums text-ink-gray-5">{{ p.last_updated.slice(0, 16).replace('T', ' ') }}</span>
          <span class="flex justify-end"><Button size="sm" variant="ghost" label="Forget" :loading="settings.busy === 'cache.clear' && forgetting === p.name" @click="forget(p.name)" /></span>
        </div>
        <p class="px-3 py-2 text-xs text-ink-gray-4">Their cached benches and sites still answer searches and `where`; Forget drops one from the cache. Removing an instance with cwcli forgets it by itself.</p>
      </div>
    </section>
  </div>
  <Dialog :open="!!removing" :title="`Remove ${removing?.name || ''} from this hub?`" icon="lucide-server-off" @update:open="(v) => { if (!v) removing = null }">
    <p class="text-p-base text-ink-gray-7">This hub forgets the host and drops its connection; nothing changes on {{ removing?.name }}. Its owner withdraws this hub's key there with <code class="font-mono text-sm">cwcli remote revoke</code>.</p>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" theme="red" data-primary label="Remove host" :loading="removeBusy" @click="confirmRemove" />
      </div>
    </template>
  </Dialog>
</template>
