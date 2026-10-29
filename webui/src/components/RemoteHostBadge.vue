<script setup>
// Which machine an instance lives on: nothing for this machine, a quiet pill with
// the host's name and its link dot for a remote one. Every string a remote host
// sends is rendered as text here, never as markup.
import { computed } from 'vue'
import { Tooltip } from 'frappe-ui'
import { host as hostOf, hostName, linkLook, linkAdvice } from '../data/hosts'

const props = defineProps({ host: { type: String, default: null }, size: { type: String, default: 'sm' } })
const h = computed(() => hostOf(props.host))
const look = computed(() => linkLook(h.value?.state))
</script>

<template>
  <Tooltip v-if="host" :text="linkAdvice(h) || hostName(host)">
    <span class="inline-flex max-w-full items-center gap-1.5 rounded-full border border-outline-gray-2 bg-surface-gray-1 pl-1.5 pr-2 text-ink-gray-7" :class="size === 'md' ? 'h-6 text-sm' : 'h-5 text-xs'">
      <span class="size-1.5 shrink-0 rounded-full" :class="look.dot" aria-hidden="true" />
      <span class="lucide-server size-3 shrink-0 text-ink-gray-5" aria-hidden="true" />
      <span class="truncate">{{ hostName(host) }}</span>
    </span>
  </Tooltip>
</template>
