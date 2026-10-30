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
import { instances, detailOf, benchName, search, instancePath, benchPath, sitePath } from '../data/fleet'
import { hostName } from '../data/hosts'
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

const benches = computed(() => instances.value.flatMap((i) => (detailOf(i.project, i.host)?.data?.benches || []).map((b) => ({ i, p: i.project, b }))))
const sites = computed(() => benches.value.flatMap(({ i, p, b }) => (b.sites || []).map((s) => ({ i, p, b, s: s.name }))))
const where = (i) => (i.host ? ` on ${hostName(i.host)}` : '')
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
    if (availability('instance.start', i).ok) out.push({ id: 'instance.start', label: `Start ${i.project}${where(i)}`, icon: 'lucide-play', ctx: { project: i.project, host: i.host } })
    if (availability('instance.stop', i).ok) out.push({ id: 'instance.stop', label: `Stop ${i.project}${where(i)}`, icon: 'lucide-square', ctx: { project: i.project, host: i.host } })
    if (availability('instance.refresh', i).ok) out.push({ id: 'instance.refresh', label: `Refresh ${i.project} health${where(i)}`, icon: 'lucide-refresh-cw', ctx: { project: i.project, host: i.host } })
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
        <CommandPaletteItem v-for="i in instances" :key="i.key" :value="{ to: instancePath(i) }" :keywords="i.host ? [hostName(i.host)] : []">
          <template #prefix><StatusDot :token="i.overall" class="mr-2" /></template>
          {{ i.project }}
          <template v-if="i.host" #suffix><span class="text-xs text-ink-gray-5">{{ hostName(i.host) }}</span></template>
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Benches">
        <CommandPaletteItem v-for="{ i, p, b } in benches" :key="i.key + b.index" :value="{ to: benchPath(i, b.index) }" :keywords="[b.path]">
          <template #prefix><span class="lucide-layers mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          {{ benchName(b) }}
          <template #suffix><span class="text-xs text-ink-gray-5">{{ p }}{{ where(i) }} #{{ b.index }}</span></template>
        </CommandPaletteItem>
      </CommandPaletteGroup>
      <CommandPaletteGroup label="Sites">
        <CommandPaletteItem v-for="{ i, b, s } in sites" :key="i.key + b.index + s" :value="{ to: sitePath(i, b.index, s) }">
          <template #prefix><span class="lucide-globe mr-1 size-4 text-ink-gray-6" aria-hidden="true" /></template>
          {{ s }}
          <template #suffix><span class="text-xs text-ink-gray-5">{{ benchName(b) }}{{ where(i) }}</span></template>
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
        <CommandPaletteItem v-for="c in commands" :key="c.id + (c.ctx.host || '') + c.ctx.project" :value="{ action: c.id, ctx: c.ctx }">
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
