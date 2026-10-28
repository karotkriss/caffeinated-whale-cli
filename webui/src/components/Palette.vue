<script setup>
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { KeyboardShortcut, useKeyboardShortcut } from 'frappe-ui'
import {
  CommandPalette,
  CommandPaletteInput,
  CommandPaletteList,
  CommandPaletteGroup,
  CommandPaletteItem,
  CommandPaletteEmpty,
  CommandPaletteFooter,
} from 'frappe-ui/experimental'
import StatusDot from './StatusDot.vue'
import { instances, details, benchName, search } from '../data/fleet'
import { paletteOpen } from '../data/palette'
import { openAction } from '../data/dialog'
import { availability } from '../data/actions'
import { openSettings } from '../data/settings-dialog'

const router = useRouter()
const query = ref('')
const apps = ref([])
let timer = null
// Apps come from core.where's cache search, verified-vs-remembered token intact.
watch(query, (q) => {
  clearTimeout(timer)
  if (q.trim().length < 2) return (apps.value = [])
  timer = setTimeout(
    () =>
      search(q.trim())
        .then((r) => (apps.value = r.matches.filter((m) => m.type === 'app').slice(0, 8)))
        .catch(() => (apps.value = [])),
    200,
  )
})
const open = computed({ get: () => paletteOpen.value, set: (v) => (paletteOpen.value = v) })

useKeyboardShortcut({
  combo: 'Mod+K',
  description: 'Search or run a command',
  allowInInput: true,
  handler: () => (paletteOpen.value = true),
})

const benches = computed(() => Object.entries(details).flatMap(([p, d]) => (d.data?.benches || []).map((b) => ({ p, b }))))
const sites = computed(() => benches.value.flatMap(({ p, b }) => (b.sites || []).map((s) => ({ p, b, s: s.name }))))
const pages = [
  ['Instances', 'lucide-boxes', '/instances'],
  ['Apps', 'lucide-package', '/apps'],
  ['Sites', 'lucide-globe', '/sites'],
  ['Health check', 'lucide-stethoscope', 'settings:health'],
  ['Settings', 'lucide-settings', 'settings'],
]
const commands = computed(() =>
  instances.value.flatMap((i) => {
    const out = []
    if (availability('instance.start', i).ok) out.push({ id: 'instance.start', label: `Start ${i.project}`, icon: 'lucide-play', ctx: { project: i.project } })
    if (availability('instance.stop', i).ok) out.push({ id: 'instance.stop', label: `Stop ${i.project}`, icon: 'lucide-square', ctx: { project: i.project } })
    if (availability('instance.refresh', i).ok) out.push({ id: 'instance.refresh', label: `Refresh ${i.project} health`, icon: 'lucide-refresh-cw', ctx: { project: i.project } })
    return out
  }),
)

function select(value) {
  paletteOpen.value = false
  if (value.to?.startsWith('settings')) openSettings(value.to.split(':')[1])
  else if (value.to) router.push(value.to)
  else if (value.action) openAction(value.action, value.ctx || {})
}
</script>

<template>
  <CommandPalette v-model:open="open" v-model:query="query" title="Search or run a command" @select="select">
    <CommandPaletteInput placeholder="Search instances, benches, sites, apps, or run a command" />
    <CommandPaletteList>
      <CommandPaletteGroup label="Go to">
        <CommandPaletteItem v-for="[label, icon, to] in pages" :key="to" :value="{ to }">
          <template #prefix><span :class="icon" class="mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          {{ label }}
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Instances">
        <CommandPaletteItem v-for="i in instances" :key="i.project" :value="{ to: `/instances/${i.project}` }">
          <template #prefix><StatusDot :token="i.overall" class="mr-2" /></template>
          {{ i.project }}
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Benches">
        <CommandPaletteItem v-for="{ p, b } in benches" :key="p + b.index" :value="{ to: `/instances/${p}/benches/${b.index}` }" :keywords="[b.path]">
          <template #prefix><span class="lucide-layers mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          {{ benchName(b) }}
          <template #suffix><span class="text-xs text-ink-gray-5">{{ p }} #{{ b.index }}</span></template>
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Sites">
        <CommandPaletteItem v-for="{ p, b, s } in sites" :key="p + b.index + s" :value="{ to: `/instances/${p}/benches/${b.index}/sites/${s}` }">
          <template #prefix><span class="lucide-globe mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          {{ s }}
          <template #suffix><span class="text-xs text-ink-gray-5">{{ benchName(b) }}</span></template>
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Apps, from the cache">
        <CommandPaletteItem
          v-for="m in apps"
          :key="m.project + m.bench + m.name + m.site"
          :label="m.name"
          :keywords="[query]"
          :value="{ to: `/instances/${m.project}` }"
          :disabled="m.project_state === 'absent'"
        >
          <template #prefix><span class="lucide-package mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          <span class="font-mono">{{ m.name }}</span>
          <span class="ml-2 text-sm text-ink-gray-5">{{ m.version }} {{ m.branch }}</span>
          <template #suffix>
            <span class="text-xs" :class="m.project_state === 'present' ? 'text-ink-gray-5' : 'text-ink-amber-6'">
              {{ m.project }}{{ m.site ? ' · ' + m.site : '' }}{{ m.project_state === 'absent' ? ' · removed' : m.project_state === 'unverified' ? ' · unverified' : '' }}
            </span>
          </template>
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Commands">
        <CommandPaletteItem v-for="c in commands" :key="c.id + c.ctx.project" :value="{ action: c.id, ctx: c.ctx }">
          <template #prefix><span :class="c.icon" class="mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          {{ c.label }}
        </CommandPaletteItem>
      </CommandPaletteGroup>
    </CommandPaletteList>
    <CommandPaletteEmpty>Nothing matches in instances, benches, sites or the app cache.</CommandPaletteEmpty>
    <CommandPaletteFooter>
      <span class="flex items-center gap-1.5"><KeyboardShortcut combo="Enter" /> open</span>
      <span class="flex items-center gap-1.5"><KeyboardShortcut combo="Escape" /> close</span>
    </CommandPaletteFooter>
  </CommandPalette>
</template>
