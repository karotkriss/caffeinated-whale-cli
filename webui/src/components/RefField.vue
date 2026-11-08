<script setup>
// One branch/tag/commit field with the shared autocomplete (data/git-refs.js), for a
// place that needs several at once - New site's app picker has one per picked app, and
// a composable cannot be made per row. Typed text is the value as typed (a commit hash
// needs no list row); clearing it emits ''.
import { Combobox } from 'frappe-ui'
import { useGitRefs } from '../data/git-refs'

const props = defineProps({
  modelValue: { type: String, default: '' },
  source: { type: String, default: null }, // the repository whose refs are listed
  host: { type: String, default: null },
  label: { type: String, default: 'Branch or tag' },
  placeholder: { type: String, default: '' },
  commit: { type: Boolean, default: false }, // a commit is accepted too (a checkout, not a fetch)
  active: { type: Boolean, default: true }, // list the refs only while shown
})
const emit = defineEmits(['update:modelValue'])
const setRef = (query) => emit('update:modelValue', query || '')
const { refs, options, emptyText, statusText } = useGitRefs(() => props.source, setRef, {
  active: () => props.active,
  host: () => props.host,
  commit: props.commit,
})
</script>

<template>
  <div>
    <Combobox :model-value="modelValue" :label="label" :options="options" :loading="refs.loading" :placeholder="placeholder" :empty-text="emptyText" spellcheck="false" @update:model-value="setRef" @update:query="setRef" />
    <p class="mt-1 text-xs text-ink-gray-5">{{ statusText }}</p>
  </div>
</template>
