# Changelog

**English** | [简体中文](CHANGELOG.zh.md)

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
