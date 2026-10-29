<script setup>
// The Arc-style picker body, shared by the quick picker dialog and Settings >
// Appearance: a swatch grid (each swatch is the theme's CSS stand-in in the
// scheme that is painted right now), the light/dark/system choice, and the one
// switch that turns the whole premium layer off. Picking a swatch is feedback-
// tier motion: the ring moves with a 120ms CSS transition and nothing else.
import { Switch } from 'frappe-ui'
import { effects, resolvedScheme, scheme, setScheme } from './effects'
import { THEMES, cssGradient } from './themes'
const SCHEMES = [['light', 'Light', 'lucide-sun'], ['dark', 'Dark', 'lucide-moon'], ['system', 'System', 'lucide-monitor']]
</script>

<template>
  <div class="space-y-4">
    <div>
      <div class="mb-2 text-sm text-ink-gray-6">Gradient</div>
      <div role="radiogroup" aria-label="Gradient theme" class="grid grid-cols-4 gap-2">
        <button v-for="t in THEMES" :key="t.id" type="button" role="radio" :aria-checked="effects.theme === t.id" :title="t.name"
          class="cw-swatch group flex flex-col items-center gap-1 rounded-6 p-1 outline-none focus-visible:focus-ring" @click="effects.theme = t.id">
          <span class="cw-swatch-chip block h-12 w-full rounded-4 border border-outline-gray-2" :class="effects.theme === t.id && 'cw-swatch-on'" :style="{ background: cssGradient(t[resolvedScheme]) }" />
          <span class="text-xs" :class="effects.theme === t.id ? 'text-ink-gray-9' : 'text-ink-gray-5'">{{ t.name }}</span>
        </button>
      </div>
    </div>
    <div>
      <div class="mb-2 text-sm text-ink-gray-6">Scheme</div>
      <div role="radiogroup" aria-label="Colour scheme" class="inline-flex rounded-4 border border-outline-gray-2 bg-surface-gray-1 p-0.5">
        <button v-for="[id, label, icon] in SCHEMES" :key="id" type="button" role="radio" :aria-checked="scheme === id"
          class="flex h-7 items-center gap-1.5 rounded-[5px] px-2.5 text-sm transition-colors duration-100" :class="scheme === id ? 'bg-surface-base text-ink-gray-9 shadow-sm' : 'text-ink-gray-6 hover:text-ink-gray-8'" @click="setScheme(id)">
          <span :class="icon" class="size-3.5" aria-hidden="true" />{{ label }}
        </button>
      </div>
    </div>
    <label class="flex items-center justify-between gap-4 rounded-6 border border-outline-gray-1 px-3 py-2">
      <span><span class="block text-base text-ink-gray-8">Visual effects</span><span class="block text-sm text-ink-gray-5">The moving gradient, glass and reveals. Off keeps the plain desk.{{ effects.reducedMotion ? ' Your system asks for reduced motion, so the gradient holds still.' : '' }}</span></span>
      <Switch v-model="effects.enabled" />
    </label>
  </div>
</template>
