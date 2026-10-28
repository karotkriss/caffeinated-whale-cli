<script setup>
// One app in the grid. Two densities: `storefront` (Frappe Cloud marketplace
// prominence: a large logo leads, then title, module, description, source and
// where it is installed) and `compact` (Pilot's marketplace tile: logo left,
// title, installed check or Install, one description line).
import { computed } from 'vue'
import { Badge, Button, Tooltip } from 'frappe-ui'
import AppIcon from './AppIcon.vue'
import HostIcon from './HostIcon.vue'
import UpdatePill from './UpdatePill.vue'
import { updatesForApp } from '../data/updates'
import { installText, namespaceOf, sourceLabel, STATE_LABEL, STATE_THEME } from '../data/catalog'
import { openInstall } from '../data/app-panel'

const props = defineProps({
  app: { type: Object, required: true },
  density: { type: String, default: 'storefront' }, // storefront | compact
})
const emit = defineEmits(['select'])
const installed = computed(() => props.app.state !== 'remote')
const update = computed(() => updatesForApp(props.app)[0] || null)
const where = computed(() => installText(props.app))
const canInstall = computed(() => !!props.app.source?.url || installed.value)
const live = computed(() => (props.app.installs || []).some((i) => i.verified))
const ref = computed(() => {
  const refs = [...new Set((props.app.installs || []).map((i) => i.checked_out).filter(Boolean))]
  return refs.length === 1 ? refs[0] : refs.length ? `${refs.length} refs` : null
})
</script>

<template>
  <button
    v-if="density === 'compact'"
    class="group flex w-full items-start gap-3 rounded-6 border border-outline-gray-1 bg-surface-elevation-1 p-3 text-left transition hover:border-outline-gray-3 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4"
    @click="emit('select', app)"
  >
    <AppIcon :app="app" size="md" />
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-2">
        <span class="truncate text-base-medium text-ink-gray-9">{{ app.title }}</span>
        <Tooltip v-if="app.official" text="Official Frappe app (github.com/frappe)"><span class="lucide-badge-check size-3.5 shrink-0 text-ink-blue-6" aria-hidden="true" /></Tooltip>
        <span v-if="app.source?.private" class="lucide-lock size-3 shrink-0 text-ink-gray-4" aria-hidden="true" title="private repository" />
        <span class="ml-auto flex shrink-0 items-center gap-1">
          <UpdatePill v-if="update" :item="update" />
          <Tooltip v-if="installed" :text="where || 'installed'"><span class="lucide-circle-check size-4 text-ink-green-5" aria-hidden="true" /></Tooltip>
          <Button v-else-if="canInstall" size="xs" label="Install" @click.stop="openInstall(app)" />
        </span>
      </div>
      <p class="mt-0.5 truncate text-sm text-ink-gray-5">{{ app.description || app.name }}</p>
      <div class="mt-1.5 flex items-center gap-2 text-xs text-ink-gray-5">
        <span class="flex min-w-0 items-center gap-1"><HostIcon :host="app.source?.kind === 'marketplace' ? 'marketplace' : app.source?.host" size="size-3" /><span class="truncate">{{ sourceLabel(app) }}<template v-if="namespaceOf(app)"> · {{ namespaceOf(app) }}</template></span></span>
        <span v-if="ref" class="truncate font-mono">{{ ref }}</span>
        <span v-if="app.source?.stars" class="flex items-center gap-0.5"><span class="lucide-star size-3" aria-hidden="true" />{{ app.source.stars }}</span>
      </div>
    </div>
  </button>

  <button
    v-else
    class="group flex w-full flex-col rounded-7 border border-outline-gray-1 bg-surface-elevation-1 p-4 text-left transition hover:border-outline-gray-3 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4"
    @click="emit('select', app)"
  >
    <div class="flex w-full items-start gap-3">
      <AppIcon :app="app" size="xl" />
      <div class="min-w-0 flex-1 pt-0.5">
        <div class="flex items-start gap-1.5">
          <span class="line-clamp-2 text-lg-semibold leading-6 text-ink-gray-9">{{ app.title }}</span>
          <Tooltip v-if="app.official" text="Official Frappe app (github.com/frappe)"><span class="lucide-badge-check mt-1 size-4 shrink-0 text-ink-blue-6" aria-hidden="true" /></Tooltip>
          <span v-if="app.source?.private" class="lucide-lock mt-1.5 size-3.5 shrink-0 text-ink-gray-4" aria-hidden="true" title="private repository" />
        </div>
        <div class="mt-0.5 truncate font-mono text-xs text-ink-gray-5">{{ app.name }}</div>
      </div>
    </div>
    <p class="mt-3 line-clamp-2 min-h-10 text-p-sm text-ink-gray-6">{{ app.description || 'No description in the repository.' }}</p>
    <div class="mt-3 flex w-full items-center gap-2 border-t border-outline-gray-1 pt-3">
      <span class="flex min-w-0 items-center gap-1 text-xs text-ink-gray-5"><HostIcon :host="app.source?.kind === 'marketplace' ? 'marketplace' : app.source?.host" /><span class="truncate">{{ sourceLabel(app) }}<template v-if="namespaceOf(app)"> · {{ namespaceOf(app) }}</template>{{ app.source?.categories?.length ? ' · ' + app.source.categories.slice(0, 2).join(', ') : '' }}</span></span>
      <UpdatePill v-if="update" :item="update" class="ml-auto shrink-0" />
      <Badge size="sm" :theme="STATE_THEME[app.state]" variant="subtle" :label="installed ? (where || STATE_LABEL[app.state]) : 'Not installed'" class="shrink-0" :class="update ? '' : 'ml-auto'" />
      <Button v-if="!installed && canInstall" size="sm" variant="subtle" label="Install" @click.stop="openInstall(app)" />
    </div>
    <div v-if="installed" class="mt-1.5 flex w-full items-center gap-2 text-xs text-ink-gray-4">
      <span :class="live ? 'text-ink-green-6' : ''">{{ live ? 'remote verified live' : 'remembered from the cache' }}</span>
      <span v-if="ref" class="ml-auto truncate font-mono" :title="`checked out: ${ref}`">{{ ref }}</span>
    </div>
  </button>
</template>
