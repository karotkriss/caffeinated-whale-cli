<script setup>
// The quick Arc-style picker: one small dialog over wherever you are. It is
// opened from a sidebar footer entry (sidebar footer, brand mark or hero);
// Settings > Appearance carries the same controls for the settings-minded.
import { Dialog } from 'frappe-ui'
import AppearanceControls from './AppearanceControls.vue'
import { picker } from './picker'
</script>

<template>
  <Dialog v-model:open="picker.open" title="Appearance" icon="lucide-palette" size="sm">
    <AppearanceControls />
  </Dialog>
</template>
