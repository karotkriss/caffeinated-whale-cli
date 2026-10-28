<script setup>
import { Badge, Tooltip } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import { parseApp } from '../data/fleet'
import DevModeBadge from './DevModeBadge.vue'

defineProps({
  bench: { type: Object, required: true },
  route: { type: Function, default: null }, // (site) => route location
  devModes: { type: Object, default: () => ({}) }, // detail.dev_modes, keyed `<bench path>/<site>`
})
const appsOf = (site) => site.installed_apps.map(parseApp).filter((a) => a.name !== 'frappe')
</script>

<template>
  <div v-if="!bench.sites?.length" class="px-3 py-8 text-center text-p-sm text-ink-gray-5">No sites on this bench.</div>
  <List v-else class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '6rem'], md: ['minmax(12rem,1fr)', 'minmax(0,1.4fr)', '7.5rem'] }" :row-height="44">
    <ListHeader>
      <ListHeaderCell>Site</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Apps</ListHeaderCell>
      <ListHeaderCell>Apps checked</ListHeaderCell>
    </ListHeader>
    <ListRows :items="bench.sites" row-key="name" v-slot="{ item }">
      <ListRow :route="route ? route(item) : undefined">
        <ListCell class="gap-2">
          <span class="truncate font-mono text-sm text-ink-gray-8">{{ item.name }}</span>
          <Badge v-if="item.name === bench.default_site" size="sm" theme="gray" label="default" />
          <DevModeBadge :state="devModes[`${bench.path}/${item.name}`]" quiet-off />
        </ListCell>
        <ListCell class="gap-1 overflow-hidden max-md:hidden">
          <span v-if="!appsOf(item).length" class="text-sm text-ink-gray-5">frappe only</span>
          <Badge v-for="a in appsOf(item).slice(0, 3)" :key="a.name" size="sm" variant="outline" theme="gray" :label="a.name" />
          <span v-if="appsOf(item).length > 3" class="text-xs text-ink-gray-5">+{{ appsOf(item).length - 3 }}</span>
        </ListCell>
        <ListCell>
          <Tooltip :text="item.installed_apps_verified ? 'Read live by a full inspect' : 'Remembered from the cache; a full inspect verifies them'">
            <span class="text-sm" :class="item.installed_apps_verified ? 'text-ink-green-6' : 'text-ink-gray-5'">
              {{ item.installed_apps_verified ? 'verified' : 'remembered' }}
            </span>
          </Tooltip>
        </ListCell>
      </ListRow>
    </ListRows>
  </List>
</template>
