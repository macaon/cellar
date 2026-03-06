# GTK4 / Libadwaita GUI Notes

A reference for patterns, gotchas, and named tokens that have come up during Cellar development.

---

## CSS

### `@media` queries are not supported

GTK4's CSS engine does not implement `@media` queries. They parse silently without error but have no effect. Do not use them for dark/light mode switching.

### Named colors for dark mode adaptation

Prefer libadwaita's semantic named colors over hardcoded values or manual dark-mode overrides — they resolve to the correct value in both light and dark mode automatically.

| Token | Use |
|---|---|
| `@window_bg_color` | Window/application background |
| `@window_fg_color` | Window foreground (text) |
| `@card_bg_color` | Card surface background |
| `@card_fg_color` | Card foreground (text) |
| `@card_shade_color` | Separator / shade lines **inside** `.card` widgets |
| `@headerbar_bg_color` | Header bar background |
| `@sidebar_bg_color` | Sidebar background |
| `@accent_color` | Active/accent color |

**Lesson learned:** a 1 px separator between info-card cells was hardcoded as `alpha(@window_fg_color, 0.08)`. This looked fine in light mode but did not adapt in dark mode. Replacing it with `@card_shade_color` fixed both modes with a single rule.

### Targeting dark mode explicitly

If a semantic named color is not available and you must override for dark mode only, libadwaita applies the `.dark` CSS class to the top-level window when dark mode is active:

```css
.my-widget {
  background-color: white;
}
.dark .my-widget {
  background-color: #1e1e1e;
}
```

---

## Icons

### Registering custom symbolic icons

Call `Gtk.IconTheme.get_for_display(...).add_search_path(icons_dir())` at startup (see `main.py`). Icons must be placed under `data/icons/hicolor/symbolic/apps/` and named `<name>-symbolic.svg`.

Icons in use:

| File | Name used in code |
|---|---|
| `grid-large-symbolic.svg` | `grid-large-symbolic` |
| `penguin-alt-symbolic.svg` | `penguin-alt-symbolic` (Linux native apps info card) |
