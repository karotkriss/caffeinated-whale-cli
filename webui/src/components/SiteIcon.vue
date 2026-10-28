<script setup>
// A site's own favicon from the daemon's same-origin cache, else a quiet globe.
import { computed, ref, watch } from 'vue'
import { siteIcon } from '../data/site-icons'

const props = defineProps({ project: { type: String, required: true }, site: { type: String, required: true }, size: { type: String, default: 'md' } })
const SIZE = { sm: 'size-6 rounded-md', md: 'size-8 rounded-lg', lg: 'size-10 rounded-lg' }
const ICON = { sm: 'size-3.5', md: 'size-4', lg: 'size-5' }
const broken = ref(false)
const logo = computed(() => siteIcon(props.project, props.site))
watch(logo, () => (broken.value = false))
</script>

<template>
  <span class="inline-flex shrink-0 select-none items-center justify-center overflow-hidden border border-outline-gray-1 bg-surface-elevation-1" :class="SIZE[size]" :title="logo ? `${site}'s favicon` : 'No favicon read yet'">
    <img v-if="logo && !broken" :src="logo.url" alt="" class="size-full object-contain p-[15%]" loading="lazy" @error="broken = true" />
    <span v-else class="lucide-globe text-ink-gray-4" :class="ICON[size]" aria-hidden="true" />
  </span>
</template>
