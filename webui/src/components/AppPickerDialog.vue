<script setup>
// A multiselect for apps (captain note 40): the field shows what is picked as chips,
// this modal does the choosing - search, select all / none, one row per app with its
// icon, title and module name. `locked` entries are always included and only shown.
import { computed, ref, watch } from 'vue'
import { Button, Checkbox, Dialog, TextInput } from 'frappe-ui'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: 'Choose apps' },
  options: { type: Array, default: () => [] }, // [{ value, label, app?, hint?, section?, name? }]
  locked: { type: Array, default: () => [] }, // [{ value, label, app? }] always included
  modelValue: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:open', 'update:modelValue'])
const query = ref('')
const draft = ref([])
watch(() => props.open, (o) => { if (o) { draft.value = [...props.modelValue]; query.value = '' } })
const q = computed(() => query.value.trim().toLowerCase())
const shown = computed(() => (q.value ? props.options.filter((o) => `${o.label} ${o.value} ${o.hint || ''}`.toLowerCase().includes(q.value)) : props.options))
const has = (v) => draft.value.includes(v)
function toggle(v, on) {
  draft.value = on ? [...new Set([...draft.value, v])] : draft.value.filter((x) => x !== v)
}
function all(on) {
  const vis = shown.value.map((o) => o.value)
  draft.value = on ? [...new Set([...draft.value, ...vis])] : draft.value.filter((v) => !vis.includes(v))
}
function done(close) {
  emit('update:modelValue', props.options.map((o) => o.value).filter((v) => draft.value.includes(v)))
  close()
}
</script>

<template>
  <Dialog :open="open" :title="title" size="xl" @update:open="emit('update:open', $event)">
    <div class="space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <TextInput v-model="query" class="w-64 max-w-full" type="search" placeholder="Search apps" autofocus>
          <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
        </TextInput>
        <Button size="sm" variant="ghost" label="Select all" :disabled="!shown.length" @click="all(true)" />
        <Button size="sm" variant="ghost" label="None" :disabled="!draft.length" @click="all(false)" />
        <span class="ml-auto text-xs tabular-nums text-ink-gray-5">{{ draft.length }} of {{ options.length }} picked</span>
      </div>
      <div class="max-h-[50vh] overflow-auto rounded-6 border border-outline-gray-1">
        <div v-for="l in locked" :key="`locked-${l.value}`" class="flex items-center gap-3 border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-2">
          <Checkbox :model-value="true" disabled size="sm" />
          <AppIcon v-if="l.app" :app="l.app" size="sm" />
          <span class="text-base text-ink-gray-8">{{ l.label }}</span>
          <span class="font-mono text-xs text-ink-gray-4">{{ l.value }}</span>
          <span class="ml-auto text-xs text-ink-gray-5">always</span>
        </div>
        <!-- The row is the control (frappe-ui's Checkbox is itself a label, so nesting it in one toggled twice). -->
        <template v-for="(o, i) in shown" :key="o.value">
        <div v-if="o.section && (i === 0 || shown[i - 1].section !== o.section)" class="border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-1 text-xs text-ink-gray-5">{{ o.section }}</div>
        <button type="button" role="checkbox" :aria-checked="has(o.value)" class="flex w-full items-center gap-3 border-b border-outline-gray-1 px-3 py-2 text-left last:border-b-0 hover:bg-surface-gray-1 focus-visible:outline-none focus-visible:bg-surface-gray-1" @click="toggle(o.value, !has(o.value))">
          <Checkbox :model-value="has(o.value)" size="sm" class="pointer-events-none" tabindex="-1" />
          <AppIcon v-if="o.app" :app="o.app" size="sm" />
          <span class="min-w-0 flex-1">
            <span class="block truncate text-base text-ink-gray-8">{{ o.label }}</span>
            <span v-if="o.hint" class="block truncate text-xs text-ink-gray-5">{{ o.hint }}</span>
          </span>
          <span class="font-mono text-xs text-ink-gray-4">{{ o.name || o.value }}</span>
        </button>
        </template>
        <div v-if="!shown.length" class="px-3 py-8 text-center text-p-sm text-ink-gray-5">{{ options.length ? `No app matches "${query}".` : 'Nothing to choose from here.' }}</div>
      </div>
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" data-primary :label="draft.length ? `Use ${draft.length} ${draft.length === 1 ? 'app' : 'apps'}` : 'Use none'" @click="done(close)" />
      </div>
    </template>
  </Dialog>
</template>
