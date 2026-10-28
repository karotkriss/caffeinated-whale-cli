<script setup>
// One job state, the way the Jobs list always drew "succeeded": an icon and the word,
// no pill (captain note 33: pills clashed with it). The same component everywhere a
// state is shown, so every state reads alike.
import { computed } from 'vue'

const props = defineProps({ state: { type: String, required: true }, size: { type: String, default: 'sm' } })
const LOOK = {
  queued: ['lucide-clock', 'text-ink-gray-4', 'queued'],
  running: ['lucide-loader-circle animate-spin', 'text-ink-blue-5', 'running'],
  interrupting: ['lucide-loader-circle animate-spin', 'text-ink-amber-5', 'cleaning up'],
  succeeded: ['lucide-circle-check', 'text-ink-green-5', 'succeeded'],
  failed: ['lucide-circle-x', 'text-ink-red-5', 'failed'],
  interrupted: ['lucide-circle-slash', 'text-ink-amber-5', 'interrupted'],
  needs_choice: ['lucide-circle-help', 'text-ink-amber-5', 'needs decision'],
  answered: ['lucide-corner-down-right', 'text-ink-gray-4', 'answered'],
  unknown: ['lucide-circle-dashed', 'text-ink-gray-4', 'unknown'],
}
const look = computed(() => LOOK[props.state] || ['lucide-circle', 'text-ink-gray-4', props.state.replace('_', ' ')])
</script>

<template>
  <span class="inline-flex items-center gap-1.5 text-ink-gray-6" :class="size === 'md' ? 'text-base' : 'text-sm'">
    <span :class="[look[0], look[1], size === 'md' ? 'size-4' : 'size-3.5']" aria-hidden="true" />{{ look[2] }}
  </span>
</template>
