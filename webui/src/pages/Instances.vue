<script setup>
// Direction A: Pilot's Sites list. Compact cards in a two-column grid, a meta
// line instead of columns, status quiet when healthy (a dot and a word only
// when an instance is not running), the toolbar only once the list is long,
// everything in a centred reading column.
import { computed, onMounted, ref } from 'vue'
import { Button, Dropdown, Badge, TextInput, Tooltip } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import FleetPerformance from '../components/FleetPerformance.vue'
import { loadUpdates, updatesFor } from '../data/updates'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { fleet, instances, details, publishedRange, countSites, cacheProjects, setFocus } from '../data/fleet'
import { applySetting, settings } from '../data/settings'
import { openAction, menuOption } from '../data/dialog'
import { openJob } from '../data/job-dialog'

onMounted(() => {
  loadUpdates()
  setFocus(null)
  cacheProjects.load()
})
const query = ref('')
const live = computed(() => new Set(instances.value.map((i) => i.project)))
const remembered = computed(() => (cacheProjects.data || []).filter((p) => !live.value.has(p.name)).sort((a, b) => String(b.last_updated).localeCompare(String(a.last_updated))))
const showRemembered = ref(false)
const forgetting = ref('')
async function forget(name) {
  forgetting.value = name
  try { await applySetting('cache.clear', { project: name }, { done: `Forgot ${name}`, refresh: false }); await cacheProjects.load(true) } finally { forgetting.value = '' }
}
const rows = computed(() => {
  const q = query.value.trim().toLowerCase()
  return instances.value.filter((i) => !q || i.project.toLowerCase().includes(q))
})
const meta = (i) => {
  const d = details[i.project]?.data
  const parts = []
  if (d) parts.push(`${d.benches.length} ${d.benches.length === 1 ? 'bench' : 'benches'}`, `${countSites(d)} ${countSites(d) === 1 ? 'site' : 'sites'}`)
  else parts.push(details[i.project]?.loading ? 'reading' : 'not cached')
  const web = publishedRange(i.ports).web
  if (web) parts.push(`ports ${web}`)
  return parts.join(' · ')
}
const QUIET = { running: null, online: 'started, not serving', degraded: 'degraded', offline: 'stopped', unknown: 'probing' }
const DOT = { running: 'bg-surface-green-7', online: 'bg-surface-blue-7', degraded: 'bg-surface-amber-7', offline: 'bg-surface-gray-5', unknown: 'border border-outline-gray-4' }
const menu = (i) => [
  i.container_running
    ? { label: 'Stop', icon: 'lucide-square', onClick: () => openAction('instance.stop', { project: i.project }) }
    : { label: 'Start', icon: 'lucide-play', onClick: () => openAction('instance.start', { project: i.project }) },
  menuOption('instance.restart', { project: i.project }),
  menuOption('instance.refresh', { project: i.project }),
  menuOption('instance.scale', { project: i.project }),
]
</script>

<template>
  <PageHead title="Instances" :count="instances.length || null">
    <template #actions>
      <Button variant="solid" icon-left="lucide-plus" label="New instance" @click="openJob('init')" />
      <!-- Captain note 41: the updates check lives here, as a job; the card badge says the rest. -->
      <Dropdown :options="[{ label: 'Check for updates', icon: 'refresh-cw', onClick: async () => trackJob((await createJob({ verb: 'check-updates', target: {}, args: {} })).id) }]" align="end">
        <Button variant="ghost" icon="lucide-ellipsis" aria-label="More" />
      </Dropdown>
    </template>
  </PageHead>
  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <div v-if="instances.length > 10" class="flex items-center gap-2">
      <TextInput v-model="query" class="w-64" type="search" placeholder="Search instances">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <span class="text-sm text-ink-gray-5">{{ rows.length }} of {{ instances.length }}</span>
    </div>
    <div v-if="fleet.connection === 'connecting' && !instances.length" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      <div v-for="i in 4" :key="i" class="h-20 animate-pulse rounded-6 border border-outline-gray-1 bg-surface-gray-1" />
    </div>
    <EmptyState v-else-if="!instances.length" icon="lucide-boxes" title="No instances on this Docker daemon" description="Create one here. The detached job keeps going if you close this tab.">
      <Button variant="solid" icon-left="lucide-plus" label="Create instance" @click="openJob('init')" />
    </EmptyState>
    <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      <router-link v-for="i in rows" :key="i.project" :to="`/instances/${i.project}`" class="group flex items-center gap-3 rounded-6 border border-outline-gray-1 bg-surface-elevation-1 p-3 transition hover:border-outline-gray-3 hover:shadow-sm">
        <span class="flex size-10 shrink-0 items-center justify-center rounded-lg border border-outline-gray-1 bg-surface-gray-1 text-ink-gray-6"><span class="lucide-boxes size-5" aria-hidden="true" /></span>
        <span class="min-w-0 flex-1">
          <span class="flex items-center gap-2">
            <span class="truncate text-base-medium text-ink-gray-9">{{ i.project }}</span>
            <Badge v-if="updatesFor(i.project).length" size="sm" :theme="updatesFor(i.project).some((u) => u.core) ? 'orange' : 'blue'" variant="subtle" :label="`${updatesFor(i.project).length} ${updatesFor(i.project).length === 1 ? 'update' : 'updates'}`" />
            <Tooltip v-if="QUIET[i.overall] !== null" :text="`container ${i.docker_status}`">
              <span class="flex items-center gap-1 text-xs text-ink-gray-5"><span class="size-1.5 rounded-full" :class="DOT[i.overall] || DOT.unknown" />{{ QUIET[i.overall] || i.overall }}</span>
            </Tooltip>
          </span>
          <span class="mt-0.5 block truncate text-sm text-ink-gray-5">{{ meta(i) }}</span>
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
</template>
