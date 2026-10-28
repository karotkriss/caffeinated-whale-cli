// Tabs that live in the URL (?tab=), so a refresh, Back, or a shared link lands
// on the same tab. The pages used to read ?tab= once and never write it back.
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

export function useTabQuery(fallback, key = 'tab') {
  const route = useRoute()
  const router = useRouter()
  return computed({
    get: () => String(route.query[key] || fallback),
    set: (value) => {
      const query = { ...route.query }
      if (value === fallback) delete query[key]
      else query[key] = value
      router.replace({ query })
    },
  })
}
