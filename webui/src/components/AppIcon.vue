<script setup>
// One app icon, used everywhere an app is named: the app's own logo from the
// daemon's same-origin cache (an <img>, so an SVG can never run script), else a
// lettered tile on a hue hashed from the name. `kind: card` logos (a GitHub
// social banner standing in for an icon) are cropped to the square on purpose.
import { computed, ref, watch } from 'vue'
import { letterTile } from '../data/catalog'

const props = defineProps({
  app: { type: Object, required: true }, // { name, title, logo?: { url, kind } }
  size: { type: String, default: 'md' }, // xs | sm | md | lg | xl
})
const SIZE = { xs: 'size-5 rounded-[5px] text-[10px]', sm: 'size-7 rounded-md text-xs', md: 'size-10 rounded-lg text-sm', lg: 'size-14 rounded-xl text-lg', xl: 'size-16 rounded-2xl text-xl' }
const broken = ref(false)
watch(() => props.app.logo?.url, () => (broken.value = false))
const tile = computed(() => letterTile(props.app.name || props.app.title))
const src = computed(() => (!broken.value && props.app.logo?.url) || null)
</script>

<template>
  <span class="relative inline-flex shrink-0 select-none items-center justify-center overflow-hidden border border-outline-gray-1 bg-surface-elevation-1" :class="SIZE[size]" :title="app.title || app.name">
    <img v-if="src" :src="src" alt="" class="size-full object-contain p-[12%]" :class="app.logo?.kind === 'card' && 'object-cover p-0'" loading="lazy" @error="broken = true" />
    <span v-else class="flex size-full items-center justify-center font-semibold" :class="tile.cls">{{ tile.letter }}</span>
  </span>
</template>
