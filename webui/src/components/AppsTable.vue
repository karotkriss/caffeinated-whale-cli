<script setup>
import { computed } from 'vue'
import { Badge, Button, Tooltip } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import { parseApp, instance } from '../data/fleet'
import { openAction } from '../data/dialog'
import { availability } from '../data/actions'

const props = defineProps({
  bench: { type: Object, required: true },
  project: { type: String, required: true },
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
const checkout = computed(() => availability('app.checkout', instance(props.project)))
const ctx = (name) => ({ project: props.project, bench: String(props.bench.index), benchName: props.bench.label, app: name })
</script>

<template>
  <List class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '6rem', '2.5rem'], md: ['minmax(9rem,1fr)', '6rem', 'minmax(0,1.6fr)', '5.5rem', '2.5rem'] }" :row-height="44">
    <ListHeader>
      <ListHeaderCell>App</ListHeaderCell>
      <ListHeaderCell>Version</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Branch at install</ListHeaderCell>
      <ListHeaderCell class="max-md:hidden">Sites</ListHeaderCell>
      <ListHeaderCell />
    </ListHeader>
    <ListRows :items="rows" row-key="name" v-slot="{ item }">
      <ListRow>
        <ListCell><span class="font-mono text-sm text-ink-gray-8">{{ item.name }}</span></ListCell>
        <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.versions.join(', ') || 'unknown' }}</span></ListCell>
        <ListCell class="gap-1 overflow-hidden max-md:hidden">
          <span v-if="!item.branches.length" class="text-sm text-ink-gray-5">not installed on a site</span>
          <Badge v-for="b in item.branches.slice(0, 2)" :key="b" size="sm" variant="outline" theme="gray">
            <template #prefix><span class="lucide-git-branch size-3" aria-hidden="true" /></template>
            {{ b }}
          </Badge>
        </ListCell>
        <ListCell class="max-md:hidden"><span class="tabular-nums text-sm text-ink-gray-6">{{ item.installs.length }} / {{ bench.sites.length }}</span></ListCell>
        <ListCell class="justify-end">
          <Tooltip :text="checkout.ok ? 'Check out a branch, tag or commit' : checkout.reason">
            <Button size="xs" variant="ghost" icon="lucide-git-branch" aria-label="Check out ref" :disabled="!checkout.ok" @click="openAction('app.checkout', ctx(item.name))" />
          </Tooltip>
        </ListCell>
      </ListRow>
    </ListRows>
  </List>
</template>
