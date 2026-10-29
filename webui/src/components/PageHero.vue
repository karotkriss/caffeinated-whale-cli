<script setup>
// Pilot's PageHero, made readable: a plain band, a bordered card with a large
// title, the status as a proper badge beside it, and the page's facts as
// separate labelled items (label above value, full contrast) instead of one
// joined grey line. The two most useful actions sit on the right.
import { Breadcrumbs } from 'frappe-ui'
// With effects on, the detail header card (site, bench, instance) carries the same moving gradient, scrim and text
// treatment as the Instances hero - the hero card IS the gradient surface. The sidebar's Appearance row is the one
// place to change the theme.
import ShaderGradient from '../premium/ShaderGradient.vue'
import { active, motion, stops } from '../premium/effects'
defineProps({
  icon: { type: String, default: 'lucide-box' },
  title: { type: String, required: true },
  crumbs: { type: Array, default: null },
  facts: { type: Array, default: () => [] }, // [{ label, value, mono?, tone? }]
})
</script>

<template>
  <div v-if="crumbs" class="sticky top-0 z-10 flex h-12 items-center border-b border-outline-gray-1 bg-surface-base px-3 sm:px-5"><Breadcrumbs :items="crumbs" /></div>
  <div class="border-b border-outline-gray-1 bg-surface-gray-1">
    <!-- The card spans the content column like the Instances hero (same side padding). -->
    <div class="px-3 py-5 sm:px-5">
      <div class="relative overflow-hidden rounded-7 border border-outline-gray-1 bg-surface-elevation-1 p-5 shadow-sm" :class="active && 'cw-c-hero'">
        <template v-if="active">
          <div class="absolute inset-0" aria-hidden="true"><ShaderGradient :stops="stops" :animate="motion" :scale="0.8" /></div>
          <div class="cw-c-scrim absolute inset-0" aria-hidden="true" />
        </template>
        <div class="relative flex flex-wrap items-center gap-4">
          <span class="flex size-12 shrink-0 items-center justify-center rounded-xl border border-outline-gray-1 text-ink-gray-7" :class="active ? 'cw-glass' : 'bg-surface-gray-1'">
            <slot name="icon"><span :class="icon" class="size-6" aria-hidden="true" /></slot>
          </span>
          <div class="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
            <h1 class="truncate font-semibold leading-8 text-ink-gray-9" :class="active ? 'text-3xl tracking-tight sm:text-4xl' : 'text-2xl'">{{ title }}</h1>
            <slot name="badge" />
          </div>
          <div class="flex items-center gap-2"><slot name="actions" /></div>
        </div>
        <dl v-if="facts.length" class="relative mt-4 flex flex-wrap gap-x-8 gap-y-3 border-t border-outline-gray-1 pt-4">
          <div v-for="f in facts" :key="f.label" class="min-w-[7rem]">
            <dt class="text-xs font-medium uppercase tracking-wide text-ink-gray-5">{{ f.label }}</dt>
            <dd class="mt-1 text-base text-ink-gray-9" :class="[f.mono && 'font-mono text-sm', f.tone === 'muted' && 'text-ink-gray-6']">{{ f.value }}</dd>
          </div>
        </dl>
      </div>
    </div>
  </div>
</template>
