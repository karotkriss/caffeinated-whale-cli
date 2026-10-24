<script setup>
import { computed } from 'vue'
import { Badge } from 'frappe-ui'
import { HEALTH_THEME, PROCESS_THEME } from '../data/fleet'

// Machine words keep their exact spelling (design/readme.md voice rules):
// `running`, `degraded`, `RUNNING`, `FATAL` are never retitled.
const props = defineProps({
  token: { type: String, default: null },
  kind: { type: String, default: 'health' }, // health | process
  size: { type: String, default: 'sm' },
})
const label = computed(() => props.token || 'unknown')
const theme = computed(() => (props.kind === 'process' ? PROCESS_THEME : HEALTH_THEME)[props.token] ?? 'gray')
// "Unknown is a state, not a default": it must not look like `offline`.
const variant = computed(() => (props.token && props.token !== 'unknown' ? 'subtle' : 'outline'))
</script>

<template>
  <Badge :theme="theme" :variant="variant" :size="size" :label="label" />
</template>
