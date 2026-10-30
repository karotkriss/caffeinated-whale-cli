<script setup>
// The App column of every apps table: icon, catalog title, module name, then
// whatever the caller adds (the update pill). One row is one line: the title
// truncates with its full text on hover, the module name gives way first, and
// nothing wraps into the next row or the footnote below the table. On a phone the
// module name is dropped rather than squeezed to a stray fragment.
import { ListCell } from 'frappe-ui/list'
import AppIcon from './AppIcon.vue'

defineProps({
  app: { type: Object, required: true }, // a catalog entry: { name, title, logo }
  name: { type: String, required: true }, // the module name as bench knows it
})
</script>

<template>
  <ListCell class="gap-2 overflow-hidden">
    <AppIcon :app="app" size="sm" />
    <span class="truncate text-base text-ink-gray-8" :title="app.title">{{ app.title }}</span>
    <span v-if="name !== app.title" class="shrink-[8] truncate font-mono text-xs text-ink-gray-4 max-md:hidden" :title="name">{{ name }}</span>
    <slot />
  </ListCell>
</template>
