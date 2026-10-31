// One loader per page, so every route that names a page shares it: the router resolves
// it once and the idle prefetch (data/prefetch.js) warms it once.
const loaders = new Map()

export function page(name) {
  if (!loaders.has(name)) loaders.set(name, () => import(`./pages/${name}.vue`))
  return loaders.get(name)
}
