<script setup>
// The editor's own icon (official VS Code and Insiders marks; Cursor's monochrome
// mark), bundled as an image so no inline SVG script can run.
import code from '../assets/editors/code.svg'
import insiders from '../assets/editors/code-insiders.svg'
import cursor from '../assets/editors/cursor.svg'
const ICON = { code, 'code-insiders': insiders, cursor }
defineProps({ editor: { type: String, required: true }, size: { type: String, default: 'size-4' } })
</script>

<template>
  <img v-if="ICON[editor]" :src="ICON[editor]" alt="" :class="size" class="shrink-0" />
</template>
