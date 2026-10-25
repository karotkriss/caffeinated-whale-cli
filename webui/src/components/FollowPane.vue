<script setup>
// A windowed log view that follows its own growth: it stays pinned to the bottom
// while the reader is there, pauses the moment they scroll up on purpose, and
// offers one control to jump back. Every log view (job event log, bench Logs tab)
// renders through this so none of them can drift back to "scroll it yourself".
//
// Only the rows in and near the viewport are in the DOM (@vueuse/core's
// useVirtualList), each at the fixed height `itemHeight` gives it, so a 30,000
// line log costs the same per refresh as a short one and every line stays
// reachable by scrolling. Rows are only ever appended, so nothing above a paused
// reader moves. The pin re-runs after each DOM update that changes `items` and
// whenever the pane itself resizes, never on the window's own range shifts, which
// a slow scroll up would otherwise snap back to the bottom.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Button } from 'frappe-ui'
import { useVirtualList } from '@vueuse/core'
import { nextFollowing } from '../data/follow'

const props = defineProps({
  items: { type: Array, required: true },
  itemHeight: { type: [Number, Function], default: 20 },
})
const following = ref(true)
const { list, containerProps, wrapperProps } = useVirtualList(
  computed(() => props.items),
  { itemHeight: props.itemHeight, overscan: 20 },
)
const pane = containerProps.ref
const rowHeight = (index) => (typeof props.itemHeight === 'number' ? props.itemHeight : props.itemHeight(index))
let observer = null
let lastTop = 0

const stick = () => {
  pane.value.scrollTop = pane.value.scrollHeight
  containerProps.onScroll()
}
const onScroll = () => {
  containerProps.onScroll()
  following.value = nextFollowing(following.value, lastTop, pane.value)
  lastTop = pane.value.scrollTop
}
const resume = () => {
  following.value = true
  stick()
}

watch(() => props.items, () => following.value && stick(), { flush: 'post' })
onMounted(() => {
  // Fires once on observe, so a pane that mounts with content (a finished job) opens at its end.
  observer = new ResizeObserver(() => following.value && stick())
  observer.observe(pane.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div class="relative flex flex-col overflow-hidden">
    <div ref="pane" class="min-h-0 flex-1 overflow-auto" @scroll.passive="onScroll">
      <div v-bind="wrapperProps">
        <div v-for="row in list" :key="row.index" class="overflow-y-clip" :style="{ height: `${rowHeight(row.index)}px` }">
          <slot :item="row.data" :index="row.index" />
        </div>
      </div>
    </div>
    <Button v-if="!following" class="absolute bottom-3 right-3 shadow-md" variant="solid" size="sm" icon-left="lucide-arrow-down" label="Jump to latest" @click="resume" />
  </div>
</template>
