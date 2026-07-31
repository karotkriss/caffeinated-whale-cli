Renders one Lucide glyph inline; use it for every icon in this system rather than pasting SVG.

```jsx
<Icon name="rotate-cw" size={16} />
```

- `name` must be one of `ICON_NAMES` (48 glyphs copied from Lucide into `assets/icons/`).
- Colour comes from `currentColor`, so set `color` on the parent.
- Stroke is 1.5px everywhere; only raise it for a glyph sitting on a filled accent button at 18px+.
