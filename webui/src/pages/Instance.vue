<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Button, Dropdown, Alert, TextInput, Tabs, TabList, TabTrigger, TabPanel, LoadingText, Badge } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import StatusBadge from '../components/StatusBadge.vue'
import PageHero from '../components/PageHero.vue'
import PerformancePanel from '../components/PerformancePanel.vue'
import DevModeBadge from '../components/DevModeBadge.vue'
import UpdatePill from '../components/UpdatePill.vue'
import RemoteHostBadge from '../components/RemoteHostBadge.vue'
import { updatesFor, updatesForApp } from '../data/updates'
import { openJob } from '../data/job-dialog'
import { openNewBench } from '../data/new-bench-dialog'
import AppIcon from '../components/AppIcon.vue'
import StatusDot from '../components/StatusDot.vue'
import { catalogApp } from '../data/catalog'
import { appOpener } from '../data/app-panel'
import CommandLine from '../components/CommandLine.vue'
import { fleet, instance as findInstance, loadDetail, benchName, publishedRange, countSites, parseApp, setFocus, ago, instancePath, benchPath, sitePath } from '../data/fleet'
import { openAction, menuOption, dangerGroup } from '../data/dialog'
import { useTabQuery } from '../data/tabs'
import { can } from '../data/session'
import { host as hostOf, hostName, linkAdvice } from '../data/hosts'

const route = useRoute()
const project = route.params.project
// A remote host's instance: the same page, every read and action through the hub.
const host = route.params.host || null
const where = { project, host }
onMounted(() => setFocus(project, host))
const inst = computed(() => findInstance(project, host))
const detail = loadDetail(project, { host })
const tab = useTabQuery('benches')
const filter = ref('')
const link = computed(() => hostOf(host))

const health = computed(() => Object.fromEntries((inst.value?.benches || []).map((b) => [b.index, b])))
const benches = computed(() => {
  const q = filter.value.trim().toLowerCase()
  return (detail.data?.benches || []).filter(
    (b) => !q || benchName(b).toLowerCase().includes(q) || b.path.toLowerCase().includes(q) || b.sites.some((s) => s.name.includes(q)),
  )
})
const allSites = computed(() => (detail.data?.benches || []).flatMap((b) => b.sites.map((s) => ({ ...s, bench: b }))))
const apps = computed(() => {
  const out = {}
  for (const b of detail.data?.benches || []) for (const name of b.available_apps) if (/^[a-z][a-z0-9_]*$/i.test(name)) (out[name] ||= { name, benches: 0, branches: new Set() }).benches++
  for (const s of allSites.value) for (const a of s.installed_apps.map(parseApp)) a.branch && out[a.name]?.branches.add(a.branch)
  return Object.values(out).sort((a, b) => b.benches - a.benches || a.name.localeCompare(b.name))
})
const ports = computed(() => publishedRange(inst.value?.ports))
const running = computed(() => inst.value?.container_running)
// Per-app menu on the Apps tab (captain note 29): open its details, then per bench
// that holds it, Update (the apps update job) and Check out a ref.
function appMenu(name) {
  const benches = (detail.data?.benches || []).filter((b) => (b.available_apps || []).includes(name) || (b.sites || []).some((s) => s.installed_apps.some((e) => e.split(/\s+/)[0] === name)))
  // A remote instance's app has no details here: the panel is this machine's catalog.
  const open = appOpener(host, name)
  const groups = open ? [{ group: 'App', options: [{ label: 'Open details', icon: 'lucide-panel-right-open', onClick: open }] }] : []
  for (const b of benches) {
    const c = { project, host, bench: String(b.index), benchName: benchName(b), app: name }
    groups.push({ group: benchName(b), options: [
      { label: `Update ${name}`, icon: 'lucide-package-check', onClick: () => openJob('apps-update', { ...c, args: { apps: name, sites: '' } }) },
      menuOption('app.checkout', c, inst.value?.benches?.find((h) => String(h.index) === String(b.index))),
    ] })
  }
  return groups
}
// No container on this daemon: nothing to start, stop or inspect here.
const gone = computed(() => !inst.value && fleet.connection === 'live')
const dataLabel = computed(() => {
  if (detail.loading && !detail.data) return 'reading'
  if (!detail.data) return gone.value ? 'not read' : 'not cached'
  if (detail.data.served_from === 'full') return 'read live'
  const at = detail.data.cached_at ? Date.parse(detail.data.cached_at.replace(' ', 'T')) : null
  return at ? `from cache, ${ago(at)}` : 'from cache'
})
const ctx = { project, host }
const more = computed(() => [
  menuOption('instance.restart', ctx),
  menuOption('instance.refresh', ctx),
  menuOption('instance.scale', ctx),
  dangerGroup(['instance.remove'], ctx),
])
const heroFacts = computed(() => [
  ...(host ? [{ label: 'Machine', value: hostName(host) }] : []),
  { label: 'Container', value: inst.value?.docker_status || (gone.value ? 'not on Docker' : 'unknown') },
  { label: 'Benches', value: detail.data ? String(detail.data.benches.length) : detail.loading ? 'reading' : 'not cached' },
  { label: 'Sites', value: detail.data ? String(countSites(detail.data)) : detail.loading ? 'reading' : 'not cached' },
  { label: 'Published web ports', value: ports.value.web ? `${ports.value.web} (${ports.value.count})` : 'none', mono: !!ports.value.web },
  { label: 'Data', value: dataLabel.value, tone: 'muted' },
])
const machine = computed(() => (host ? hostName(host) : 'this Docker daemon'))
</script>

<template>
  <PageHero icon="lucide-boxes" :title="project" :facts="heroFacts" :crumbs="[{ label: 'Instances', route: '/instances' }, ...(host ? [{ label: hostName(host), route: '/instances' }] : []), { label: project, route: instancePath(where) }]">
    <template #badge><StatusBadge :token="inst?.overall" size="lg" /><RemoteHostBadge :host="host" size="md" /></template>
    <template #actions>
      <template v-if="!gone">
        <template v-if="can('operator')">
          <Button variant="subtle" icon-left="lucide-plus" label="New bench" @click="openNewBench({ project, host })" />
          <Button v-if="!running" variant="solid" icon-left="lucide-play" label="Start" @click="openAction('instance.start', ctx)" />
          <Button v-else icon-left="lucide-square" label="Stop" @click="openAction('instance.stop', ctx)" />
        </template>
        <Dropdown :options="more" align="end"><Button icon="lucide-ellipsis" aria-label="More actions" /></Dropdown>
      </template>
    </template>
  </PageHero>

  <div class="space-y-5 px-3 pb-20 pt-5 sm:px-5">
    <Alert v-if="host && link && !link.actions_allowed" :theme="link.readable ? 'gray' : 'amber'" :title="`${hostName(host)} is ${link.state.replaceAll('_', ' ')}`" :description="linkAdvice(link)" />
    <Alert v-if="!inst && fleet.connection === 'live'" theme="gray" :title="`${project} is not on ${machine}`" description="It may have been removed or live on another Docker daemon. Search (Ctrl+K) finds the benches and sites cwcli remembers for it." />
    <Alert v-else-if="inst && !running" theme="gray" :title="`${project} is stopped`" description="Benches, sites and apps below are remembered from the cwcli cache. Process health and logs need the container running." />

    <LoadingText v-if="detail.loading && !detail.data" text="Reading the cache..." />
    <div v-else-if="detail.error && gone" class="rounded-6 border border-outline-gray-1 px-4 py-10 text-center">
      <p class="text-base text-ink-gray-7">Docker has no container for {{ project }} on {{ machine }}, so there is nothing to read live.</p>
      <p class="mt-1 text-sm text-ink-gray-5">{{ detail.error.message }}</p>
    </div>
    <div v-else-if="detail.error" class="rounded-6 border border-outline-gray-1 px-4 py-10 text-center">
      <p class="text-base text-ink-gray-7">cwcli has not inspected {{ project }}{{ running ? '' : ', and it is stopped' }}.</p>
      <p class="mt-1 text-sm text-ink-gray-5">{{ detail.error.message }}</p>
      <div class="mx-auto mt-4 max-w-sm"><CommandLine :command="running ? `cwcli inspect ${project}` : `cwcli start ${project}`" /></div>
    </div>

    <Tabs v-else-if="detail.data" v-model="tab">
      <TabList>
        <TabTrigger value="benches" label="Benches"><template #suffix><span class="text-xs text-ink-gray-5">{{ detail.data.benches.length }}</span></template></TabTrigger>
        <TabTrigger value="sites" label="Sites"><template #suffix><span class="text-xs text-ink-gray-5">{{ allSites.length }}</span></template></TabTrigger>
        <TabTrigger value="apps" label="Apps"><template #suffix><span class="text-xs text-ink-gray-5">{{ apps.length }}</span></template></TabTrigger>
        <TabTrigger value="performance" label="Performance" />
      </TabList>

      <TabPanel value="benches" class="pt-4">
        <div class="mb-3 flex items-center gap-2">
          <Button v-if="can('operator')" variant="subtle" size="sm" icon-left="lucide-plus" label="New bench" @click="openNewBench({ project, host })" />
          <TextInput v-model="filter" class="w-72 max-w-full" placeholder="Filter benches or sites" type="search">
            <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
          </TextInput>
          <span class="text-sm text-ink-gray-5">{{ benches.length }} of {{ detail.data.benches.length }}</span>
        </div>
        <List class="-mx-3 list-row-px-3" :columns="{ base: ['2.5rem', 'minmax(0,1fr)', '6.5rem'], lg: ['2.5rem', 'minmax(12rem,1.4fr)', '6.5rem', '4.5rem', '4.5rem', '5rem'] }" :row-height="52">
          <ListHeader>
            <ListHeaderCell>#</ListHeaderCell>
            <ListHeaderCell>Bench</ListHeaderCell>
            <ListHeaderCell>Health</ListHeaderCell>
            <ListHeaderCell class="max-lg:hidden">Sites</ListHeaderCell>
            <ListHeaderCell class="max-lg:hidden">Apps</ListHeaderCell>
            <ListHeaderCell class="max-lg:hidden">Web port</ListHeaderCell>
          </ListHeader>
          <ListRows :items="benches" :row-key="(b) => b.index" v-slot="{ item }">
            <ListRow :route="benchPath(where, item.index)">
              <ListCell><span class="tabular-nums text-sm text-ink-gray-4">{{ item.index }}</span></ListCell>
              <ListCell class="flex-col !items-start justify-center gap-1 overflow-hidden">
                <span class="flex max-w-full items-center gap-1.5">
                  <span v-if="item.label" class="flex min-w-0 items-center gap-1.5 truncate text-base-medium text-ink-gray-9">
                    <span class="lucide-tag size-3 shrink-0 text-ink-gray-5" aria-hidden="true" />{{ item.label }}</span>
                  <span v-else class="truncate text-base text-ink-gray-7">{{ benchName(item) }} <span class="text-xs text-ink-gray-4">no label</span></span>
                  <Badge v-if="updatesFor(project, item.index, host).length" size="sm" :theme="updatesFor(project, item.index, host).some((u) => u.core) ? 'orange' : 'blue'" variant="subtle" class="shrink-0" :label="`${updatesFor(project, item.index, host).length} ${updatesFor(project, item.index, host).length === 1 ? 'update' : 'updates'}`" />
                </span>
                <span class="truncate font-mono text-xs text-ink-gray-5">{{ item.path }}</span>
              </ListCell>
              <ListCell>
                <StatusBadge v-if="running && inst?.probe_error" token="unknown" />
                <span v-else-if="!running || health[item.index]?.overall === 'running'" class="flex items-center gap-1.5 text-sm text-ink-gray-6"><StatusDot :token="running ? 'running' : 'offline'" />{{ running ? 'running' : 'stopped' }}</span>
                <StatusBadge v-else :token="health[item.index]?.overall" />
              </ListCell>
              <ListCell class="max-lg:hidden"><span class="tabular-nums text-ink-gray-7">{{ item.sites.length }}</span></ListCell>
              <ListCell class="max-lg:hidden"><span class="tabular-nums text-ink-gray-7">{{ item.available_apps.length }}</span></ListCell>
              <ListCell class="max-lg:hidden"><span class="font-mono text-sm text-ink-gray-6">{{ health[item.index]?.web_port ?? 'unknown' }}</span></ListCell>
            </ListRow>
          </ListRows>
        </List>
      </TabPanel>

      <TabPanel value="sites" class="pt-4">
        <List class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '4rem'], md: ['minmax(12rem,1.2fr)', 'minmax(0,1fr)', '6rem'] }" :row-height="44">
          <ListHeader>
            <ListHeaderCell>Site</ListHeaderCell>
            <ListHeaderCell class="max-md:hidden">Bench</ListHeaderCell>
            <ListHeaderCell>Apps</ListHeaderCell>
          </ListHeader>
          <ListRows :items="allSites" :row-key="(s) => s.bench.index + s.name" v-slot="{ item }">
            <ListRow :route="sitePath(where, item.bench.index, item.name)">
              <ListCell class="gap-2"><span class="truncate font-mono text-sm text-ink-gray-8">{{ item.name }}</span><DevModeBadge :state="detail.data?.dev_modes?.[`${item.bench.path}/${item.name}`]" quiet-off /></ListCell>
              <ListCell class="max-md:hidden"><span class="truncate text-sm text-ink-gray-6">{{ benchName(item.bench) }}</span></ListCell>
              <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.installed_apps.length }}</span></ListCell>
            </ListRow>
          </ListRows>
        </List>
      </TabPanel>

      <TabPanel value="apps" class="pt-4">
        <List class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '5rem', '2.5rem'], md: ['minmax(10rem,1fr)', '6rem', 'minmax(0,2fr)', '2.5rem'] }" :row-height="44">
          <ListHeader>
            <ListHeaderCell>App</ListHeaderCell>
            <ListHeaderCell>Benches</ListHeaderCell>
            <ListHeaderCell class="max-md:hidden">Branches at install</ListHeaderCell>
            <ListHeaderCell />
          </ListHeader>
          <ListRows :items="apps" row-key="name" v-slot="{ item }">
            <ListRow :on-click="appOpener(host, item.name)">
              <!-- A remote host's row opens no panel: the app panel is this machine's catalog. -->
              <ListCell class="gap-2"><AppIcon :app="catalogApp(item.name)" size="sm" /><span class="text-base text-ink-gray-8">{{ catalogApp(item.name).title }}</span><span class="font-mono text-xs text-ink-gray-4">{{ item.name }}</span><UpdatePill :item="host ? null : updatesForApp(item.name).find((u) => u.project === project) || null" /></ListCell>
              <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.benches }}</span></ListCell>
              <ListCell class="gap-1 overflow-hidden max-md:hidden">
                <!-- Captain note 51: a branch is a fact, not a status - plain mono text, no pill. -->
                <span v-if="!item.branches.size" class="text-sm text-ink-gray-4">not installed on a site</span>
                <span v-else class="truncate font-mono text-xs text-ink-gray-6"><span class="lucide-git-branch mr-1 inline-block size-3 align-[-2px] text-ink-gray-4" aria-hidden="true" />{{ [...item.branches].slice(0, 4).join(' · ') }}<span v-if="item.branches.size > 4" class="text-ink-gray-4"> +{{ item.branches.size - 4 }}</span></span>
              </ListCell>
              <ListCell class="justify-end" @click.stop.prevent>
                <Dropdown :options="appMenu(item.name)" align="end">
                  <Button size="xs" variant="ghost" icon="lucide-ellipsis" :aria-label="`Actions for ${item.name}`" />
                </Dropdown>
              </ListCell>
            </ListRow>
          </ListRows>
        </List>
        <p class="mt-3 text-p-sm text-ink-gray-5">Branches are what bench recorded at install time. git is the authority on what is checked out now.</p>
      </TabPanel>
      <TabPanel value="performance" class="pt-4">
        <PerformancePanel :project="project" :host="host" />
      </TabPanel>
    </Tabs>
  </div>
</template>
