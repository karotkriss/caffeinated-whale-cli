<script setup>
// Developer mode as a calm badge: on (amber, it is a development setting), off
// (quiet). The tooltip names where the value comes from, because Frappe merges
// the bench-wide common_site_config.json with the site's own site_config.json.
import { computed } from 'vue'
import { Badge, Tooltip } from 'frappe-ui'
const props = defineProps({ state: { type: Object, default: null }, size: { type: String, default: 'sm' }, quietOff: { type: Boolean, default: false } })
const SOURCE = { site: "set in the site's site_config.json", common: 'set bench-wide in common_site_config.json', default: "not set anywhere (Frappe's default is off)" }
const text = computed(() => (props.state ? `Developer mode ${props.state.effective ? 'on' : 'off'}: ${SOURCE[props.state.source] || props.state.source}${props.state.verified === false ? ' (from the cache)' : ''}` : 'Developer mode unknown'))
</script>

<template>
  <Tooltip v-if="state && (state.effective || !quietOff)" :text="text">
    <Badge :size="size" :theme="state.effective ? 'amber' : 'gray'" :variant="state.effective ? 'subtle' : 'outline'" :label="state.effective ? (state.source === 'common' ? 'dev mode · bench-wide' : 'dev mode') : 'dev mode off'" />
  </Tooltip>
</template>
