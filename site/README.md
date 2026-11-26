# Truckroll one-pager

`truckroll/` is the page for https://siniaev.dev/truckroll: one self-contained HTML file and `img/`.

- Screenshots come from the live system: `python -I site/capture.py [seconds to wait for a question]` (Playwright).
- Copy rules: no em dashes, curly apostrophes, stop-words glued to the next word with a non-breaking space.
- Preview: open `truckroll/index.html` in a browser.

This is the only copy of the page. Fixes go here; siniaev.dev publishes it with
`scripts/publish_page.py truckroll ~/projects/truckroll/site/truckroll`, and nobody edits
`public/truckroll/` there by hand.
