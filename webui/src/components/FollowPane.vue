<script setup>
// A windowed log view that follows its own growth: it stays pinned to the bottom
// while the reader is there, pauses the moment they scroll up on purpose, and
// offers one control to jump back. Every log view (job event log, bench Logs tab)
// renders through this so none of them can drift back to "scroll it yourself".
//
// Only the rows in and near the viewport are in the DOM (@tanstack/vue-virtual),
// and each rendered row reports its real, wrapped height through measureElement,
// so long lines wrap instead of being cut and a 30,000-line log costs the same
// per refresh as a short one. Rows are only ever appended, and the virtualizer
// compensates the scroll offset when a row above the viewport is measured, so
// nothing moves under a paused reader. The pin re-runs whenever the list's total
// height changes: an append, or a row near the end being measured after a pin.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Button } from 'frappe-ui'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { nextFollowing } from '../data/follow'

const props = defineProps({
  items: { type: Array, required: true },
  // A one-line row; a wrapped or padded row corrects itself once measured.
  estimateHeight: { type: Number, default: 20 },
})
const pane = ref(null)
const body = ref(null)
const following = ref(true)
let lastTop = 0
let observer = null

const virtualizer = useVirtualizer(
  computed(() => ({
    count: props.items.length,
    getScrollElement: () => pane.value,
    estimateSize: () => props.estimateHeight,
    overscan: 20,
  })),
)
const rows = computed(() => virtualizer.value.getVirtualItems())
const totalSize = computed(() => virtualizer.value.getTotalSize())
const measure = (el) => el && virtualizer.value.measureElement(el)

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
  observer.observe(body.value)
  observer.observe(pane.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div class="relative flex flex-col overflow-hidden">
    <div ref="pane" class="min-h-0 flex-1 overflow-auto" @scroll.passive="onScroll">
      <div ref="body" class="relative w-full" :style="{ height: `${totalSize}px` }">
        <div v-for="row in rows" :key="row.key" :ref="measure" :data-index="row.index" class="absolute left-0 top-0 w-full" :style="{ transform: `translateY(${row.start}px)` }">
          <slot :item="items[row.index]" :index="row.index" />
        </div>
      </div>
    </div>
    <Button v-if="!following" class="absolute bottom-3 right-3 shadow-md" variant="solid" size="sm" icon-left="lucide-arrow-down" label="Jump to latest" @click="resume" />
  </div>
</template>
