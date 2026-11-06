// One ref lookup for every branch/tag/commit field. The endpoint reads the
// acting machine's repository with its own git credentials. Each Combobox binds
// `@update:query` and `@update:model-value` to one setter: the Combobox throws
// typed text away on blur, and clearing it emits null. That keeps a commit hash,
// or any ref the list lacks, as ordinary free text in a plain string.
import { computed, onUnmounted, ref, toValue, watch } from 'vue'
import { apiBase, getJson } from './api'

const clean = (value) => String(value || '').trim()

export const isRepositoryUrl = (value) => /^(https?:\/\/|git@|ssh:\/\/)[^\s'"]+$/.test(clean(value))

export function repoForAppInput(value, { bench = null, apps = [] } = {}) {
  const specs = clean(value).split(',').map((item) => item.trim()).filter(Boolean)
  if (specs.length !== 1) return null
  const spec = specs[0]
  if (isRepositoryUrl(spec)) return spec
  return bench?.app_remotes?.[spec]?.[0]
    || apps.find((app) => app.name === spec && app.source?.url)?.source.url
    || null
}

const initial = () => ({ key: '', url: '', branches: [], tags: [], loading: false, error: null })

export function useGitRefs(source, commitTyped, { active = true, host = null, delay = 0 } = {}) {
  const refs = ref(initial())
  let timer = null
  let request = 0

  watch(
    () => [!!toValue(active), clean(toValue(source)), clean(toValue(host))],
    ([enabled, url, hostId]) => {
      clearTimeout(timer)
      const current = ++request
      if (!enabled || !isRepositoryUrl(url)) {
        refs.value = initial()
        return
      }

      const key = `${hostId}\n${url}`
      refs.value = { key, url, branches: [], tags: [], loading: true, error: null }
      const load = async () => {
        try {
          const data = await getJson(`${apiBase(hostId || null)}/apps/refs?url=${encodeURIComponent(url)}`)
          if (request !== current) return
          refs.value = {
            key,
            url,
            branches: data.branches || [],
            tags: data.tags || [],
            loading: false,
            error: data.ok ? null : data.error || 'could not list refs',
          }
        } catch (error) {
          if (request !== current) return
          refs.value = { key, url, branches: [], tags: [], loading: false, error: error.message }
        }
      }
      if (delay) timer = setTimeout(load, delay)
      else load()
    },
    { immediate: true },
  )

  onUnmounted(() => clearTimeout(timer))

  const options = computed(() => [
    ...(refs.value.branches.length
      ? [{ group: 'Branches', options: refs.value.branches.map((branch) => ({ label: branch, value: branch })) }]
      : []),
    ...(refs.value.tags.length
      ? [{ group: 'Tags', options: refs.value.tags.map((tag) => ({ label: tag, value: tag })) }]
      : []),
    {
      type: 'custom',
      key: 'typed',
      label: 'Use what I typed',
      description: 'a ref the list does not show',
      condition: ({ query }) => !!query && !refs.value.branches.includes(query) && !refs.value.tags.includes(query),
      onClick: ({ query }) => commitTyped(query),
    },
  ])
  const emptyText = computed(() => (refs.value.error ? `refs not listed: ${refs.value.error}` : 'type a branch, tag or commit'))
  const statusText = computed(() => {
    if (refs.value.loading) return 'listing branches and tags…'
    if (refs.value.branches.length || refs.value.tags.length) return `${refs.value.branches.length} branches, ${refs.value.tags.length} tags from the repository`
    if (refs.value.error) return 'free text; the list could not be read'
    return 'free text; enter a branch, tag or commit'
  })

  return { refs, options, emptyText, statusText }
}
