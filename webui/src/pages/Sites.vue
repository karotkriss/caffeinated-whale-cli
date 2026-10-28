<script setup>
// Every site across every instance and bench, polished to the Apps standard
// (captain note 04:10Z): the site's own favicon beside its name, instance and
// bench as a quiet second line, a calm status word, the default badge, aligned
// app logos, and an Open action per row.
import { computed, onMounted, ref, watch } from 'vue'
import { Badge, Button, Select, TabButtons, TextInput, Tooltip } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import AppIcon from '../components/AppIcon.vue'
import SiteIcon from '../components/SiteIcon.vue'
import StatusDot from '../components/StatusDot.vue'
import DevModeBadge from '../components/DevModeBadge.vue'
import { instances, details, loadDetail, benchName, parseApp, setFocus } from '../data/fleet'
import { catalogApp } from '../data/catalog'
import { openApp } from '../data/app-panel'
import { openAction } from '../data/dialog'
import { loadSiteIcons, refreshSiteIcons, siteIcons } from '../data/site-icons'
import { openNewSite } from '../data/new-site-dialog'
import { can } from '../data/session'

onMounted(() => {
  setFocus(null)
  instances.value.forEach((i) => loadDetail(i.project))
  loadSiteIcons()
})
const query = ref('')
const project = ref('all')
const state = ref('all')
// A site's status is its BENCH's (one instance holds benches that serve and benches
// that do not), read from the fleet's per-bench health; the instance's word would
// call every site of a half-started instance 'degraded'.
const QUIET = { running: 'serving', degraded: 'degraded', online: 'started, not serving', offline: 'stopped', unknown: 'probing' }
const rows = computed(() => {
  const out = []
  for (const i of instances.value) {
    for (const b of details[i.project]?.data?.benches || []) {
      for (const s of b.sites || []) {
        const health = i.container_running ? i.benches?.find((h) => String(h.index) === String(b.index)) : null
        const token = !i.container_running ? 'offline' : health ? health.overall : 'unknown'
        out.push({
          key: `${i.project}/${b.index}/${s.name}`,
          project: i.project,
          bench: b,
          site: s,
          apps: s.installed_apps.map(parseApp).filter((a) => a.name !== 'frappe'),
          running: !!i.container_running,
          serving: token === 'running' || token === 'degraded',
          token,
          word: QUIET[token] || token,
          isDefault: s.name === b.default_site,
          devMode: details[i.project]?.data?.dev_modes?.[`${b.path}/${s.name}`] || null,
        })
      }
    }
  }
  return out.sort((a, b) => a.site.name.localeCompare(b.site.name))
})
// Ask for the icons once the rows are known (and again when an instance starts).
const iconRequest = computed(() => rows.value.map((r) => ({ project: r.project, bench_path: r.bench.path, site: r.site.name, running: r.running })))
watch(() => JSON.stringify(iconRequest.value.map((s) => [s.project, s.site, s.running])), () => refreshSiteIcons(iconRequest.value), { immediate: false })
watch(() => rows.value.length, (n, was) => { if (n && !was) refreshSiteIcons(iconRequest.value) })

const shown = computed(() => {
  const q = query.value.trim().toLowerCase()
  return rows.value.filter(
    (r) =>
      (project.value === 'all' || r.project === project.value) &&
      (state.value === 'all' || (state.value === 'serving' ? r.serving : !r.serving)) &&
      (!q || r.site.name.toLowerCase().includes(q) || r.project.toLowerCase().includes(q) || benchName(r.bench).toLowerCase().includes(q) || r.apps.some((a) => a.name.includes(q))),
  )
})
const projectOptions = computed(() => [{ label: 'Any instance', value: 'all' }, ...instances.value.map((i) => ({ label: i.project, value: i.project }))])
const stateOptions = [{ label: 'All', value: 'all' }, { label: 'Serving', value: 'serving' }, { label: 'Not serving', value: 'stopped' }]
const loading = computed(() => instances.value.some((i) => details[i.project]?.loading && !details[i.project]?.data))
const filtered = computed(() => query.value.trim() || project.value !== 'all' || state.value !== 'all')
const withIcon = computed(() => rows.value.filter((r) => siteIcons.byKey[`${r.project}/${r.site.name}`]?.logo).length)
const ctx = (r) => ({ project: r.project, bench: String(r.bench.index), benchName: benchName(r.bench), site: r.site.name })
</script>

<template>
  <PageHead title="Sites" :count="rows.length || null">
    <template #actions><Button v-if="can('operator')" variant="solid" icon-left="lucide-plus" label="New site" @click="openNewSite({ project: project !== 'all' ? project : null })" /></template>
  </PageHead>
  <div class="space-y-4 px-3 pb-20 pt-5 sm:px-5">
    <div class="flex flex-wrap items-center gap-2">
      <TextInput v-model="query" class="w-72 max-w-full" type="search" placeholder="Search sites, instances, benches, apps">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <Select v-model="project" :options="projectOptions" />
      <TabButtons v-model="state" :options="stateOptions" />
      <span class="ml-auto flex items-center gap-2 text-sm tabular-nums text-ink-gray-5">
        <Tooltip text="A favicon is read from the site itself, so only a serving site has one; the others show a globe until their bench serves"><span v-if="rows.length" class="text-xs">{{ withIcon }} of {{ rows.length }} favicons{{ siteIcons.refreshing ? ', reading' : '' }}</span></Tooltip>
        <Button size="xs" variant="ghost" icon-left="lucide-refresh-cw" label="Re-read favicons" :disabled="siteIcons.refreshing || !rows.length" @click="refreshSiteIcons(iconRequest, { force: true })" />
        <span>{{ shown.length }} of {{ rows.length }}</span>
      </span>
    </div>

    <div v-if="loading && !rows.length" class="space-y-2"><div v-for="i in 5" :key="i" class="h-14 animate-pulse rounded-6 bg-surface-gray-1" /></div>
    <EmptyState
      v-else-if="!shown.length"
      icon="lucide-globe"
      :title="filtered ? 'No site matches these filters' : 'No sites in the cache yet'"
      :description="filtered ? 'Clear a filter to see every site.' : 'Sites appear once an instance has been inspected.'"
      :command="filtered ? '' : 'cwcli inspect <instance>'"
    />
    <List v-else class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '2.5rem'], lg: ['minmax(14rem,1.5fr)', '9rem', 'minmax(10rem,1.4fr)', '7rem', '2.5rem'] }" :row-height="56">
      <ListHeader>
        <ListHeaderCell>Site</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Status</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Apps</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Apps checked</ListHeaderCell>
        <ListHeaderCell><span class="sr-only">Open</span></ListHeaderCell>
      </ListHeader>
      <ListRows :items="shown" row-key="key" v-slot="{ item }">
        <ListRow :route="`/instances/${item.project}/benches/${item.bench.index}/sites/${item.site.name}`">
          <ListCell class="gap-3 overflow-hidden">
            <SiteIcon :project="item.project" :site="item.site.name" size="md" />
            <span class="min-w-0">
              <span class="flex items-center gap-1.5">
                <span class="truncate text-base-medium text-ink-gray-9">{{ item.site.name }}</span>
                <Badge v-if="item.isDefault" size="sm" theme="gray" variant="subtle" label="default" />
                <DevModeBadge :state="item.devMode" quiet-off />
              </span>
              <span class="flex items-center gap-1.5 text-xs text-ink-gray-5"><span class="truncate">{{ item.project }} · {{ benchName(item.bench) }}</span><span class="flex shrink-0 items-center gap-1 lg:hidden"><StatusDot :token="item.token" />{{ item.word }}</span></span>
            </span>
          </ListCell>
          <ListCell class="gap-1.5 max-lg:hidden"><StatusDot :token="item.token" /><span class="truncate text-sm text-ink-gray-6">{{ item.word }}</span></ListCell>
          <ListCell class="gap-1.5 overflow-hidden max-lg:hidden" @click.stop.prevent>
            <span v-if="!item.apps.length" class="text-sm text-ink-gray-4">frappe only</span>
            <Tooltip v-for="a in item.apps.slice(0, 6)" :key="a.name" :text="`${catalogApp(a.name).title}${a.version ? ' · ' + a.version : ''}${a.branch ? ' · ' + a.branch : ''}`">
              <button class="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4" @click.stop.prevent="openApp(catalogApp(a.name))"><AppIcon :app="catalogApp(a.name)" size="sm" /></button>
            </Tooltip>
            <span v-if="item.apps.length > 6" class="text-xs tabular-nums text-ink-gray-5">+{{ item.apps.length - 6 }}</span>
          </ListCell>
          <ListCell class="max-lg:hidden">
            <Tooltip :text="item.site.installed_apps_verified ? 'Read live by a full inspect' : 'Remembered from the cache; a full inspect verifies them'">
              <span class="text-sm" :class="item.site.installed_apps_verified ? 'text-ink-green-6' : 'text-ink-gray-5'">{{ item.site.installed_apps_verified ? 'verified' : 'remembered' }}</span>
            </Tooltip>
          </ListCell>
          <ListCell class="justify-end" @click.stop.prevent>
            <Tooltip :text="item.serving ? `Open ${item.site.name} in a new tab` : item.running ? `Bench ${benchName(item.bench)} is not serving; start it first` : `${item.project} is stopped; cwcli asks to start it first`">
              <Button size="sm" variant="ghost" icon="lucide-external-link" :aria-label="`Open ${item.site.name}`" @click.stop.prevent="openAction('site.open', ctx(item))" />
            </Tooltip>
          </ListCell>
        </ListRow>
      </ListRows>
    </List>
  </div>
</template>
