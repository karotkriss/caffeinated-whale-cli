import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'

const here = dirname(fileURLToPath(import.meta.url))
// The build lands inside the Python package as package data (git-ignored);
// `cwcli ui` serves it from there. See src/caffeinated_whale_cli/commands/serve.py.
// CWCLI_WEBUI_OUTDIR builds the page somewhere else (a dist per checkout, served by
// `cwcli ui` through CWCLI_WEBUI_DIST) without touching the package data.
const outDir = process.env.CWCLI_WEBUI_OUTDIR ? resolve(process.env.CWCLI_WEBUI_OUTDIR) : resolve(here, '../src/caffeinated_whale_cli/webui/dist')

// reka-ui (frappe-ui's headless layer) injects one `<style>` text node per
// scrolling viewport (ScrollArea, Select, Combobox, Viewport) at runtime. The
// daemon serves the SPA under `style-src 'self'` with NO 'unsafe-inline', so
// each of those blocks must be allowed by the sha256 of its exact text. This
// plugin finds every such string in the installed reka-ui, hashes it, and
// writes dist/csp.json, which the daemon merges into its policy - so the
// hashes follow the dependency version instead of living in a Python constant.
function cspHashes() {
  const walk = (dir, out = []) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) walk(path, out)
      else if (name.endsWith('.js')) out.push(path)
    }
    return out
  }
  const styles = new Set()
  for (const file of walk(resolve(here, 'node_modules/reka-ui/dist'))) {
    const source = readFileSync(file, 'utf8')
    // `as: "style"` ... createTextVNode("<css>", -1)
    const re = /as: "style"[\s\S]{0,400}?createTextVNode\("((?:[^"\\]|\\.)*)", -1\)/g
    for (const m of source.matchAll(re)) styles.add(JSON.parse(`"${m[1]}"`))
  }
  if (styles.size === 0) throw new Error('csp: found no reka-ui inline style blocks to hash')
  return {
    name: 'cwcli-csp-hashes',
    closeBundle() {
      const hashes = [...styles].map(
        (css) => `'sha256-${createHash('sha256').update(css, 'utf8').digest('base64')}'`,
      )
      mkdirSync(outDir, { recursive: true })
      writeFileSync(join(outDir, 'csp.json'), JSON.stringify({ 'style-src': hashes }, null, 2) + '\n')
    },
  }
}

export default defineConfig({
  // No Frappe site behind the app: the API is cwcli's own same-origin JSON + SSE.
  plugins: [frappeui({ frappeProxy: false, jinjaBootData: false, buildConfig: false }), vue(), cspHashes()],
  server: {
    port: 5173,
    // Development only: `cwcli ui --foreground --port 8765` serves the API.
    proxy: { '/api': { target: 'http://127.0.0.1:8765' } },
  },
  build: {
    outDir,
    // Keep earlier builds' chunks: a tab opened before a rebuild still lazy-loads
    // the route chunks it was built with (its Job page, say) instead of a 404.
    emptyOutDir: false,
    chunkSizeWarningLimit: 2000,
    // Never inline assets as data: URIs - the CSP has no data: in font-src/script-src.
    assetsInlineLimit: 0,
  },
})
