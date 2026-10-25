<script setup>
// A scroll container that follows its own growth: it stays pinned to the bottom
// while the reader is there, pauses the moment they scroll up on purpose, and
// offers one control to jump back. Every log view (job event log, bench Logs tab)
// renders through this so none of them can drift back to "scroll it yourself".
//
// Growth is observed with a ResizeObserver on the content wrapper rather than a
// per-consumer "content changed" signal: whatever appends (a new event row, a
// collapsed output block getting longer, a filter change) grows the wrapper, and
// the observer fires after layout and before paint, so the pin never flickers.
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Button } from 'frappe-ui'
import { nextFollowing } from '../data/follow'

const pane = ref(null)
const content = ref(null)
const following = ref(true)
let observer = null
let lastTop = 0

const stick = () => {
  pane.value.scrollTop = pane.value.scrollHeight
}
const onScroll = () => {
  following.value = nextFollowing(following.value, lastTop, pane.value)
  lastTop = pane.value.scrollTop
}
const resume = () => {
  following.value = true
  stick()
}

onMounted(() => {
  // Fires once on observe, so a pane that mounts with content (a finished job) opens at its end.
  observer = new ResizeObserver(() => following.value && stick())
  observer.observe(content.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div class="relative flex flex-col overflow-hidden">
    <div ref="pane" class="min-h-0 flex-1 overflow-auto" @scroll.passive="onScroll">
      <div ref="content"><slot /></div>
    </div>
    <Button v-if="!following" class="absolute bottom-3 right-3 shadow-md" variant="solid" size="sm" icon-left="lucide-arrow-down" label="Jump to latest" @click="resume" />
  </div>
</template>
