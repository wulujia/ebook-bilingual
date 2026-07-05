# ebook-bilingual

**English** | [简体中文](README.zh.md)

Turn an **EPUB** or a **text-based PDF** into a paragraph-by-paragraph **bilingual
(English + 中文) EPUB** — each English paragraph followed by its Chinese translation.

> ⚠️ **Backend requirement — read this first.** Translation runs through the local
> **[Claude Code](https://claude.com/claude-code) CLI** (`claude -p`) using your
> **Claude subscription**. No API key is read, and there is currently **no API-key
> fallback**. If you don't have Claude Code installed and signed in, this tool will not
> run. If you want OpenAI/DeepL/Gemini API-key backends instead, use
> [bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker).

## Features

- **EPUB → bilingual EPUB** — English is never mutated; a styled Chinese sibling is
  appended after each element. Translates `<p>`, headings `<h1>`–`<h6>`, `<li>`,
  `<blockquote>` (configurable via `--tags`); skips `<sup>` / `<code>`.
- **Text PDF → bilingual EPUB** — `pdftotext` + paragraph reconstruction (width-based
  paragraph detection, cross-page merge, soft-hyphen rejoin, de-spaced numbers,
  header/footer/page-number removal, auto `-raw` fallback for glyph-shredded text layers, and
  trailing index/back-cover trim), then builds a fresh spec-compliant EPUB.
- **Auto glossary** — extracts recurring proper nouns and fixes one Chinese rendering for
  the whole book, so names stay consistent.
- **3-tier QA against hallucination** — deterministic checks (numbers, length, leftover
  English) → independent semantic back-check → self-repair re-translation.
- **Readable output** — normalizes unreadably tiny source font sizes back to the
  reader's `1em` and applies a default `Noto Sans SC` CJK font stack
  (`--base-font`, `--no-font-normalize` to opt out).
- **Self-healing** — all state is in SQLite; kill it any time and re-run to resume.
- **Multi-book** — each book is isolated under
  `~/.local/share/ebook-bilingual/runs/<slug>/` (`$EBOOK_BILINGUAL_RUNS` overrides;
  kept out of Dropbox-synced folders, whose sync engines corrupt live SQLite files).

## Requirements

- **Python 3.9+** with `lxml` — `pip install -r requirements.txt`
- **Claude Code CLI** on `PATH`, signed in (active subscription) — the translation backend
- **poppler** (`pdftotext`) — only for PDF input
  (macOS `brew install poppler` · Debian/Ubuntu `apt install poppler-utils`)

## Usage

```bash
python3 ebook_bilingual.py run --epub book.epub      # EPUB  → bilingual EPUB
python3 ebook_bilingual.py run --pdf  book.pdf       # text-PDF → bilingual EPUB
python3 ebook_bilingual.py status                    # dashboard: progress of every run
python3 ebook_bilingual.py status --book <slug>      # progress of one run
python3 ebook_bilingual.py run --book <slug>         # resume / rebuild an existing run
```

Output is `<source name> - Bilingual EN-ZH.epub` (or `<source name> - ZH.epub` under
`--single-translate`), written next to the source file. Interruptible
and resumable — just re-run `run`. `run` chains `extract → glossary → translate → qa →
inject → repackage`; each is also a standalone subcommand.

### Options

| Flag | Default | Purpose |
|------|---------|---------|
| `--epub` / `--pdf <file>` | — | source file (a run slug is derived from its name) |
| `--book <slug>` | last active | operate on an existing run under `runs/<slug>/` |
| `--tags` | `p,h1,h2,h3,h4,h5,h6,li,blockquote` | EPUB element tags to translate |
| `--single-translate` | off | output Chinese only, instead of bilingual |
| `--translation-style` | `color:#777; font-size:0.92em;` | CSS for the Chinese text |
| `--base-font` | `Noto Sans SC` | whole-book font (CJK fallback stack); `""` keeps book fonts |
| `--no-font-normalize` | off | keep the book's own (possibly tiny) font sizes |
| `--concurrency` | `10` | parallel `claude -p` workers |
| `--unit-words` | `2500` | words per translation unit |
| `--qa-sample` | `0.20` | fraction of paragraphs given the semantic back-check |
| `--min-words` | `150` | min body words for a spine doc to be translated |
| `--skip` | common front/back matter | filename substrings to exclude |
| `--no-auto-skip` | off | keep content-detected front/back matter (don't auto-skip) |
| `--test-file <name>` | — | limit translate/inject to one file (e.g. `Chap1`) |

## How it works

- **Backend** — a slimmed `claude -p` worker (`--tools "" --strict-mcp-config
  --system-prompt`, `MAX_THINKING_TOKENS=0`) keeps per-call overhead ~3.9k tokens and
  turns off extended thinking (pure waste for translation).
- **Batch protocol** — paragraphs are separated by an `@@SEG@@` sentinel (robust against
  the quotes/dashes that break JSON); on a count mismatch it bisects and retries.
- **EPUB injection** — `lxml.etree` appends a same-tag `<… class="zh">` sibling after each
  translatable element and self-injects a `<style>` into each `<head>`. The source bytes
  are otherwise untouched.
- **Navigation rebuild** — `repackage` regenerates `toc.ncx` so translated chapters are
  reachable from the reader's TOC (injection leaves the source navMap, often a Z-Library stub,
  as-is). It parses the book's own contents page when there is one — accurate titles and targets —
  and otherwise falls back to each document's heading. Kindle-style books that pack many
  chapters into one XHTML file are expanded into one anchored entry per chapter
  (`CHAPTER 1 — THE RIVER BANK`); only the navMap and the injected anchors change, page
  list and body text are untouched.

## Limitations

- **Requires Claude Code + a Claude subscription** (see the note above).
- **Scanned PDFs** (no text layer) are rejected — OCR is not included.
- **PDF chapter detection** is best-effort (explicit “Chapter N” / ALL-CAPS titles); if it
  misses, the book still reads fine as one flow.
- Tuned for **single-column prose**; heavy multi-column / table layouts may reflow imperfectly.

## License

[MIT](LICENSE)
