<script setup>
// Build A with the captain's picks from B: the summary strip above the grid and
// storefront tiles (64 px logos, lock on private repos, source shown). A tile
// opens the Frappe Cloud style detail panel.
import { computed, onMounted } from 'vue'
import { Button } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import AppsGrid from '../components/AppsGrid.vue'
import HostIcon from '../components/HostIcon.vue'
import { setFocus, localInstances } from '../data/fleet'
import { apps, catalog, scanCatalog, activeSources, ago, hostLabel } from '../data/catalog'
import { can } from '../data/session'

onMounted(() => setFocus(null))
const counts = computed(() => ({
  both: apps.value.filter((a) => a.state === 'both').length,
  installed: apps.value.filter((a) => a.state === 'installed').length,
  remote: apps.value.filter((a) => a.state === 'remote').length,
  logos: apps.value.filter((a) => a.logo).length,
}))
const running = computed(() => localInstances.value.filter((i) => i.container_running).length)
</script>

<template>
  <PageHead title="Apps" :count="apps.length || null">
    <template #actions>
      <Button v-if="can('operator')" icon-left="lucide-refresh-cw" label="Rescan sources" :loading="catalog.scanning" @click="scanCatalog()" />
    </template>
  </PageHead>
  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <div class="grid gap-px overflow-hidden rounded-7 border border-outline-gray-1 bg-surface-gray-2 sm:grid-cols-2 lg:grid-cols-4">
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Installed and in a repo</div>
        <div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ catalog.data ? counts.both : '…' }}</div>
        <div class="mt-0.5 text-xs text-ink-gray-5">on {{ localInstances.length }} {{ localInstances.length === 1 ? 'instance' : 'instances' }}, {{ running }} running</div>
      </div>
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Only on a bench</div>
        <div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ catalog.data ? counts.installed : '…' }}</div>
        <div class="mt-0.5 text-xs text-ink-gray-5">no reachable remote, or a stopped instance</div>
      </div>
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Ready to install</div>
        <div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ catalog.data ? counts.remote : '…' }}</div>
        <div class="mt-0.5 text-xs text-ink-gray-5">Frappe apps in your repositories, on no bench yet</div>
      </div>
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Sources</div>
        <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
          <span v-for="s in activeSources" :key="s.host" class="flex items-center gap-1 text-sm text-ink-gray-8">
            <HostIcon :host="s.host" size="size-4" />{{ hostLabel(s.host) }}
          </span>
        </div>
        <div class="mt-0.5 text-xs text-ink-gray-5">{{ catalog.data ? `scanned ${ago(catalog.data.scanned_at)}` : 'reading installed apps and the last scan' }}</div>
      </div>
    </div>
    <AppsGrid density="storefront" columns="grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4" />
  </div>
</template>
