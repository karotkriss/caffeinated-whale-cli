<script setup>
// The one empty state (Pilot's EmptyState): icon tile, title, one line, and the
// cwcli command that fills it when there is one. `filtered` swaps the copy so a
// filtered-out list never claims to be empty.
import CommandLine from './CommandLine.vue'
defineProps({
  icon: { type: String, default: 'lucide-inbox' },
  title: { type: String, required: true },
  description: { type: String, default: '' },
  command: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})
</script>

<template>
  <div class="flex flex-col items-center justify-center rounded-6 border border-dashed border-outline-gray-2 text-center" :class="compact ? 'gap-2 px-4 py-6' : 'gap-3 px-6 py-14'">
    <div class="rounded-full bg-surface-gray-2 text-ink-gray-5" :class="compact ? 'p-2' : 'p-3'"><span :class="[icon, compact ? 'size-4' : 'size-6']" aria-hidden="true" /></div>
    <p class="text-base text-ink-gray-8">{{ title }}</p>
    <p v-if="description" class="max-w-md text-p-sm text-ink-gray-5">{{ description }}</p>
    <div v-if="command" class="mt-1 w-full max-w-sm"><CommandLine :command="command" /></div>
    <slot />
  </div>
</template>
