<script setup>
// frappe-ui's SettingsRow keeps its control column shrink-0 beside the text, which
// is right from the sm breakpoint up and wrong on a phone: there the text column
// collapses to nothing and a control wider than the row (a form with an input and
// a button) leaves the dialog. Below sm every row with a control stacks it under its
// text instead. One place for that rule, so a new row cannot forget it.
import { SettingsRow } from 'frappe-ui'
</script>

<template>
  <SettingsRow :class="$slots.default && 'max-sm:flex-col max-sm:items-stretch max-sm:gap-3'">
    <slot />
  </SettingsRow>
</template>
