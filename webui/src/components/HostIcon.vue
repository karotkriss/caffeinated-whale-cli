<script setup>
// The forge host's own icon (GitHub's mark, the GitLab instance's favicon),
// resolved and cached by the daemon beside the app logos; a globe when none.
import { computed, ref, watch } from 'vue'
import { hostIcon } from '../data/catalog'
const props = defineProps({ host: { type: String, default: '' }, size: { type: String, default: 'size-3.5' } })
const broken = ref(false)
const src = computed(() => (!broken.value && hostIcon(props.host)) || null)
watch(src, () => (broken.value = false))
</script>

<template>
  <img v-if="src" :src="src" alt="" :class="[size, 'inline-block shrink-0 rounded-[3px] object-contain']" @error="broken = true" />
  <span v-else :class="[host ? 'lucide-globe' : 'lucide-hard-drive', size, 'inline-block shrink-0']" aria-hidden="true" />
</template>
