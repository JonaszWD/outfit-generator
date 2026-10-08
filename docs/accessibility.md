# Accessibility checklist

Target: WCAG 2.2 AA for the pages a signed-in person uses. What is checked automatically is in
`tests/test_quality.py`; the rest is a manual pass to repeat before a release.

## Checked by tests (every page, with data in it)

- [x] `<html lang>`, a non-empty `<title>`, exactly one `<h1>`, a `<main id="main">`, a skip link
      to it, and a labelled `<nav>`.
- [x] Every input, select and textarea has a label (`for`/`id`, wrapping `<label>`, or
      `aria-label`).
- [x] Every `<img>` has an `alt`. Item photos use the item's name or its tags
      ("jeans, blue"); outfit tiles use type and color.
- [x] Buttons are `<button type=...>`, never clickable `div`s; no positive `tabindex`.
- [x] Like/Dislike are text buttons (not icon-only); the dislike reason has a label.
- [x] Text/background pairs used by the stylesheet meet 4.5:1 (ink, body and on-primary on their
      surfaces; error text on its white plate). `--muted` is 4.43:1 on `--canvas`, so captions
      use `--body`.
- [x] `:focus-visible` shows a 2 px ink outline; no `outline: none` without a border/shadow
      replacement (form fields thicken their border to 2 px).
- [x] No inline scripts or handlers, so everything also works under the CSP.

## Manual pass (keyboard and screen reader)

- [ ] Tab through `/closet`, `/outfits`, `/planner`, `/settings/preferences` and `/account`: the
      order follows the visual order and every control is reachable and operable with Enter/Space.
- [ ] The delete dialog traps focus, closes with Escape and returns focus to its button.
- [ ] HTMX updates are announced: suggestions and the closet count are `aria-live="polite"`;
      errors use `role="alert"` and notices `role="status"`.
- [ ] Zoom to 200 percent and 320 px width: no horizontal scroll; the planner stacks to one
      column.
- [ ] Reduced motion: the only animation is the tagging spinner, which slows down under
      `prefers-reduced-motion`.
- [ ] Color is never the only signal: availability and archived states are badges with text.

Known limits: the color-strictness control is a native range input with
its value shown in an `<output>`; screen-reader behavior of native sliders varies, so the value
is also stated in the label.
