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

## What it does

- **EPUB → bilingual EPUB** — English is never mutated; a styled Chinese sibling is
  appended after each element. Translates `<p>`, headings `<h1>`–`<h6>`, `<li>`,
  `<blockquote>` (configurable via `--tags`); skips `<sup>` / `<code>`.
- **Navigation rescue** — if the source EPUB shipped without a table of contents (or only a
  trivial one), a flat one is synthesized from each chapter's `<h1>`/`<h2>` heading, so a
  chapter-split book becomes navigable. A real existing TOC is left as-is.
- **Text PDF → bilingual EPUB** — `pdftotext` + paragraph reconstruction (width-based
  paragraph detection, cross-page merge, soft-hyphen rejoin, de-spaced numbers,
  header/footer/page-number removal, auto `-raw` fallback for glyph-shredded text layers,
  trailing index/back-cover trim), then builds a fresh spec-compliant EPUB.
- **Auto glossary** — fixes one Chinese rendering per recurring proper noun, book-wide, so
  names stay consistent.
- **3-tier QA against hallucination** — deterministic checks (numbers, length, leftover
  English) → independent semantic back-check → self-repair re-translation.
- **Resumable** — all state is in SQLite; kill it any time and re-run to continue.
- **Multi-book** — each book is isolated under `runs/<slug>/`.

## Requirements

- **Python 3.9+** with `lxml` — `pip install -r requirements.txt`
- **Claude Code CLI** on `PATH`, signed in (active subscription) — the translation backend
- **poppler** (`pdftotext`) — only for PDF input
  (macOS `brew install poppler` · Debian/Ubuntu `apt install poppler-utils`)

## Quick start

```bash
python3 ebook_bilingual.py run --epub book.epub      # EPUB     → bilingual EPUB
python3 ebook_bilingual.py run --pdf  book.pdf       # text-PDF → bilingual EPUB
python3 ebook_bilingual.py status                    # progress of every run
```

The result is **`<source name> - Bilingual EN-ZH.epub`** (or `… - ZH.epub` with
`--single-translate`), written **next to the source file**.

`run` is **idempotent and resumable**: if it stops (Ctrl-C, crash, usage limit), run the
same command again and it continues only the unfinished work — every translated paragraph
is cached in `runs/<slug>/cache.sqlite`.

## Pipeline — `run` and its six stages

`run` chains six stages and skips anything already cached, so day to day you only need
`run` and `status`. Each stage is also a standalone subcommand — reach for one to redo or
debug a single step.

| Subcommand | What it does | Run it alone when… |
|---|---|---|
| `extract` | Unzip the source, pick content docs (spine, ≥ `--min-words`, minus front/back matter), slice paragraphs into ~`--unit-words` units. | you changed `--skip` / `--min-words` / `--tags`. |
| `glossary` | One Claude call → proper-noun glossary (`glossary.json`) for book-wide name consistency. | you want to review/edit terms first. |
| `translate` | Pool of `claude -p` workers; per-unit timeout, retry, resume. | resuming or retrying stuck units. |
| `qa` | 3-tier hallucination check → `qa-report.md`. | re-checking after a re-translate. |
| `inject` | Add a styled `<… class="zh">` sibling after each translated element (EPUB only; English untouched). | re-rendering after editing the DB. |
| `repackage` | Re-zip a spec-compliant EPUB (EPUB source) / build one from scratch (PDF source). | rebuilding the output file. |
| `run` | All six, in order, resumably. | the normal case. |
| `status` | Progress of every run (or one with `--book <slug>`). | any time; read-only. |

## Run layout — where state & output live

Each book is isolated under `runs/<slug>/` (slug derived from the source filename):

```
runs/
  active.txt          # last-targeted slug — used when you omit --epub/--pdf/--book
  <slug>/
    cache.sqlite      # all state: paragraphs, units, translations, QA verdicts
    work/             # the unzipped EPUB being edited (or the staged PDF build)
    glossary.json     # proper-noun glossary
    qa-report.md      # paragraphs flagged for human review
```

- **New book:** `--epub` / `--pdf <file>` (derives the slug, marks it active).
- **Existing book:** `--book <slug>`.
- **Neither:** uses the slug in `active.txt`.
- The finished EPUB is written next to the **source file**, not under `runs/`.

## Options

| Flag | Default | Purpose |
|------|---------|---------|
| `--epub` / `--pdf <file>` | — | source file (a run slug is derived from its name) |
| `--book <slug>` | last active | operate on an existing run under `runs/<slug>/` |
| `--model` | `sonnet` | Claude model passed to `claude -p` |
| `--tags` | `p,h1,h2,h3,h4,h5,h6,li,blockquote` | EPUB element tags to translate |
| `--single-translate` | off | output Chinese only, instead of bilingual |
| `--no-toc` | off | don't synthesize a table of contents when the source EPUB lacks one |
| `--translation-style` | `color:#777; font-size:0.92em;` | CSS for the Chinese text |
| `--concurrency` | `10` | parallel `claude -p` workers |
| `--unit-words` | `2500` | words per translation unit |
| `--unit-timeout` | `240` | seconds before a unit's worker times out |
| `--max-attempts` | `5` | retries before a unit is left "stuck" |
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
- **TOC synthesis** — at repackage time, an EPUB whose table of contents is missing or
  trivial (≤ 1 entry) gets an EPUB3 nav doc **and** an EPUB2 NCX built from each chapter's
  first `<h1>`/`<h2>`. Conservative — a real multi-entry TOC is never overwritten; opt out
  with `--no-toc`.

## Resuming & troubleshooting

- **Interrupted?** Re-run the same `run` command — cached work is skipped.
- **`N units stuck (need attention)`** — those units hit `--max-attempts`. Re-run to retry;
  raise `--unit-timeout` for timeouts, or wait out a Claude usage limit. Inspect the cause
  in the `units.error` column of `cache.sqlite`.
- **Rate limited?** `translate` backs off exponentially and keeps going; re-running later
  also works.
- **Translation quality?** Read `runs/<slug>/qa-report.md` — QA-flagged paragraphs are
  listed for review. They still ship (English is always intact).
- **`0 translatable paragraphs`?** Everything was skipped — lower `--min-words`, narrow
  `--skip`, or pass `--no-auto-skip`. A scanned (image-only) book has no text to translate.

## Development

- **Tests:** `python3 -m unittest test_ebook_bilingual.py` (pure Python; no network and no
  `claude`/`pdftotext` needed — they cover the deterministic text heuristics).
- Single-file program (`ebook_bilingual.py`), no build step.
- **Conventions:** code comments and commit messages in English; user-facing docs are
  bilingual paired files (`*.md` + `*.zh.md`) — update both together. See
  [CLAUDE.md](CLAUDE.md) for the architecture map and invariants.

## Limitations

- **Requires Claude Code + a Claude subscription** (see the note above).
- **Scanned PDFs** (no text layer) are rejected — OCR is not included.
- **PDF chapter detection** is best-effort (explicit "Chapter N" / ALL-CAPS titles); if it
  misses, the book still reads fine as one flow.
- **Synthesized TOC needs headings** — the navigation rescue keys off `<h1>`/`<h2>`, so a
  book that is one single file, or whose chapters carry no h1/h2 heading, won't get a
  synthesized TOC (it still reads fine top to bottom). An existing real TOC keeps its
  original titles.
- Tuned for **single-column prose**; heavy multi-column / table layouts may reflow imperfectly.

## License

[MIT](LICENSE)
