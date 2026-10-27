<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { PageHeader, Breadcrumbs, Button, Dropdown, Alert, TextInput, Tabs, TabList, TabTrigger, TabPanel, LoadingText, Badge } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import StatusBadge from '../components/StatusBadge.vue'
import CommandLine from '../components/CommandLine.vue'
import { fleet, instance as findInstance, loadDetail, benchName, publishedRange, countSites, parseApp, setFocus, ago } from '../data/fleet'
import { openAction, menuOption, dangerGroup } from '../data/dialog'

const route = useRoute()
const project = route.params.project
onMounted(() => setFocus(project))
const inst = computed(() => findInstance(project))
const detail = loadDetail(project)
const tab = ref(route.query.tab || 'benches')
const filter = ref('')

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
  for (const b of detail.data?.benches || []) for (const name of b.available_apps) (out[name] ||= { name, benches: 0, branches: new Set() }).benches++
  for (const s of allSites.value) for (const a of s.installed_apps.map(parseApp)) a.branch && out[a.name]?.branches.add(a.branch)
  return Object.values(out).sort((a, b) => b.benches - a.benches || a.name.localeCompare(b.name))
})
const ports = computed(() => publishedRange(inst.value?.ports))
const running = computed(() => inst.value?.container_running)
// No container on this daemon: nothing to start, stop or inspect here.
const gone = computed(() => !inst.value && fleet.connection === 'live')
const dataLabel = computed(() => {
  if (detail.loading && !detail.data) return 'reading'
  if (!detail.data) return gone.value ? 'not read' : 'not cached'
  if (detail.data.served_from === 'full') return 'read live'
  const at = detail.data.cached_at ? Date.parse(detail.data.cached_at.replace(' ', 'T')) : null
  return at ? `from cache, ${ago(at)}` : 'from cache'
})
const ctx = { project }
const more = computed(() => [
  menuOption('instance.restart', ctx),
  menuOption('instance.refresh', ctx),
  menuOption('instance.scale', ctx),
  dangerGroup(['instance.remove'], ctx),
])
</script>

<template>
  <PageHeader>
    <Breadcrumbs :items="[{ label: 'Instances', route: '/instances' }, { label: project, route: `/instances/${project}` }]" />
    <div class="flex items-center gap-2">
      <StatusBadge class="max-md:hidden" :token="inst?.overall" size="md" />
      <template v-if="!gone">
      <Button v-if="!running" variant="solid" icon-left="lucide-play" label="Start" @click="openAction('instance.start', ctx)" />
      <Button v-else icon-left="lucide-square" label="Stop" @click="openAction('instance.stop', ctx)" />
      <Dropdown :options="more" align="end">
        <Button icon="lucide-ellipsis" aria-label="More actions" />
      </Dropdown>
      </template>
    </div>
  </PageHeader>

  <div class="space-y-5 px-3 pb-20 pt-5 sm:px-5">
    <Alert v-if="!inst && fleet.connection === 'live'" theme="gray" :title="`${project} is not on this Docker daemon`" description="It may have been removed or live on another Docker daemon. Search (Ctrl+K) finds the benches and sites cwcli remembers for it." />
    <Alert v-else-if="inst && !running" theme="gray" :title="`${project} is stopped`" description="Benches, sites and apps below are remembered from the cwcli cache. Process health and logs need the container running." />

    <div class="grid grid-cols-2 gap-px overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-gray-2 lg:grid-cols-4">
      <div class="bg-surface-base px-4 py-3">
        <div class="text-sm text-ink-gray-5">Container</div>
        <div class="mt-1.5 font-mono text-base text-ink-gray-8">{{ inst?.docker_status || (gone ? 'not on Docker' : 'unknown') }}</div>
      </div>
      <div class="bg-surface-base px-4 py-3">
        <div class="text-sm text-ink-gray-5">Benches / sites</div>
        <div class="mt-1.5 text-base tabular-nums text-ink-gray-8"><template v-if="detail.data">{{ detail.data.benches.length }} / {{ countSites(detail.data) }}</template><template v-else>{{ detail.loading ? 'reading' : gone ? 'not read' : 'not cached' }}</template></div>
      </div>
      <div class="bg-surface-base px-4 py-3">
        <div class="text-sm text-ink-gray-5">Published web ports</div>
        <div class="mt-1.5 font-mono text-base text-ink-gray-8">{{ ports.web || 'none' }} <span class="font-sans text-sm text-ink-gray-5">({{ ports.count }})</span></div>
      </div>
      <div class="bg-surface-base px-4 py-3">
        <div class="text-sm text-ink-gray-5">Data</div>
        <div class="mt-1.5 text-base text-ink-gray-8">{{ dataLabel }}</div>
      </div>
    </div>

    <LoadingText v-if="detail.loading && !detail.data" text="Reading the cache..." />
    <div v-else-if="detail.error && gone" class="rounded-6 border border-outline-gray-1 px-4 py-10 text-center">
      <p class="text-base text-ink-gray-7">Docker has no container for {{ project }}, so there is nothing to read live.</p>
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
      </TabList>

      <TabPanel value="benches" class="pt-4">
        <div class="mb-3 flex items-center gap-2">
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
            <ListRow :route="`/instances/${project}/benches/${item.index}`">
              <ListCell><span class="tabular-nums text-sm text-ink-gray-4">{{ item.index }}</span></ListCell>
              <ListCell class="flex-col !items-start justify-center gap-1 overflow-hidden">
                <span v-if="item.label" class="flex items-center gap-1.5 truncate text-base-medium text-ink-gray-9">
                  <span class="lucide-tag size-3 text-ink-gray-5" aria-hidden="true" />{{ item.label }}</span>
                <span v-else class="truncate text-base text-ink-gray-7">{{ benchName(item) }} <span class="text-xs text-ink-gray-4">no label</span></span>
                <span class="truncate font-mono text-xs text-ink-gray-5">{{ item.path }}</span>
              </ListCell>
              <ListCell><StatusBadge :token="running ? (inst?.probe_error ? 'unknown' : health[item.index]?.overall) : 'offline'" /></ListCell>
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
            <ListRow :route="`/instances/${project}/benches/${item.bench.index}/sites/${item.name}`">
              <ListCell><span class="truncate font-mono text-sm text-ink-gray-8">{{ item.name }}</span></ListCell>
              <ListCell class="max-md:hidden"><span class="truncate text-sm text-ink-gray-6">{{ benchName(item.bench) }}</span></ListCell>
              <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.installed_apps.length }}</span></ListCell>
            </ListRow>
          </ListRows>
        </List>
      </TabPanel>

      <TabPanel value="apps" class="pt-4">
        <List class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '5rem'], md: ['minmax(10rem,1fr)', '6rem', 'minmax(0,2fr)'] }" :row-height="44">
          <ListHeader>
            <ListHeaderCell>App</ListHeaderCell>
            <ListHeaderCell>Benches</ListHeaderCell>
            <ListHeaderCell class="max-md:hidden">Branches at install</ListHeaderCell>
          </ListHeader>
          <ListRows :items="apps" row-key="name" v-slot="{ item }">
            <ListRow>
              <ListCell><span class="font-mono text-sm text-ink-gray-8">{{ item.name }}</span></ListCell>
              <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.benches }}</span></ListCell>
              <ListCell class="gap-1 overflow-hidden max-md:hidden">
                <Badge v-for="b in [...item.branches].slice(0, 4)" :key="b" size="sm" variant="outline" theme="gray" :label="b" />
                <span v-if="item.branches.size > 4" class="text-xs text-ink-gray-5">+{{ item.branches.size - 4 }}</span>
              </ListCell>
            </ListRow>
          </ListRows>
        </List>
        <p class="mt-3 text-p-sm text-ink-gray-5">Branches are what bench recorded at install time. git is the authority on what is checked out now.</p>
      </TabPanel>
    </Tabs>
  </div>
</template>
