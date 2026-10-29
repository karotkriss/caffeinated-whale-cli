<script setup>
// Every site across every instance and bench, polished to the Apps standard
// (captain note 04:10Z): the site's own favicon beside its name, instance and
// bench as a quiet second line, a calm status word, the default badge, aligned
// app logos, and an Open action per row. Sites on a remote host are in the same
// list, their second line naming the machine first.
// With a publishing integration on (or any site published), a Public address column
// shows each published site's address and the state of its DNS record, proxy host
// and certificate, with Publish, Retry, Check for drift and Unpublish in its menu.
// Publications are this machine's own, so a remote host's site has none here.
import { computed, onMounted, ref, watch } from 'vue'
import { Badge, Button, Dropdown, Select, TabButtons, TextInput, Tooltip } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import AppIcon from '../components/AppIcon.vue'
import SiteIcon from '../components/SiteIcon.vue'
import StatusDot from '../components/StatusDot.vue'
import DevModeBadge from '../components/DevModeBadge.vue'
import { instances, detailOf, loadDetail, benchName, parseApp, setFocus, sitePath } from '../data/fleet'
import { catalogApp } from '../data/catalog'
import { openApp } from '../data/app-panel'
import { openAction } from '../data/dialog'
import { loadSiteIcons, refreshSiteIcons, siteIcons, siteIcon } from '../data/site-icons'
import { openNewSite } from '../data/new-site-dialog'
import { can } from '../data/session'
import { canPublish, checkSite, isLive, isPublicName, loadPublications, PART_LABEL, PART_STATE, publicationAt, SHOWN_PARTS } from '../data/publish'
import { hostName, hostReadable } from '../data/hosts'

onMounted(() => {
  setFocus(null)
  instances.value.forEach((i) => loadDetail(i.project, { host: i.host }))
  loadSiteIcons()
  loadPublications()
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
    const stale = !!i.host && !hostReadable(i.host)
    for (const b of detailOf(i.project, i.host)?.data?.benches || []) {
      for (const s of b.sites || []) {
        const health = i.container_running ? i.benches?.find((h) => String(h.index) === String(b.index)) : null
        const token = stale ? 'unknown' : !i.container_running ? 'offline' : health ? health.overall : 'unknown'
        out.push({
          key: `${i.key}/${b.index}/${s.name}`,
          project: i.project,
          host: i.host,
          inst: i,
          bench: b,
          site: s,
          apps: s.installed_apps.map(parseApp).filter((a) => a.name !== 'frappe'),
          running: !!i.container_running,
          serving: token === 'running' || token === 'degraded',
          token,
          word: stale ? 'unknown' : QUIET[token] || token,
          isDefault: s.name === b.default_site,
          devMode: detailOf(i.project, i.host)?.data?.dev_modes?.[`${b.path}/${s.name}`] || null,
          pub: i.host ? null : publicationAt(s.name, i.project, b.path),
        })
      }
    }
  }
  return out.sort((a, b) => a.site.name.localeCompare(b.site.name))
})
// Ask for the icons once the rows are known (and again when an instance starts); each
// remote host reads its own sites' favicons through the hub.
const iconRequest = computed(() => rows.value.map((r) => ({ host: r.host, project: r.project, bench_path: r.bench.path, site: r.site.name, running: r.running })))
function requestIcons(force = false) {
  const groups = new Map()
  for (const s of iconRequest.value) {
    if (!groups.has(s.host)) groups.set(s.host, [])
    groups.get(s.host).push({ project: s.project, bench_path: s.bench_path, site: s.site, running: s.running })
  }
  for (const [host, sites] of groups) refreshSiteIcons(sites, { force, host })
}
watch(() => JSON.stringify(iconRequest.value.map((s) => [s.host, s.project, s.site, s.running])), () => requestIcons(), { immediate: false })
watch(() => rows.value.length, (n, was) => { if (n && !was) requestIcons() })

const shown = computed(() => {
  const q = query.value.trim().toLowerCase()
  return rows.value.filter(
    (r) =>
      (project.value === 'all' || r.inst.key === project.value) &&
      (state.value === 'all' || (state.value === 'serving' ? r.serving : !r.serving)) &&
      (!q || r.site.name.toLowerCase().includes(q) || r.project.toLowerCase().includes(q) || benchName(r.bench).toLowerCase().includes(q) || r.apps.some((a) => a.name.includes(q)) || (r.host && hostName(r.host).toLowerCase().includes(q))),
  )
})
const projectOptions = computed(() => [{ label: 'Any instance', value: 'all' }, ...instances.value.map((i) => ({ label: i.host ? `${i.project} on ${hostName(i.host)}` : i.project, value: i.key }))])
const stateOptions = [{ label: 'All', value: 'all' }, { label: 'Serving', value: 'serving' }, { label: 'Not serving', value: 'stopped' }]
const loading = computed(() => instances.value.some((i) => detailOf(i.project, i.host)?.loading && !detailOf(i.project, i.host)?.data))
const filtered = computed(() => query.value.trim() || project.value !== 'all' || state.value !== 'all')
const withIcon = computed(() => rows.value.filter((r) => siteIcon(r.project, r.site.name, r.host)).length)
const ctx = (r) => ({ project: r.project, host: r.host, bench: String(r.bench.index), benchName: benchName(r.bench), site: r.site.name })
const whereLine = (r) => `${r.host ? hostName(r.host) + ' · ' : ''}${r.project} · ${benchName(r.bench)}`
const picked = computed(() => instances.value.find((i) => i.key === project.value))
// The column shows once publishing is possible or anything is published.
const publishing = computed(() => canPublish() || rows.value.some((r) => r.pub))
const columns = computed(() =>
  publishing.value
    ? { base: ['minmax(0,1fr)', '4.75rem'], lg: ['minmax(13rem,1.4fr)', '8.5rem', 'minmax(8rem,1fr)', 'minmax(14rem,1.3fr)', '6.5rem', '4.75rem'] }
    : { base: ['minmax(0,1fr)', '2.5rem'], lg: ['minmax(14rem,1.5fr)', '9rem', 'minmax(10rem,1.4fr)', '7rem', '2.5rem'] },
)
// One word for a publication: live, part-way, or failed.
function pubWord(p) {
  if (!p) return null
  if (isLive(p)) return { text: 'published', short: 'published', theme: 'green', cls: 'text-ink-green-5' }
  const failed = Object.entries(p.parts || {}).find(([, part]) => part.state === 'failed')
  if (failed) return { text: `${PART_LABEL[failed[0]].toLowerCase()} failed`, short: 'publish failed', theme: 'red', cls: 'text-ink-red-5' }
  return { text: 'publishing', short: 'publishing', theme: 'blue', cls: 'text-ink-blue-5' }
}
const partTip = (p, k) => `${PART_LABEL[k]}: ${PART_STATE[p.parts?.[k]?.state]?.label || 'waiting'}${p.parts?.[k]?.message ? ' · ' + p.parts[k].message : ''}`
function pubMenu(r) {
  const c = ctx(r)
  const open = r.pub ? [{ label: 'Open public address', icon: 'lucide-external-link', onClick: () => window.open(r.pub.url, '_blank', 'noopener') }] : []
  const manage = []
  if (!r.pub && canPublish() && isPublicName(r.site.name)) manage.push({ label: 'Publish…', icon: 'lucide-globe', onClick: () => openAction('site.publish', c) })
  if (r.pub && !isLive(r.pub)) manage.push({ label: 'Retry what is missing…', icon: 'lucide-rotate-cw', onClick: () => openAction('site.publish', c) })
  if (r.pub) manage.push({ label: 'Check for drift', icon: 'lucide-scan-search', onClick: () => checkSite(r.site.name) })
  const groups = [{ group: 'Public address', options: [...open, ...manage] }]
  if (r.pub && can('admin')) groups.push({ group: 'Danger zone', options: [{ label: 'Unpublish…', icon: 'lucide-globe-lock', theme: 'red', onClick: () => openAction('site.unpublish', c) }] })
  return groups
}
// A remote host's site is published from that machine's own web UI, never from here.
const hasMenu = (r) => !r.host && (!!r.pub || (canPublish() && isPublicName(r.site.name) && can('operator')))
</script>

<template>
  <PageHead title="Sites" :count="rows.length || null">
    <template #actions><Button v-if="can('operator')" variant="solid" icon-left="lucide-plus" label="New site" @click="openNewSite({ project: picked?.project || null, host: picked?.host || null })" /></template>
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
        <Button size="xs" variant="ghost" icon-left="lucide-refresh-cw" label="Re-read favicons" :disabled="siteIcons.refreshing || !rows.length" @click="requestIcons(true)" />
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
    <List v-else class="-mx-3 list-row-px-3" :columns="columns" :row-height="56">
      <ListHeader>
        <ListHeaderCell>Site</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Status</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Apps</ListHeaderCell>
        <ListHeaderCell v-if="publishing" class="max-lg:hidden">Public address</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Apps checked</ListHeaderCell>
        <ListHeaderCell><span class="sr-only">Open</span></ListHeaderCell>
      </ListHeader>
      <ListRows :items="shown" row-key="key" v-slot="{ item }">
        <ListRow :route="sitePath(item.inst, item.bench.index, item.site.name)">
          <ListCell class="gap-3 overflow-hidden">
            <SiteIcon :project="item.project" :host="item.host" :site="item.site.name" size="md" />
            <span class="min-w-0">
              <span class="flex items-center gap-1.5">
                <span class="truncate text-base-medium text-ink-gray-9">{{ item.site.name }}</span>
                <Badge v-if="item.isDefault" size="sm" theme="gray" variant="subtle" label="default" />
                <DevModeBadge :state="item.devMode" quiet-off />
                <Tooltip v-if="item.pub" :text="`${item.pub.url} · ${pubWord(item.pub).text}`"><span class="lucide-globe size-3.5 shrink-0 lg:hidden" :class="pubWord(item.pub).cls" role="img" :aria-label="`Public address ${pubWord(item.pub).short}`" /></Tooltip>
              </span>
              <span class="flex items-center gap-1.5 text-xs text-ink-gray-5"><span v-if="item.host" class="lucide-server size-3 shrink-0 text-ink-gray-4" aria-hidden="true" /><span class="truncate">{{ whereLine(item) }}</span><span class="flex shrink-0 items-center gap-1 lg:hidden"><StatusDot :token="item.token" />{{ item.word }}</span></span>
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
          <ListCell v-if="publishing" class="max-lg:hidden">
            <span v-if="item.pub" class="min-w-0">
              <span class="flex items-center gap-1.5"><span class="truncate text-sm text-ink-gray-8">{{ item.pub.url.replace(/^https?:\/\//, '') }}</span><Badge size="sm" variant="subtle" :theme="pubWord(item.pub).theme" :label="pubWord(item.pub).text" /></span>
              <span class="mt-0.5 flex items-center gap-2.5">
                <Tooltip v-for="k in SHOWN_PARTS" :key="k" :text="partTip(item.pub, k)">
                  <span class="flex items-center gap-1 text-xs text-ink-gray-6"><span class="size-3.5" :class="PART_STATE[item.pub.parts?.[k]?.state]?.icon || PART_STATE.waiting.icon" aria-hidden="true" />{{ PART_LABEL[k] }}</span>
                </Tooltip>
              </span>
            </span>
            <Tooltip v-else-if="item.host" :text="`Publishing is each machine's own: ${hostName(item.host)}'s web UI shows and changes this site's public address`"><span class="text-sm text-ink-gray-4">not shown here</span></Tooltip>
            <span v-else-if="isPublicName(item.site.name)" class="text-sm text-ink-gray-5">not published</span>
            <Tooltip v-else text="A .localhost site answers on this machine only; publishing needs its own domain"><span class="text-sm text-ink-gray-4">this machine only</span></Tooltip>
          </ListCell>
          <ListCell class="max-lg:hidden">
            <Tooltip :text="item.site.installed_apps_verified ? 'Read live by a full inspect' : 'Remembered from the cache; a full inspect verifies them'">
              <span class="text-sm" :class="item.site.installed_apps_verified ? 'text-ink-green-6' : 'text-ink-gray-5'">{{ item.site.installed_apps_verified ? 'verified' : 'remembered' }}</span>
            </Tooltip>
          </ListCell>
          <ListCell class="justify-end gap-0.5" @click.stop.prevent>
            <Dropdown v-if="publishing && hasMenu(item)" :options="pubMenu(item)" placement="right">
              <Button size="sm" variant="ghost" icon="lucide-globe" :aria-label="`Public address of ${item.site.name}`" />
            </Dropdown>
            <Tooltip :text="item.serving ? `Open ${item.site.name} in a new tab` : item.running ? `Bench ${benchName(item.bench)} is not serving; start it first` : `${item.project} is stopped; cwcli asks to start it first`">
              <Button size="sm" variant="ghost" icon="lucide-external-link" :aria-label="`Open ${item.site.name}`" @click.stop.prevent="openAction('site.open', ctx(item))" />
            </Tooltip>
          </ListCell>
        </ListRow>
      </ListRows>
    </List>
  </div>
</template>
