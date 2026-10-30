<script setup>
import { computed } from 'vue'
import { Button, Dropdown } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import { parseApp } from '../data/fleet'
import AppNameCell from './AppNameCell.vue'
import { catalogApp } from '../data/catalog'
import { appOpener } from '../data/app-panel'
import { menuOption, dangerGroup } from '../data/dialog'
import UpdatePill from './UpdatePill.vue'
import { updatesFor } from '../data/updates'
import { openJob } from '../data/job-dialog'

const props = defineProps({
  bench: { type: Object, required: true },
  project: { type: String, required: true },
  host: { type: String, default: null }, // a remote host's bench: jobs run there through the hub
})

// available_apps on the bench, joined with what each site has installed.
const rows = computed(() =>
  (props.bench.available_apps || []).map((name) => {
    const installs = []
    for (const site of props.bench.sites || []) {
      const hit = site.installed_apps.map(parseApp).find((a) => a.name === name)
      if (hit) installs.push({ site: site.name, ...hit })
    }
    const versions = [...new Set(installs.map((i) => i.version).filter(Boolean))]
    const branches = [...new Set(installs.map((i) => i.branch).filter(Boolean))]
    return { name, installs, versions, branches }
  }),
)
const ctx = (name) => ({ project: props.project, host: props.host, bench: String(props.bench.index), benchName: props.bench.label, app: name })
// Per-app menu: the plain checkout, then the reset form behind its own typed confirm.
const menu = (name) => [{ group: 'Update', options: [{ label: `Update ${name}`, icon: 'lucide-package-check', onClick: () => openJob('apps-update', { ...ctx(name), args: { apps: name, sites: '' } }) }] }, menuOption('app.checkout', ctx(name)), dangerGroup(['app.checkout_reset'], ctx(name))]
</script>

<template>
  <List class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '6rem', '2.5rem'], md: ['minmax(12rem,1.5fr)', '6rem', 'minmax(0,1fr)', '5.5rem', '2.5rem'] }" :row-height="44">
    <ListHeader>
      <ListHeaderCell>App</ListHeaderCell>
      <ListHeaderCell>Version</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Branch at install</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Sites</ListHeaderCell>
      <ListHeaderCell />
    </ListHeader>
    <ListRows :items="rows" row-key="name" v-slot="{ item }">
      <ListRow :on-click="appOpener(host, item.name)">
        <!-- A remote host's row opens no panel: the app panel is this machine's catalog. -->
        <AppNameCell :app="catalogApp(item.name)" :name="item.name"><UpdatePill :item="updatesFor(project, bench.index, host).find((u) => u.app === item.name) || null" compact /></AppNameCell>
        <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.versions.join(', ') || 'unknown' }}</span></ListCell>
        <ListCell class="gap-1 overflow-hidden max-md:hidden">
          <span v-if="!item.branches.length" class="text-sm text-ink-gray-5">not installed on a site</span>
          <span v-else class="truncate font-mono text-xs text-ink-gray-6"><span class="lucide-git-branch mr-1 inline-block size-3 align-[-2px] text-ink-gray-4" aria-hidden="true" />{{ item.branches.slice(0, 2).join(' · ') }}</span>
        </ListCell>
        <ListCell class="max-md:hidden"><span class="tabular-nums text-sm text-ink-gray-6">{{ item.installs.length }} / {{ bench.sites.length }}</span></ListCell>
        <ListCell class="justify-end">
          <Dropdown :options="menu(item.name)" align="end">
            <Button size="xs" variant="ghost" icon="lucide-ellipsis" :aria-label="`Actions for ${item.name}`" />
          </Dropdown>
        </ListCell>
      </ListRow>
    </ListRows>
  </List>
</template>
