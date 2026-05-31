# Changelog

## 0.1.1

- **输出文件名改为纯 ASCII**：生成的文件现在是 `<原名> - Bilingual EN-ZH.epub`（加
  `--single-translate` 则为 `<原名> - ZH.epub`），不再用 `中英对照` 后缀；EPUB 的
  `dc:title` 也用同样的英文标签。`--single-translate` 模式现在有独立的 `ZH` 标签，
  不再被错标成「双语」。
- **内容识别前置/后置页**：发现阶段除了看文件名，还会分析每个文档的内容，自动跳过索引、
  版权页、参考书目、致谢、目录等（即使文件名是 Z-Library 那种通用的 `part00XX.html` 也能认出）。
  修复了 1000+ 条目的索引被当作正文翻译的问题。`--no-auto-skip` 可关闭。

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
