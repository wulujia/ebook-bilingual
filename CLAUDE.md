# CLAUDE.md

Guidance for AI agents (and humans) working **on this codebase**. For how to *use* the
tool, see [README.md](README.md) — this file is about understanding and changing the code.

## What this is

A single-file Python program, `ebook_bilingual.py`, that turns an EPUB or a text-based PDF
into a paragraph-by-paragraph bilingual (English + 中文) EPUB. Translation is performed by
the local **Claude Code CLI** (`claude -p`), not an HTTP API. All state lives in SQLite, so
every stage is idempotent and resumable.

## Architecture at a glance

Everything is in `ebook_bilingual.py`, laid out top-to-bottom in labelled sections
(grep for the `# ──` banner comments):

| Section | Key functions | Responsibility |
|---|---|---|
| run resolution | `resolve_run`, `slugify` | pick `runs/<slug>/` and set the path globals |
| sqlite helpers | `db_connect`, `db_init`, `meta_get/set` | schema + key/value meta |
| XHTML handling | `iter_translatable`, `visible_text`, `parse_xhtml`, `ensure_zh_style` | shared by extract & inject — they MUST agree on the element set |
| claude worker | `claude_call`, `translate_robust`, `build_translation_prompt` | one headless call; `@@SEG@@` batch protocol + bisect fallback |
| extract (EPUB) | `discover_targets`, `looks_like_matter`, `store_units` | pick spine docs, slice into units |
| extract (PDF) | `reconstruct_paragraphs`, `split_chapters`, `extract_pdf` | text-layer reflow → chapters |
| glossary | `cmd_glossary` | one call → proper-noun map |
| translate | `cmd_translate`, `workable_units` | supervised worker pool |
| status | `cmd_status`, `cmd_status_all` | progress dashboards |
| inject | `cmd_inject` | add `<… class="zh">` siblings (EPUB only) |
| repackage | `cmd_repackage`, `build_bilingual_epub`, `write_epub` | zip the final EPUB |
| QA | `check_l1`, `qa_judge_robust`, `cmd_qa`, `write_qa_report` | L1/L2/L3 checks |
| CLI | `build_argparser`, `cmd_run`, `main` | argument parsing + dispatch |

Pipeline: `extract → glossary → translate → qa → inject → repackage` (chained by
`cmd_run`). EPUB and PDF share the engine from `store_units` onward; only the front end
differs (EPUB injects into existing XHTML; PDF builds a fresh EPUB in `build_bilingual_epub`).

## Data model (`runs/<slug>/cache.sqlite`)

- `meta(k, v)` — title, source path (`epub`), `source_type` (`epub`/`pdf`), tags, glossary…
- `paragraphs(id, file, idx, en, sha, unit_id)` — source units of text; `sha = sha1(en)`.
- `units(id, name, file, state, attempts, error)` — translation work items
  (`pending` / `active` / `done` / `failed`).
- `translations(sha, zh, qa_state, qa_reason)` — keyed by `sha`, so identical English is
  translated once and reused. `qa_state`: `untested` / `l1ok` / `l1flag` / `passed` /
  `failed` / `repaired`.

Idempotency rides on these keys: a re-run only touches rows that are missing, not yet
`done`, or not yet judged.

## Invariants — don't break these

- **English is never mutated.** Bilingual `inject` only *adds* `<… class="zh">` siblings;
  `--single-translate` replaces text in place. The original English must round-trip.
- **`extract` and `inject` must select the identical element set** via `iter_translatable`
  (same `--tags`). A mismatch makes `inject` skip the file ("element mismatch").
- **`is_body_paragraph` / `is_translatable_text` exclude `class="zh"`** so re-running
  `inject` is idempotent (it strips prior zh elements first, then re-adds).
- **The `@@SEG@@` protocol yields exactly one ZH paragraph per EN paragraph.** Keep
  `translate_robust`'s bisect so a miscount can't shift alignment; `qa_judge_robust`
  mirrors it for verdicts.
- **PDF has no source XHTML** — `cmd_inject` returns early for PDF; rendering happens in
  `build_bilingual_epub` at repackage time.

## Conventions

- **Code comments and commit messages: English.**
- **User-facing docs are bilingual paired files** — `*.md` (English) + `*.zh.md` (中文)
  with a language-switch header (README, CHANGELOG). **Update both languages together.**
- **CHANGELOG.md / CHANGELOG.zh.md** get an entry per release that explains the *why* and
  the real-world symptom a fix addresses — match the house style of existing entries.
- Keep it a **single Python file with no third-party deps beyond `lxml`**. `pdftotext`
  (poppler) and the `claude` CLI are external binaries, not Python imports.

## Testing

- `python3 -m unittest test_ebook_bilingual.py` — pure-Python unit tests, no network and no
  `claude`/`pdftotext` binary needed. They cover the deterministic heuristics: PDF reflow,
  front/back-matter detection, L1 checks, QA bisect, status formatting, etc.
- Add tests next to the behaviour you change; the suite is the regression net for the many
  text-heuristic edge cases (each one usually traces back to a specific real-world book).

## Gotchas

- `discover_targets` deliberately keeps `index` out of the default `--skip`: Calibre/
  Z-Library name every chapter `index_split_*.html`, so `index` would swallow the book.
  Real index pages are caught by `looks_like_matter` instead.
- `_META` (the AI-refusal/junk filter) withholds bad output from injection but still records
  it for the QA report — don't widen it to match ordinary prose (history: a bare `抱歉`
  wrongly dropped faithful dialogue).
- `concise_error` exists because `subprocess` exceptions stringify the full command first
  (which embeds the multi-thousand-char system prompt); never `str(e)[:N]` a worker error —
  it would just be the command dump.
