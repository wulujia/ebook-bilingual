# Changelog

**English** | [简体中文](CHANGELOG.zh.md)

## 0.2.4

- **`run --pdf` re-extracts like `run --epub`** — passing a source file to `run` now (re)extracts
  for both formats; previously only `--epub` triggered it, so `run --pdf book.pdf` silently reused
  stale units. Re-extraction is idempotent (same PDF → same paragraph hashes → all cached), so
  resuming stays cheap.
- **Warn when falling back to the last-active book** — a command with no `--book`/`--epub`/`--pdf`
  uses the slug in `runs/active.txt`, which concurrent runs rewrite; it now prints a warning so a
  multi-book session doesn't silently operate on the wrong book. Pass `--book` to be explicit.

## 0.2.3

- **Calibre / Z-Library EPUBs no longer extract to zero chapters** — these tools split a book
  into per-chapter files named `index_split_<n>.html`, and the default `--skip` list carried an
  `index` token that substring-matched every one of them, so the whole book was filtered out
  (`0 content docs`). Removed the `index` filename token; genuine index pages are still caught by
  content-based front/back-matter detection (`looks_like_matter`). A representative Z-Library
  novel ("The Mountain in the Sea") now extracts 54 chapters (one real index page auto-skipped by
  content) instead of 0.

## 0.2.2

- **Running heads stripped even when the page number varies** — a running head recurs as
  "PHRASE &lt;page&gt;" with a changing number, so the old exact-line frequency check never caught
  it; on books whose sidebars force `-raw` extraction it leaked into ~1 in 6 paragraphs, sometimes
  mid-word (`the teleLinus Torvalds and David Diamond xix phone`). Detection now normalizes the
  page number out before counting and strips the head line anywhere on the page (not just the
  edges), which also lets a word split across the page break rejoin (`tele-`/`phone` →
  `telephone`). Page numbers are validated as canonical roman numerals, so English words made of
  roman letters (`did`, `mill`) aren't mistaken for them.
- **Re-extract prunes orphaned translations** — when a re-extract changes paragraph text (e.g.
  after a cleanup fix), translations for the old text no longer linger in the cache; only rows a
  current paragraph references are kept, so QA and the report don't process text that never ships.

## 0.2.1

- **Fix: short chapters dense with dates no longer skipped as an index** — the content-based
  front/back-matter detector flags a page that is mostly short, page-number-like entries as an
  index/TOC. A *short* narrative chapter thick with notebook quotes, dates and footnote markers
  (e.g. *Leonardo da Vinci*, ch. 19 "Personal Turmoil", ~1.7k words) is 60%+ short digit-bearing
  lines and was misclassified as an index, dropping the whole chapter from translation. The index
  check now also requires the page to lack real prose — three or more long paragraphs that are
  not dense lists of page numbers mark it as a chapter, not an index.

## 0.2.0

Substantially better **text-PDF extraction**, driven by a hard case (Linus Torvalds'
*Just for Fun*) whose text layer mixes clean prose with glyph-shredded sidebars.

- **Auto `pdftotext -raw` fallback** — some PDFs typeset sidebars / boxed text in a font the
  default mode shreds letter-by-letter (`a m i c rokerne l`). Extraction now measures the
  lone-letter fraction and switches to `-raw` (content-stream order) when the default output
  is shredded, recovering pages that were previously untranslatable garbage.
- **Soft-hyphen de-hyphenation** — discretionary hyphens (U+00AD) left at justified line
  breaks are dropped and the word rejoined (`win­ ter` → `winter`); they affected roughly 1 in
  5 paragraphs on a typical justified book.
- **Digit de-spacing** — loose-glyph PDFs that split numbers (`1 9 1 7`, `3 5 0 ,000`) are
  rejoined; only spaces strictly between digits are collapsed, so prose and decimals survive.
- **Trailing index / back-cover trim** — a page-number-dense index with no heading to split on
  (plus the jacket blurb, price and barcode junk after it) is now cut from the end.
- **Stricter PDF heading detection** — single letters, speaker tags (`L:`), bare roman numerals
  (`IV.`) and lone all-caps tech terms (`LINUX`) no longer count as chapter headings, so a
  conversational, all-caps-heavy memoir is no longer shredded into dozens of fake chapters.
- **Content-based front/back-matter skip now covers PDFs** — the index/copyright detector added
  for EPUB in 0.1.1 is applied per section to the PDF front-end. Disable with `--no-auto-skip`.

## 0.1.2

- **Fix: long narrative chapters no longer skipped as copyright pages** — the content-based
  front/back-matter detector flagged any document whose opening paragraphs contained colophon
  boilerplate (e.g. "printed in …") as matter, so a long chapter whose prose merely mentioned
  something being "printed in" a newspaper (e.g. *The Power Broker*, ch. 42, ~11k words) was
  misclassified as a copyright page and auto-skipped from translation. The body-boilerplate
  check is now length-gated — it only fires on short documents (< 300 words), which is all a
  genuine copyright/colophon page ever is.

## 0.1.1

- **ASCII edition labels** — generated files are now `<name> - Bilingual EN-ZH.epub` (or
  `<name> - ZH.epub` with `--single-translate`) instead of the CJK `中英对照` suffix; the EPUB
  `dc:title` uses the same ASCII label. `--single-translate` output now carries its own `ZH`
  label instead of being mislabeled bilingual.
- **Content-based front/back-matter detection** — discovery now inspects each document's
  content (not just its filename) and auto-skips indexes, copyright pages, bibliographies,
  acknowledgments, and tables of contents — even when files are generically named
  (`part00XX.html`, common in Z-Library EPUBs). Fixes a 1000+ entry index being translated as
  body text. Disable with `--no-auto-skip`.

## 0.1.0

First public release.

- **EPUB → bilingual EPUB**: spine-based content discovery (any EPUB, no per-book code
  changes); multi-tag translation (`<p>`, `<h1>`–`<h6>`, `<li>`, `<blockquote>`, configurable
  via `--tags`); `<sup>`/`<code>` skipped; English never mutated — a styled Chinese sibling is
  appended after each element.
- **Text-based PDF → bilingual EPUB**: `pdftotext` extraction + paragraph reconstruction
  (width-based paragraph detection, cross-page merge, header/footer/page-number removal);
  builds a fresh, spec-compliant EPUB from scratch. Scanned PDFs are rejected (no OCR).
- **Backend**: the local Claude Code CLI (`claude -p`, Claude subscription — no API key).
- **Quality**: auto-built proper-noun glossary; 3-tier QA (deterministic checks → semantic
  back-check → self-repair) against hallucination and omission.
- **Robustness**: all state in SQLite — idempotent and resumable (kill and re-run); per-book
  isolation under `runs/<slug>/`.
- **Options**: `--tags`, `--translation-style`, `--single-translate`, `--skip`, `--min-words`,
  `--concurrency`, `--qa-sample`, `--test-file`, and more.
