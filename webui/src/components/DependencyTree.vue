<script setup>
// The dependency plan as a tree (core/app_deps.py): each app at the ref that will
// land, its Frappe range against the bench, what cwcli will do with it, and why a
// required app joins. Blockers, source choices, the consent and the typed override
// are rendered here so the Install sheet, New site and New bench read the same way.
import { computed, ref, watch } from 'vue'
import { Alert, Badge, Checkbox, FormControl } from 'frappe-ui'
import { onlyOverridable, planBlockers, requiredNodes, sourcePicks } from '../data/app-plan'

const props = defineProps({
  plan: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  error: { type: String, default: null },
  // Show the consent checkbox (the Install sheet); New site and New bench add the
  // required apps to their picks instead and say so.
  askConsent: { type: Boolean, default: false },
  consentNote: { type: String, default: '' },
})
const consent = defineModel('consent', { type: Boolean, default: false })
const override = defineModel('override', { type: Boolean, default: false })
const depSources = defineModel('depSources', { type: Object, default: () => ({}) })

const nodes = computed(() => props.plan?.nodes || [])
const blockers = computed(() => planBlockers(props.plan))
const choices = computed(() => sourcePicks(props.plan))
const required = computed(() => requiredNodes(props.plan))
const notices = computed(() => (props.plan?.problems || []).filter((p) => p.severity === 'notice' && !(p.options || []).length))
const overridable = computed(() => onlyOverridable(props.plan))
const requested = computed(() => nodes.value.filter((n) => n.requested))
const overrideName = computed(() => requested.value[0]?.app || '')
const typed = ref('')
watch(typed, (t) => (override.value = overridable.value && !!overrideName.value && t.trim() === overrideName.value))
watch(overridable, (ok) => { if (!ok) { typed.value = ''; override.value = false } })
// An open pick takes its preselected source at once, so the plan below it is complete.
watch(choices, (cs) => {
  const open = cs.filter((c) => c.severity === 'choice' && !depSources.value[c.app])
  if (open.length) depSources.value = { ...depSources.value, ...Object.fromEntries(open.map((c) => [c.app, c.default])) }
})

const frappeRange = (n) => (n.ranges || []).find((r) => r.dep === 'frappe')
const otherRanges = (n) => (n.ranges || []).filter((r) => r.dep !== 'frappe' && r.ok === false)
const ACTION_THEME = { 'fetch+install': 'blue', fetch: 'blue', install: 'blue', none: 'gray' }
const ACTION_LABEL = { 'fetch+install': 'fetch + install', fetch: 'fetch', install: 'install', none: 'on the bench' }
const order = computed(() => nodes.value.filter((n) => n.action !== 'none' && n.action !== 'fetch').map((n) => n.app))
const sitesText = computed(() => (props.plan?.sites || []).join(', '))
const readAt = computed(() => requested.value.map((n) => n.ref).filter(Boolean)[0] || '')
</script>

<template>
  <section class="rounded-6 border border-outline-gray-1" aria-live="polite">
    <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-b border-outline-gray-1 px-3 py-2">
      <h3 class="text-sm font-medium text-ink-gray-8">Compatibility and required apps</h3>
      <span v-if="plan" class="text-xs text-ink-gray-5">{{ readAt ? `read at ${readAt} · ` : '' }}bench runs Frappe {{ plan.frappe_version || 'unknown' }}</span>
    </div>
    <div v-if="loading && !plan" class="space-y-1.5 p-3"><div v-for="i in 2" :key="i" class="h-5 animate-pulse rounded bg-surface-gray-1" /></div>
    <p v-else-if="error" class="px-3 py-2.5 text-p-sm text-ink-gray-6">The plan could not be read ({{ error }}). The job still checks it before anything is fetched.</p>
    <template v-else-if="plan">
      <ul class="divide-y divide-outline-gray-1" :class="loading && 'opacity-60 transition-opacity'">
        <li v-for="n in nodes" :key="n.app" class="flex gap-2 px-3 py-2" :class="!n.requested && 'pl-7'">
          <span v-if="!n.requested" class="lucide-corner-down-right mt-0.5 size-4 shrink-0 text-ink-gray-4" aria-hidden="true" />
          <div class="min-w-0 flex-1">
            <div class="flex items-start justify-between gap-2">
              <div class="flex min-w-0 flex-wrap items-baseline gap-x-2">
                <span class="font-mono text-sm text-ink-gray-9">{{ n.app }}</span>
                <span class="text-xs text-ink-gray-5">{{ [n.on_bench && !n.requested ? 'on this bench' : n.ref, n.version].filter(Boolean).join(' · ') }}</span>
              </div>
              <Badge class="shrink-0" size="sm" :theme="ACTION_THEME[n.action]" variant="subtle" :label="ACTION_LABEL[n.action]" />
            </div>
            <div class="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-ink-gray-5">
              <span v-if="frappeRange(n)" class="inline-flex items-center gap-1 font-mono" :class="frappeRange(n).ok === false ? 'text-ink-red-6' : 'text-ink-gray-6'">
                frappe {{ frappeRange(n).spec }}
                <span v-if="frappeRange(n).ok === true" class="lucide-check size-3.5 text-ink-green-5" aria-label="fits this bench" />
                <span v-else-if="frappeRange(n).ok === false" class="lucide-x size-3.5" aria-label="does not fit this bench" />
              </span>
              <span v-else-if="n.readable">no Frappe range declared</span>
              <span v-else>declarations not read</span>
            </div>
            <p v-if="!n.requested || (n.source && !n.on_bench)" class="mt-0.5 break-words text-xs text-ink-gray-5">{{ [n.requested ? '' : `required by ${n.parent}`, n.source && !n.on_bench ? `from ${n.source}` : ''].filter(Boolean).join(' · ') }}</p>
            <p v-for="r in otherRanges(n)" :key="r.dep" class="mt-0.5 font-mono text-xs text-ink-red-6">needs {{ r.dep }} {{ r.spec }}, bench has {{ r.have }}</p>
          </div>
        </li>
      </ul>
      <div v-if="blockers.length || choices.length || notices.length || required.length" class="space-y-2 border-t border-outline-gray-1 p-3">
        <Alert v-for="b in blockers" :key="`${b.code}-${b.app}`" theme="red" :title="b.overridable ? `${b.app} does not fit this bench` : 'This cannot be installed as it is'" :description="[b.message, b.remedy].filter(Boolean).join(' ')" />
        <div v-for="c in choices" :key="`${c.code}-${c.app}`" class="space-y-1.5">
          <Alert :theme="c.severity === 'choice' ? 'amber' : 'gray'" :title="`${c.options.length} apps named ${c.app} are in the catalog`" :description="c.severity === 'choice' ? c.message : 'Pick the one to fetch; the one that fits this bench is preselected.'" />
          <div role="radiogroup" :aria-label="`Source for ${c.app}`" class="space-y-1 pl-1">
            <label v-for="o in c.options" :key="o.value" class="flex cursor-pointer items-start gap-2 text-sm text-ink-gray-8">
              <input type="radio" class="mt-1" :name="`src-${c.app}`" :value="o.value" :checked="(depSources[c.app] || c.default) === o.value" @change="depSources = { ...depSources, [c.app]: o.value }" />
              <span class="min-w-0"><span class="break-all">{{ o.label }}</span> <span class="text-xs" :class="o.ok ? 'text-ink-green-6' : 'text-ink-red-6'">{{ o.ok ? 'fits this bench' : 'its Frappe range does not fit' }}</span></span>
            </label>
          </div>
        </div>
        <p v-for="n in notices" :key="`${n.code}-${n.app}-${n.message}`" class="text-xs text-ink-gray-5">{{ n.message }}</p>
        <template v-if="required.length && (!blockers.length || (overridable && override))">
          <Checkbox v-if="askConsent" v-model="consent" :label="`Also ${required.some((n) => n.action.includes('install')) ? 'fetch and install' : 'fetch'} ${required.map((n) => n.app).join(', ')} first`" />
          <p class="text-p-sm text-ink-gray-6" :class="askConsent && 'pl-6'">{{ consentNote || `Required apps are installed first.` }}<template v-if="order.length"> Order{{ sitesText ? ` on ${sitesText}` : '' }}: {{ order.join(', ') }}.</template></p>
        </template>
        <div v-if="overridable" class="space-y-1.5 rounded-4 border border-outline-red-2 p-2.5">
          <p class="text-p-sm text-ink-gray-7">Install anyway: only when the declared range is wrong. The site's migrate may fail on a Frappe the app was not built for.</p>
          <FormControl v-model="typed" class="max-w-72" :label="`Type ${overrideName} to install anyway`" :placeholder="overrideName" autocomplete="off" spellcheck="false" />
        </div>
      </div>
    </template>
  </section>
</template>
