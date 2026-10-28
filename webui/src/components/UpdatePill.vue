<script setup>
// The "Update available" pill on tiles and rows; core apps read stronger.
import { Badge, Tooltip } from 'frappe-ui'
import { updateText } from '../data/updates'
defineProps({ item: { type: Object, default: null }, size: { type: String, default: 'sm' }, compact: { type: Boolean, default: false } })
</script>

<template>
  <Tooltip v-if="item" :text="`Update available${updateText(item) ? ' · ' + updateText(item) : ''}${item.release?.security ? ' · a release note mentions security' : ''}`">
    <span v-if="compact" class="inline-flex shrink-0 items-center" :class="item.core ? 'text-ink-orange-5' : 'text-ink-blue-5'" :aria-label="`Update available for ${item.app}`"><span class="lucide-arrow-up-circle size-4" aria-hidden="true" /><span v-if="item.release?.security" class="lucide-shield-alert ml-0.5 size-3.5 text-ink-red-5" aria-hidden="true" /></span>
    <Badge v-else :size="size" :theme="item.core ? 'orange' : 'blue'" variant="subtle" :class="item.core ? 'font-medium' : ''" :label="item.release?.security ? 'Update available · security' : 'Update available'">
      <template #prefix><span class="lucide-arrow-up-circle size-3" aria-hidden="true" /></template>
    </Badge>
  </Tooltip>
</template>
