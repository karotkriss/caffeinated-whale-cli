<script setup>
// Why a bench is degraded, in the daemon's own words (BenchStatus.reasons, computed
// by core.status from the same facts as `overall`): which check failed (supervisor,
// process or web), the state it saw, when the probe saw it, and the action that
// addresses it. One component for the instance, bench and site pages, so every page
// names the same reasons `cwcli axi status` reports. Several reasons fold into a native
// <details> panel, open by default, so nothing is hidden but the list can be tucked away.
import { computed } from 'vue'
import { Alert, Button } from 'frappe-ui'
import { openAction } from '../data/dialog'
import { ago, reasonShort, reasonsCurrent } from '../data/fleet'
import { can } from '../data/session'

const props = defineProps({
  bench: { type: Object, default: null }, // the live BenchStatus from the fleet
  inst: { type: Object, default: null }, // its instance, for when the probe ran
  ctx: { type: Object, required: true }, // { project, host, bench, benchName }
  site: { type: String, default: null }, // a site page: the site this bench serves
  logs: { type: Function, default: null }, // (process) => show its log, where a page has one
})

const reasons = computed(() => (reasonsCurrent(props.inst) ? props.bench?.reasons || [] : []))
const seen = computed(() => (props.inst?.probed_at ? `seen ${ago(props.inst.probed_at * 1000)}` : ''))
const title = computed(() => {
  const n = reasons.value.length
  const what = n === 1 ? reasonShort(reasons.value[0]) : `${n} checks failed`
  return props.site ? `${props.site} runs on a degraded bench (${props.ctx.benchName}): ${what}` : `${props.ctx.benchName} is degraded: ${what}`
})
// The action for a reason: restart that one program, or bring the bench (back) under
// cwcli's supervisor when there is no supervisord to ask.
const fix = (r) =>
  r.check === 'supervisor' || props.bench?.not_cwcli_supervised
    ? { label: 'Start bench', id: 'bench.start', ctx: props.ctx }
    : { label: `Restart ${r.subject}`, id: 'process.restart', ctx: { ...props.ctx, process: r.subject } }
</script>

<template>
  <Alert v-if="reasons.length" class="cw-reasons" theme="amber" :title="title">
    <template #description>
      <component :is="reasons.length > 1 ? 'details' : 'div'" open>
        <summary v-if="reasons.length > 1" class="cursor-pointer text-sm text-ink-gray-7">{{ reasons.map(reasonShort).join(', ') }}</summary>
        <ul class="mt-1 space-y-1.5">
          <li v-for="r in reasons" :key="r.check + r.subject" class="flex flex-col gap-1 text-sm sm:flex-row sm:items-center sm:gap-x-3">
            <span class="min-w-0 text-ink-gray-7 sm:flex-1">{{ r.text }} <span v-if="seen" class="text-ink-gray-5">{{ seen }}</span></span>
            <span class="flex shrink-0 gap-1 sm:ml-auto">
              <Button v-if="logs && r.check !== 'supervisor'" size="sm" variant="ghost" label="Open its log" @click="logs(r.subject)" />
              <Button v-if="can('operator')" size="sm" variant="subtle" :label="fix(r).label" @click="openAction(fix(r).id, fix(r).ctx)" />
            </span>
          </li>
        </ul>
      </component>
    </template>
  </Alert>
</template>

<style scoped>
/* The Alert lays its description out with items-start, which shrinks the list to its
   widest row and lets each row's buttons drift; the rows need the full width. */
.cw-reasons :deep([data-slot='description']) {
  width: 100%;
}
</style>
