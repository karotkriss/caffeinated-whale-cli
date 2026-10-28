<script setup>
// The compact form of CommandLine: one icon button on a settings row that copies
// the equivalent terminal command, named in its tooltip. Same rule, less room.
import { Button, Tooltip, toast } from 'frappe-ui'
const props = defineProps({ command: { type: String, required: true } })
async function copy() {
  try {
    await navigator.clipboard.writeText(props.command)
    toast.success('Command copied', { id: 'copy', description: props.command })
  } catch {
    toast.error('Copy failed', { id: 'copy', description: props.command })
  }
}
</script>

<template>
  <Tooltip :text="`Same as: ${command}`">
    <Button size="sm" variant="ghost" icon="lucide-terminal" :aria-label="`Copy ${command}`" @click="copy" />
  </Tooltip>
</template>
