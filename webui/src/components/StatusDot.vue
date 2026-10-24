<script setup>
import { computed } from 'vue'
const props = defineProps({ token: { type: String, default: null } })
const cls = computed(
  () =>
    ({
      running: 'bg-surface-green-7',
      RUNNING: 'bg-surface-green-7',
      degraded: 'bg-surface-amber-7',
      BACKOFF: 'bg-surface-amber-7',
      online: 'bg-surface-blue-7',
      STARTING: 'bg-surface-blue-7',
      FATAL: 'bg-surface-red-7',
      offline: 'bg-surface-gray-5',
      EXITED: 'bg-surface-gray-5',
      STOPPED: 'bg-surface-gray-5',
    })[props.token] ?? 'border border-outline-gray-4 bg-transparent',
)
</script>

<template>
  <span class="inline-block size-2 shrink-0 rounded-full" :class="cls" :aria-label="token || 'unknown'" />
</template>
