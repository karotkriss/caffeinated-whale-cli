import preset, { content } from 'frappe-ui/tailwind'

/** @type {import('tailwindcss').Config} */
export default {
  presets: [preset],
  content: [...content, './index.html', './src/**/*.{vue,js,ts}'],
  theme: {
    extend: {
      // Machine words (paths, process keys, tokens, commands) in the design
      // system's own monospace, bundled so it renders the same everywhere.
      fontFamily: { mono: ['"JetBrains Mono Variable"', 'ui-monospace', 'monospace'] },
    },
  },
}
