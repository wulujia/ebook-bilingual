# Changelog

**English** | [简体中文](CHANGELOG.zh.md)

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
