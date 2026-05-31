# Changelog

## 0.1.0

First public release.

- **EPUB → bilingual EPUB**: spine-based content discovery (any EPUB, no per-book
  code changes); multi-tag translation (`<p>`, `<h1>`–`<h6>`, `<li>`, `<blockquote>`,
  configurable via `--tags`); `<sup>`/`<code>` skipped; English never mutated — a styled
  Chinese sibling is appended after each element.
- **Text-based PDF → bilingual EPUB**: `pdftotext` extraction + paragraph reconstruction
  (width-based paragraph detection, cross-page merge, header/footer/page-number removal);
  builds a fresh, spec-compliant EPUB from scratch. Scanned PDFs are rejected (no OCR).
- **Backend**: the local Claude Code CLI (`claude -p`, Claude subscription — no API key).
- **Quality**: auto-built proper-noun glossary; 3-tier QA (deterministic checks →
  semantic back-check → self-repair) against hallucination and omission.
- **Robustness**: all state in SQLite — idempotent and resumable (kill and re-run);
  per-book isolation under `runs/<slug>/`.
- **Options**: `--tags`, `--translation-style`, `--single-translate`, `--skip`,
  `--min-words`, `--concurrency`, `--qa-sample`, `--test-file`, and more.
