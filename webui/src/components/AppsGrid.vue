<script setup>
// The apps grid: every Frappe app the user has or can reach, filtered, with the
// sources' sign-in and scan state stated plainly above it (never hidden).
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, ErrorMessage, Select, TabButtons, TextInput, Tooltip, toast } from 'frappe-ui'
import AppTile from './AppTile.vue'
import EmptyState from './EmptyState.vue'
import HostIcon from './HostIcon.vue'
import { ago, apps, catalog, categories, filterApps, hostFilterOptions, hostLabel, loadCatalog, progressText, scanCatalog, sources } from '../data/catalog'
import { localInstances } from '../data/fleet'
import { openApp } from '../data/app-panel'
import { loadUpdates, updatesForApp } from '../data/updates'

const props = defineProps({
  density: { type: String, default: 'storefront' }, // storefront | compact
  columns: { type: String, default: 'grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4' },
  limit: { type: Number, default: 0 }, // 0 = all
  toolbar: { type: Boolean, default: true },
  initialState: { type: String, default: 'all' },
})
const emit = defineEmits(['count'])
onMounted(() => { loadCatalog(); loadUpdates() })

const query = ref('')
const source = ref('all')
const state = ref(props.initialState)
const project = ref('all')
const category = ref('all')
// Official Frappe apps (github.com/frappe/*, the verified-badge rule): a chip
// beside the other filters, kept in the URL so a link carries it (captain note 04:12Z).
const route = useRoute()
const router = useRouter()
const official = computed({
  get: () => route.query.official === '1',
  set: (on) => router.replace({ query: { ...route.query, official: on ? '1' : undefined } }),
})
const officialCount = computed(() => apps.value.filter((a) => a.official).length)
const updatesOnly = computed({
  get: () => route.query.updates === '1',
  set: (on) => router.replace({ query: { ...route.query, updates: on ? '1' : undefined } }),
})
const updateKeys = computed(() => new Set(apps.value.filter((a) => updatesForApp(a).length).map((a) => a.key)))
const updatesCount = computed(() => updateKeys.value.size)
const categoryOptions = computed(() => [{ label: 'Any category', value: 'all' }, ...categories.value.map((c) => ({ label: c, value: c }))])

const sourceOptions = computed(() => [
  { label: 'All sources', value: 'all' },
  { label: 'Installed anywhere', value: 'installed' },
  ...hostFilterOptions.value,
  { label: 'Marketplace (public index)', value: 'marketplace' },
  { label: 'Local only', value: 'local' },
])
const projectOptions = computed(() => [{ label: 'Any instance', value: 'all' }, ...localInstances.value.map((i) => ({ label: i.project, value: i.project }))])
const stateOptions = [
  { label: 'All', value: 'all' },
  { label: 'Installed', value: 'both' },
  { label: 'Local', value: 'installed' },
  { label: 'Remote', value: 'remote' },
]
const rows = computed(() => {
  const list = filterApps(apps.value, { query: query.value, source: source.value, state: state.value, project: project.value, category: category.value, official: official.value, updatesOnly: updatesOnly.value ? updateKeys.value : null })
  emit('count', list.length)
  return props.limit ? list.slice(0, props.limit) : list
})
const filtered = computed(() => query.value.trim() || source.value !== 'all' || state.value !== 'all' || project.value !== 'all' || category.value !== 'all' || official.value || updatesOnly.value)
const signedIn = computed(() => sources.value.filter((s) => s.signed_in))

async function rescan() {
  try {
    await scanCatalog()
    toast.success('Scanning GitHub and GitLab in the background', { description: 'The grid updates when the scan lands.' })
  } catch (e) {
    toast.error('Could not start the scan', { description: e.hint ? `${e.message} ${e.hint}` : e.message })
  }
}
</script>

<template>
  <div class="space-y-4">
    <div v-if="toolbar" class="flex flex-wrap items-center gap-2">
      <TextInput v-model="query" class="w-64 max-w-full" type="search" placeholder="Search apps, repos, instances">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <Select v-model="source" :options="sourceOptions" />
      <Select v-model="project" :options="projectOptions" />
      <Select v-if="categories.length" v-model="category" :options="categoryOptions" />
      <button
        type="button"
        class="inline-flex h-7 items-center gap-1.5 rounded-4 border px-2.5 text-base transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4"
        :class="official ? 'border-outline-blue-2 bg-surface-blue-1 text-ink-blue-6' : 'border-outline-gray-2 bg-surface-elevation-1 text-ink-gray-7 hover:bg-surface-gray-2'"
        :aria-pressed="official"
        title="Only official Frappe apps (github.com/frappe)"
        @click="official = !official"
      >
        <span class="lucide-badge-check size-3.5" aria-hidden="true" />Official<span class="tabular-nums" :class="official ? 'text-ink-blue-5' : 'text-ink-gray-5'">{{ officialCount }}</span>
      </button>
      <button
        type="button"
        class="inline-flex h-7 items-center gap-1.5 rounded-4 border px-2.5 text-base transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4"
        :class="updatesOnly ? 'border-outline-orange-2 bg-surface-orange-1 text-ink-orange-6' : 'border-outline-gray-2 bg-surface-elevation-1 text-ink-gray-7 hover:bg-surface-gray-2'"
        :aria-pressed="updatesOnly"
        title="Only apps with an update available on a running instance"
        @click="updatesOnly = !updatesOnly"
      >
        <span class="lucide-arrow-up-circle size-3.5" aria-hidden="true" />Updates<span class="tabular-nums" :class="updatesOnly ? 'text-ink-orange-5' : 'text-ink-gray-5'">{{ updatesCount }}</span>
      </button>
      <TabButtons v-model="state" :options="stateOptions" />
      <span class="ml-auto text-sm tabular-nums text-ink-gray-5">{{ rows.length }} of {{ apps.length }}</span>
    </div>

    <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-gray-5">
      <template v-for="s in signedIn" :key="s.host">
        <Tooltip :text="s.error ? s.error : `${s.scope}: ${s.repos} ${s.kind === 'github' ? 'repositories' : 'projects'} scanned, ${s.apps} proven Frappe apps · ${s.state}`">
          <span class="flex items-center gap-1" :class="s.error ? 'text-ink-amber-6' : ''">
            <HostIcon :host="s.host" />
            {{ hostLabel(s.host) }} · {{ s.account || 'signed in' }}
            <span v-if="progressText(s)" class="text-ink-blue-6">· scanning {{ progressText(s) }}</span>
            <span v-else-if="s.repos" class="text-ink-gray-4">· {{ s.kind === 'marketplace' ? `${s.apps} apps` : `${s.apps} apps of ${s.repos} scanned` }}</span>
            <span v-if="s.error" class="text-ink-amber-6">· {{ s.state }}</span>
          </span>
        </Tooltip>
      </template>

      <span v-if="catalog.data" class="ml-auto flex items-center gap-2">
        <span v-if="catalog.scanning" class="flex items-center gap-1 text-ink-blue-6"><span class="lucide-loader-circle size-3.5 animate-spin" aria-hidden="true" />{{ catalog.progress ? `scanning ${catalog.progress.host} · ${catalog.progress.done} of ${catalog.progress.total} projects` : 'scanning' }}</span>
        <span v-else>remote scanned {{ ago(catalog.data.scanned_at) }}</span>
        <Button size="xs" variant="ghost" icon-left="lucide-refresh-cw" label="Rescan" :disabled="catalog.scanning" @click="rescan" />
      </span>
    </div>
    <ErrorMessage v-if="catalog.error" :message="catalog.error.hint ? `${catalog.error.message} ${catalog.error.hint}` : catalog.error.message" />

    <div v-if="catalog.loading && !catalog.data" class="grid gap-3" :class="columns">
      <div v-for="i in 6" :key="i" class="h-36 animate-pulse rounded-7 border border-outline-gray-1 bg-surface-gray-1" />
    </div>
    <EmptyState
      v-else-if="catalog.data && !rows.length"
      icon="lucide-package"
      :title="filtered ? 'No app matches these filters' : 'No Frappe apps found yet'"
      :description="filtered ? 'Clear a filter to widen the grid.' : catalog.scanning ? 'The first scan of your forges is running; installed apps appear as cwcli inspects instances.' : 'Sign in with gh or glab, then rescan. Installed apps appear once an instance is inspected.'"
      :command="filtered ? '' : 'cwcli inspect <instance>'"
    />
    <div v-else class="grid gap-3" :class="columns">
      <AppTile v-for="a in rows" :key="a.key" :app="a" :density="density" @select="openApp" />
    </div>
  </div>
</template>
