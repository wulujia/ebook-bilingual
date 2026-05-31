#!/usr/bin/env python3
"""
ebook_bilingual.py — Turn an EPUB into a bilingual (English paragraph + Chinese
paragraph) EPUB.

Pipeline (each step is a subcommand; `run` chains them):

    extract   unzip a copy, discover spine docs, pick body <p>, slice into units
    glossary  auto-build a proper-noun glossary (one claude call) for term consistency
    translate supervised pool of `claude -p` workers; per-unit timeout + retry + resume
    qa        L1 deterministic checks (100%) + L2 semantic back-check + L3 self-repair
    inject    insert <p class="zh"> after each translated <p> (English never mutated)
    repackage add .zh CSS, zip into a spec-compliant EPUB
    status    print progress

Multi-book: every book lives under runs/<slug>/ (its own cache.sqlite + work/ +
glossary). Pick the book with --epub (new) or --book (existing); the last one is
remembered as active so plain `status`/`translate` keep working.

Translation backend is the local `claude` CLI in headless mode (subscription auth,
no API key). All state lives in cache.sqlite, so the process is idempotent: kill it
any time and re-run to continue only the unfinished work.

Comments in English; user-facing docs/changelog in Chinese (per project convention).
"""

import argparse
import collections
import json
import hashlib
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
from html.entities import name2codepoint

from lxml import etree

# ── Paths & defaults ────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "runs")
# Per-run paths (runs/<book-slug>/…), assigned by resolve_run() before any command.
RUN_DIR = DB_PATH = WORKDIR = GLOSSARY_PATH = ACTIVE_SLUG = None

XHTML = "http://www.w3.org/1999/xhtml"
P_TAG = f"{{{XHTML}}}p"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

DEFAULTS = dict(
    model="sonnet",
    unit_words=2500,
    concurrency=10,
    unit_timeout=240,
    max_attempts=5,
    qa_sample=0.20,
    min_words=150,
)

# Default spine docs to exclude (front/back matter that shouldn't be bilingual).
DEFAULT_SKIP = ("cover,title-page,titlepage,toc,nav,contents,copyright,colophon,"
                "index,bibliograph,acknowledg")

# Paragraph batch separator. Robust for literary text (quotes/dashes/brackets break
# JSON); a sentinel that never appears in prose does not.
SEG = "@@SEG@@"


# ── per-book run resolution (runs/<slug>/ keeps books isolated) ─────────────
def slugify(path):
    base = os.path.splitext(os.path.basename(path))[0]
    s = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return s[:60] or "book"


def resolve_run(opts):
    """Choose the per-book run directory and set the run-scoped path globals.
    Slug priority: --book > --epub (derived from filename) > last active run.
    Every book lives under runs/<slug>/ so two books never clobber each other."""
    global RUN_DIR, DB_PATH, WORKDIR, GLOSSARY_PATH, ACTIVE_SLUG
    os.makedirs(RUNS_DIR, exist_ok=True)
    active_file = os.path.join(RUNS_DIR, "active.txt")
    src = opts.epub or opts.pdf
    slug = opts.book or (slugify(src) if src else None)
    if not slug and os.path.exists(active_file):
        slug = open(active_file, encoding="utf-8").read().strip()
    if not slug:
        sys.exit("no active book — pass --epub/--pdf <file> or --book <slug>")
    ACTIVE_SLUG = slug
    RUN_DIR = os.path.join(RUNS_DIR, slug)
    os.makedirs(RUN_DIR, exist_ok=True)
    DB_PATH = os.path.join(RUN_DIR, "cache.sqlite")
    WORKDIR = os.path.join(RUN_DIR, "work")
    GLOSSARY_PATH = os.path.join(RUN_DIR, "glossary.json")
    if src or opts.book:              # remember an explicitly-targeted book as active
        with open(active_file, "w", encoding="utf-8") as f:
            f.write(slug)
    return slug


# ── sqlite helpers ──────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS paragraphs (
            id      INTEGER PRIMARY KEY,
            file    TEXT NOT NULL,
            idx     INTEGER NOT NULL,      -- index among body <p> within the file
            en      TEXT NOT NULL,
            sha     TEXT NOT NULL,
            unit_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS units (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            file       TEXT NOT NULL,
            state      TEXT NOT NULL DEFAULT 'pending',  -- pending/active/done/failed
            attempts   INTEGER NOT NULL DEFAULT 0,
            updated_at REAL,
            error      TEXT
        );
        CREATE TABLE IF NOT EXISTS translations (
            sha      TEXT PRIMARY KEY,
            zh       TEXT NOT NULL,
            qa_state TEXT NOT NULL DEFAULT 'untested',  -- untested/passed/flagged/failed/repaired
            qa_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_par_unit ON paragraphs(unit_id);
        CREATE INDEX IF NOT EXISTS ix_par_file ON paragraphs(file, idx);
        """
    )
    conn.commit()


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, str(v)))
    conn.commit()


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# ── XHTML paragraph handling (shared by extract & inject) ───────────────────
def text_of(p):
    """Full visible text of a <p>, including inline <span>/<i>/<a> descendants."""
    return "".join(p.itertext())


def is_body_paragraph(p):
    """True if this <p> is translatable narrative text (not an image, spacer, or an
    already-injected Chinese paragraph). Excluding 'zh' keeps inject idempotent."""
    cls = p.get("class") or ""
    if "illus" in cls or "zh" in cls.split():   # image para / our own ZH para
        return False
    t = text_of(p).strip()
    if not t:                    # <p><br/><br/></p> spacers
        return False
    if not any(ch.isalpha() for ch in t):  # pure punctuation / asterisks
        return False
    return True


def iter_body_paragraphs(root):
    """Yield translatable <p> elements in document order. Deterministic: extract
    and inject must agree on the exact set and ordering."""
    for p in root.iter(P_TAG):
        if is_body_paragraph(p):
            yield p


_SKIP_INLINE = {f"{{{XHTML}}}sup", f"{{{XHTML}}}code"}   # footnote markers / inline code


def visible_text(el):
    """Text of an element, skipping <sup>/<code> subtrees (footnote refs, code)."""
    parts = [el.text or ""]
    for child in el:
        if child.tag in _SKIP_INLINE:
            parts.append(child.tail or "")
        else:
            parts.append(visible_text(child))
            parts.append(child.tail or "")
    return "".join(parts)


def is_translatable_text(el):
    cls = el.get("class") or ""
    if "illus" in cls or "zh" in cls.split():
        return False
    t = visible_text(el).strip()
    return bool(t) and any(c.isalpha() for c in t)


def iter_translatable(root, tags):
    """Yield leaf-level translatable elements among `tags`, in document order. 'Leaf'
    means no descendant is itself translatable — so <blockquote> wrapping <p>s yields
    the <p>s, not the blockquote (avoids double-translation and nesting mess)."""
    tagset = {f"{{{XHTML}}}{t.strip()}" for t in tags if t.strip()}
    for el in root.iter():
        if el.tag not in tagset:
            continue
        if any(d.tag in tagset for d in el.iterdescendants()):
            continue
        if is_translatable_text(el):
            yield el


def ensure_zh_style(root, style):
    """Self-contained styling: put a <style>.zh{…}</style> in each file's <head> so the
    Chinese is styled regardless of the EPUB's own CSS structure."""
    head = root.find(f"{{{XHTML}}}head")
    if head is None:
        return
    for s in head.iter(f"{{{XHTML}}}style"):
        if s.get("class") == "bilingual-zh":
            s.text = f".zh {{ {style} }}"
            return
    s = etree.SubElement(head, f"{{{XHTML}}}style")
    s.set("class", "bilingual-zh")
    s.text = f".zh {{ {style} }}"


_ENTITY_RE = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")
_XML_BUILTIN = {"amp", "lt", "gt", "quot", "apos"}


def normalize_entities(text):
    """Replace named HTML entities (&nbsp; &mdash; …) with numeric refs so a strict
    XML parser accepts the XHTML. Same characters, faithful round-trip."""
    def repl(m):
        name = m.group(1)
        if name in _XML_BUILTIN:
            return m.group(0)
        cp = name2codepoint.get(name)
        return f"&#{cp};" if cp else m.group(0)
    return _ENTITY_RE.sub(repl, text)


def parse_xhtml(path):
    """Parse an XHTML/XML file into an lxml tree, tolerating HTML named entities.
    Returns (root, doctype_string)."""
    with open(path, "rb") as f:
        raw = f.read()
    data = normalize_entities(raw.decode("utf-8")).encode("utf-8")
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as e:
        print(f"  ! recover-mode parse for {os.path.basename(path)}: {e}")
        root = etree.fromstring(data, etree.XMLParser(recover=True))
    doctype = root.getroottree().docinfo.doctype
    return root, doctype


# ── claude CLI worker ───────────────────────────────────────────────────────
def parse_claude_result(stdout):
    """Extract the model's text from a `claude -p --output-format json` payload.
    The payload is a JSON array of events; the final {type:result} holds .result."""
    data = json.loads(stdout)
    if isinstance(data, list):
        results = [e for e in data if isinstance(e, dict) and e.get("type") == "result"]
        if not results:
            raise ValueError("no result event in claude output")
        return results[-1].get("result", "")
    return data.get("result", "")


def strip_fences(text):
    """Defensively remove ```json ... ``` wrappers if the model added them."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def claude_call(system_prompt, user_payload, model, timeout):
    """One headless claude call. Returns the raw model text (.result).
    Slimmed worker: no tools, no MCP, full system-prompt override → ~3.9k token
    overhead instead of ~23.7k. MAX_THINKING_TOKENS=0 disables extended thinking,
    which for translation is pure waste (it 4x'd output tokens and time)."""
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    proc = subprocess.run(
        [
            "claude", "-p",
            "--model", model,
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--system-prompt", system_prompt,
        ],
        input=user_payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:300]}")
    return parse_claude_result(proc.stdout)


def translate_paragraphs(texts, system_prompt, model, timeout):
    """Translate a list of English paragraphs → list of Chinese, length-checked.
    Uses the @@SEG@@ sentinel protocol (robust against quotes/dashes in prose)."""
    payload = (f"\n{SEG}\n").join(texts)
    raw = claude_call(system_prompt, payload, model, timeout)
    blocks = [b.strip() for b in raw.split(SEG)]
    if len(blocks) != len(texts):
        # tolerate a stray empty block from a leading/trailing separator
        nonempty = [b for b in blocks if b]
        if len(nonempty) == len(texts):
            blocks = nonempty
        else:
            raise ValueError(f"segment mismatch: sent {len(texts)}, got {len(blocks)}")
    return blocks


def translate_robust(texts, system_prompt, model, timeout):
    """Translate with divide-and-conquer fallback. On a segment-count mismatch
    (the model occasionally merges two adjacent paragraphs), bisect and translate
    each half; a lone paragraph that still mis-segments is taken from raw output.
    Guarantees exactly one Chinese paragraph per input. Timeouts/errors still
    propagate so the unit is retried later."""
    try:
        return translate_paragraphs(texts, system_prompt, model, timeout)
    except ValueError:
        if len(texts) == 1:
            raw = claude_call(system_prompt, texts[0], model, timeout)
            return [raw.replace(SEG, "").strip()]
        mid = len(texts) // 2
        return (translate_robust(texts[:mid], system_prompt, model, timeout)
                + translate_robust(texts[mid:], system_prompt, model, timeout))


def build_translation_prompt(glossary, title=""):
    """System prompt for translation workers, with glossary injected."""
    book = f' for the book "{title}"' if title else ""
    lines = [
        f"You are a professional English→Simplified-Chinese literary translator{book}.",
        f"You receive several English paragraphs separated by a line containing exactly {SEG}.",
        "Translate EACH paragraph faithfully and fluently into natural Simplified Chinese. "
        "Preserve meaning, tone, names, numbers and dates exactly. Do NOT omit anything. "
        "Do NOT add anything not in the source. Do NOT explain.",
        f"Output the Chinese translations in the SAME order and SAME count, separated by a line "
        f"containing exactly {SEG}. Output ONLY translations and separators — no preamble, no "
        f"numbering, no commentary, no markdown.",
    ]
    if glossary:
        terms = "; ".join(f"{en}=>{zh}" for en, zh in glossary.items())
        lines.append("Glossary for proper nouns (use consistently): " + terms)
    return "\n".join(lines)


# ── extract (discover spine docs generically, slice into units) ─────────────
def find_opf(workdir):
    """Locate the OPF package file via META-INF/container.xml.
    Returns (opf_abspath, opf_dir_rel) where opf_dir_rel is relative to workdir."""
    root, _ = parse_xhtml(os.path.join(workdir, "META-INF", "container.xml"))
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    full = root.find(".//c:rootfile", ns).get("full-path")
    return os.path.join(workdir, full), os.path.dirname(full)


_MATTER_TITLE = re.compile(
    r"^\s*(index|copyright|acknowledge?ments?|bibliography|references|recommended\s+"
    r"(reading|bibliography)|(table\s+of\s+)?contents|about\s+the\s+author|also\s+by|"
    r"by\s+the\s+same\s+author|colophon|credits|permissions|praise\s+for|title\s+page)\b", re.I)
_MATTER_BODY = re.compile(
    r"all\s+rights\s+reserved|library\s+of\s+congress|\bISBN\b|no\s+part\s+of\s+this\s+"
    r"(book|publication)|printed\s+(and\s+bound\s+)?in\b|penguin\s+(books|group)|"
    r"catalogue\s+record\s+for\s+this\s+book", re.I)


def looks_like_matter(root, paras):
    """Content-based front/back-matter detection (complements the filename --skip): catches
    index / copyright / bibliography / acknowledgments / contents pages even when the file is
    named generically (e.g. Z-Library 'part00XX.html')."""
    head = ""
    for el in root.iter():
        if el.tag.split("}")[-1] in ("h1", "h2", "h3", "h4", "h5", "h6"):
            head = visible_text(el).strip()
            if head:
                break
    if not head and paras:
        head = paras[0]
    if head and _MATTER_TITLE.match(head):
        return True
    if not paras:
        return False
    if _MATTER_BODY.search(" ".join(paras[:8])):          # publisher / copyright boilerplate
        return True
    short = [p for p in paras if len(p.split()) <= 7]      # index/TOC: short entries with page nums
    if len(paras) >= 12 and len(short) >= 0.6 * len(paras):
        if sum(any(c.isdigit() for c in p) for p in short) >= 0.5 * len(short):
            return True
    return False


def discover_targets(workdir, opts):
    """Pick which spine documents to translate, generically (any EPUB): the XHTML docs in
    reading order that hold real body text (≥ --min-words), minus filename --skip matches and
    (unless --no-auto-skip) content-detected front/back matter (index/copyright/biblio/etc.).
    Returns (book_title, [workdir-relative paths])."""
    opf_path, opf_dir = find_opf(workdir)
    root, _ = parse_xhtml(opf_path)
    title_el = root.find(f".//{{{DC_NS}}}title")
    title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""

    manifest = {it.get("id"): (it.get("href"), it.get("media-type") or "")
                for it in root.iter(f"{{{OPF_NS}}}item")}
    skip = [s.strip().lower() for s in (opts.skip or "").split(",") if s.strip()]
    tags = [t.strip() for t in opts.tags.split(",") if t.strip()]
    targets, auto = [], []
    for ref in root.iter(f"{{{OPF_NS}}}itemref"):
        href, mtype = manifest.get(ref.get("idref"), (None, ""))
        if not href or "html" not in mtype:
            continue
        rel = os.path.normpath(os.path.join(opf_dir, href))
        full = os.path.join(workdir, rel)
        if not os.path.exists(full) or any(p in rel.lower() for p in skip):
            continue
        try:
            r, _ = parse_xhtml(full)
            paras = [visible_text(el).strip() for el in iter_translatable(r, tags)]
        except Exception:
            continue
        if sum(len(p.split()) for p in paras) < opts.min_words:
            continue
        if not opts.no_auto_skip and looks_like_matter(r, paras):
            auto.append(os.path.basename(rel))
            continue
        targets.append(rel)
    if auto:
        print(f"  · auto-skipped {len(auto)} front/back-matter file(s): " + ", ".join(auto))
    return title, targets


def store_units(conn, sections, unit_words):
    """sections: ordered list of (file_label, [paragraph_text, …]). Slice each file's
    paragraphs into ~unit_words units (units never span a file). Returns totals.
    Shared by the EPUB and PDF front-ends — the engine downstream is format-agnostic."""
    par_id = unit_id = 0
    total_paras = total_words = 0
    for rel, paras in sections:
        paras = [t for t in paras if t.strip()]
        if not paras:
            continue
        chunks, cur, cur_w = [], [], 0
        for t in paras:
            cur.append(t); cur_w += len(t.split())
            if cur_w >= unit_words:
                chunks.append(cur); cur, cur_w = [], 0
        if cur:
            chunks.append(cur)
        idx = 0
        for ci, chunk in enumerate(chunks):
            unit_id += 1
            name = f"{os.path.basename(rel).split('.')[0]}#{ci}"
            conn.execute("INSERT INTO units(id,name,file,state,updated_at) VALUES(?,?,?,?,?)",
                         (unit_id, name, rel, "pending", time.time()))
            for t in chunk:
                par_id += 1
                conn.execute("INSERT INTO paragraphs(id,file,idx,en,sha,unit_id) VALUES(?,?,?,?,?,?)",
                             (par_id, rel, idx, t, sha1(t), unit_id))
                idx += 1
            total_words += sum(len(t.split()) for t in chunk)
        total_paras += len(paras)
    conn.commit()
    return total_paras, unit_id, total_words


def _print_extract_summary(total_paras, total_units, total_words, opts):
    print("✓ extract done")
    print(f"  paragraphs : {total_paras:,}")
    print(f"  words      : {total_words:,}")
    print(f"  units      : {total_units}  (~{opts.unit_words} words each)")


def extract_epub(conn, opts, epub):
    # Fresh extraction of the whole archive (images/CSS present for in-place repackage).
    if os.path.exists(WORKDIR):
        shutil.rmtree(WORKDIR)
    os.makedirs(WORKDIR)
    with zipfile.ZipFile(epub) as z:
        z.extractall(WORKDIR)
    title, files = discover_targets(WORKDIR, opts)
    meta_set(conn, "title", title)
    meta_set(conn, "tags", opts.tags)          # remember which tags this run translates
    tags = [t.strip() for t in opts.tags.split(",") if t.strip()]
    print(f"  book : {title or ACTIVE_SLUG}")
    print(f"  files: {len(files)} content docs (≥{opts.min_words} words, skip='{opts.skip}')")
    print(f"  tags : {opts.tags}")
    sections = []
    for rel in files:
        root, _ = parse_xhtml(os.path.join(WORKDIR, rel))
        sections.append((rel, [visible_text(el).strip() for el in iter_translatable(root, tags)]))
    tp, tu, tw = store_units(conn, sections, opts.unit_words)
    _print_extract_summary(tp, tu, tw, opts)
    print(f"  workdir    : {WORKDIR}")


# ── PDF source (text layer via pdftotext + paragraph reconstruction) ─────────
def pdf_to_text(path, first=None, last=None):
    cmd = ["pdftotext", "-q"]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    cmd += [path, "-"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout


_PAGENUM = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]{1,8})\s*$", re.I)
_CHAPTER = re.compile(r"^(chapter|part|book|section)\s+[\w\d]", re.I)
_TERMINAL = re.compile(r"[.!?][\"'’”)\]]*$")   # sentence-ending punctuation (a real para's last line)


def reconstruct_paragraphs(raw):
    """Reflow pdftotext line output into paragraphs. Key heuristic: a line clearly
    SHORTER than the body width ends a paragraph; near-full lines are wraps and get
    joined (de-hyphenating). Running heads / page numbers are dropped; page breaks do
    NOT split paragraphs. Returns a flat list of paragraph strings."""
    pages = raw.split("\f")
    page_lines = []
    for pg in pages:
        lines = [ln.rstrip() for ln in pg.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            page_lines.append(lines)

    # detect repeated running heads/feet across pages
    heads, feet = collections.Counter(), collections.Counter()
    for lines in page_lines:
        heads[lines[0].strip()] += 1
        feet[lines[-1].strip()] += 1
    thresh = max(3, int(0.3 * len(page_lines)))
    drop = {s for s, c in (heads + feet).items() if c >= thresh and s}

    # flatten to a content-line stream, dropping page numbers + running heads at edges
    stream = []
    for lines in page_lines:
        body = lines[:]
        if body and (_PAGENUM.match(body[0]) or body[0].strip() in drop):
            body = body[1:]
        if body and (_PAGENUM.match(body[-1]) or body[-1].strip() in drop):
            body = body[:-1]
        stream.extend(s.strip() for s in body)

    widths = sorted(len(s) for s in stream if s)
    if not widths:
        return []
    full = widths[int(len(widths) * 0.92)]          # ~typical full-line width

    paras, buf = [], ""
    for s in stream:
        if not s:                                    # blank → paragraph break
            if buf:
                paras.append(buf); buf = ""
            continue
        if not buf:
            buf = s
        elif buf.endswith("-") and len(buf) > 1 and buf[-2].isalpha():
            buf = buf[:-1] + s                       # de-hyphenate across the wrap
        else:
            buf = buf + " " + s
        # paragraph ends on a short line that finishes a sentence, or an explicit heading
        if len(s) < 0.80 * full and (
                _TERMINAL.search(s) or _CHAPTER.match(s) or (s.isupper() and len(s.split()) <= 8)):
            paras.append(buf); buf = ""
    if buf:
        paras.append(buf)
    return [p.strip() for p in paras if p.strip()]


_HEADING_NUM = re.compile(
    r"^(chapter|part|book)\s+(\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.I)


def _is_heading(p):
    """A chapter heading is a SHORT line that is either 'Chapter/Part N' or all-caps.
    Length guard rejects sentences like 'Part of the controversy…'; leading dash/quote
    rejects epigraph attributions ('—EPICTETUS, …') and quoted epigraphs."""
    if len(p) > 60 or len(p.split()) > 9:
        return False
    if p[:1] in "—–-“”\"'‘’":
        return False
    if _HEADING_NUM.match(p):
        return True
    return p.isupper() and any(c.isalpha() for c in p)


def split_chapters(paras):
    """Best-effort chapter split (conservative). Falls back to a single section.
    Returns [(label, title, [paras])]."""
    sections, title, cur = [], "", []
    for p in paras:
        if _is_heading(p):
            if cur:
                sections.append((title, cur)); title, cur = p, []
            elif not title:
                title = p
            elif len(title) < 80:           # consecutive headings → combine ("CHAPTER 1" + title)
                title = title + ": " + p
        else:
            cur.append(p)
    if cur:
        sections.append((title, cur))
    if not sections:
        sections = [("", paras)]
    return [(f"chap-{i:03d}", t, ps) for i, (t, ps) in enumerate(sections, 1)]


def extract_pdf(conn, opts, pdf):
    # require a real text layer (no OCR in this phase)
    if len(pdf_to_text(pdf, 1, 12).strip()) < 200:
        sys.exit("PDF has no usable text layer (looks scanned). OCR is a later phase — "
                 "use a text-based PDF, or install tesseract/ocrmypdf.")
    info = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
    m = re.search(r"^Title:\s*(.+)$", info, re.M)
    title = (m.group(1).strip() if m else "") or os.path.splitext(os.path.basename(pdf))[0]
    meta_set(conn, "title", title)

    chapters = split_chapters(reconstruct_paragraphs(pdf_to_text(pdf)))
    meta_set(conn, "pdf_titles",
             json.dumps({lbl: t for lbl, t, _ in chapters}, ensure_ascii=False))
    if os.path.exists(WORKDIR):
        shutil.rmtree(WORKDIR)
    os.makedirs(WORKDIR)                              # staging dir for the built EPUB

    print(f"  book : {title}")
    print(f"  chapters: {len(chapters)} (heading-detected)")
    tp, tu, tw = store_units(conn, [(lbl, ps) for lbl, _t, ps in chapters], opts.unit_words)
    _print_extract_summary(tp, tu, tw, opts)


def cmd_extract(conn, opts):
    if opts.pdf:
        src, stype = opts.pdf, "pdf"
    elif opts.epub:
        src, stype = opts.epub, "epub"
    else:
        src, stype = meta_get(conn, "epub"), meta_get(conn, "source_type", "epub")
    if not src or not os.path.exists(src):
        sys.exit(f"source not found: {src!r} (pass --epub or --pdf)")
    meta_set(conn, "epub", src)
    meta_set(conn, "source_type", stype)
    conn.execute("DELETE FROM paragraphs")
    conn.execute("DELETE FROM units")
    conn.commit()
    (extract_pdf if stype == "pdf" else extract_epub)(conn, opts, src)


# ── glossary (Phase A) ──────────────────────────────────────────────────────
CAP_SEQ = re.compile(r"\b([A-Z][a-zA-Z'.]+(?:\s+(?:of\s+|the\s+|de\s+)?[A-Z][a-zA-Z'.]+){0,3})\b")
STOPWORDS = {"The", "A", "An", "But", "And", "He", "She", "It", "They", "I",
             "In", "On", "At", "By", "For", "To", "Of", "His", "Her", "That",
             "This", "When", "What", "There", "Then", "If", "As", "So", "Now"}


def cmd_glossary(conn, opts):
    rows = conn.execute("SELECT en FROM paragraphs").fetchall()
    if not rows:
        sys.exit("no paragraphs — run `extract` first")
    counter = collections.Counter()
    for r in rows:
        for m in CAP_SEQ.finditer(r["en"]):
            phrase = m.group(1).strip()
            first = phrase.split()[0]
            # keep multi-word phrases, or single words that aren't sentence-start stopwords
            if " " in phrase or first not in STOPWORDS:
                counter[phrase] += 1
    candidates = [w for w, c in counter.most_common(180) if c >= 3]
    print(f"  {len(candidates)} candidate proper nouns (freq ≥ 3)")

    book = f' for the book "{meta_get(conn, "title", "")}"' if meta_get(conn, "title") else ""
    sp = (
        f"You are building an English→Simplified-Chinese translation glossary{book}. "
        "You will receive a JSON array of candidate phrases extracted by a naive regex. "
        "Return ONLY a JSON object mapping each GENUINE proper noun (person, place, "
        "institution, organization) to its standard Simplified Chinese translation. DROP "
        "non-proper-noun noise. Use established Chinese renderings where they exist. "
        "No markdown, no commentary."
    )
    raw = claude_call(sp, json.dumps(candidates, ensure_ascii=False),
                      opts.model, max(opts.unit_timeout, 360))
    glossary = json.loads(strip_fences(raw))
    with open(GLOSSARY_PATH, "w", encoding="utf-8") as f:
        json.dump(glossary, f, ensure_ascii=False, indent=2, sort_keys=True)
    meta_set(conn, "glossary", json.dumps(glossary, ensure_ascii=False))
    print(f"✓ glossary: {len(glossary)} terms → {GLOSSARY_PATH}")
    for en, zh in list(glossary.items())[:12]:
        print(f"    {en} => {zh}")


def load_glossary(conn):
    if os.path.exists(GLOSSARY_PATH):
        with open(GLOSSARY_PATH, encoding="utf-8") as f:
            return json.load(f)
    g = meta_get(conn, "glossary")
    return json.loads(g) if g else {}


# ── translate (supervised pool) ─────────────────────────────────────────────
def workable_units(conn, max_attempts, test_file=None):
    q = ("SELECT * FROM units WHERE state IN ('pending','failed') AND attempts < ? ")
    args = [max_attempts]
    if test_file:
        name = test_file if test_file.endswith(".html") else test_file + ".html"
        q += "AND (file LIKE ? OR file = ?) "    # EPUB '…/<name>.html' or PDF 'chap-NNN'
        args += [f"%/{name}", test_file]
    q += "ORDER BY id"
    return conn.execute(q, args).fetchall()


def unit_uncached_paragraphs(conn, unit_id):
    """Return [(par_id, en, sha)] of a unit's paragraphs not yet in the cache."""
    rows = conn.execute(
        "SELECT id, en, sha FROM paragraphs WHERE unit_id=? ORDER BY idx", (unit_id,)
    ).fetchall()
    out = []
    for r in rows:
        hit = conn.execute("SELECT 1 FROM translations WHERE sha=?", (r["sha"],)).fetchone()
        if not hit:
            out.append((r["id"], r["en"], r["sha"]))
    return out


def cmd_translate(conn, opts):
    glossary = load_glossary(conn)
    if not glossary:
        print("  ! no glossary yet (terms won't be enforced); run `glossary` first for best results")
    sp = build_translation_prompt(glossary, meta_get(conn, "title", ""))

    backoff = 5
    while True:
        units = workable_units(conn, opts.max_attempts, opts.test_file)
        # Only work on units that still have uncached paragraphs.
        jobs = []
        for u in units:
            paras = unit_uncached_paragraphs(conn, u["id"])
            if not paras:
                conn.execute("UPDATE units SET state='done', updated_at=? WHERE id=?",
                             (time.time(), u["id"]))
                continue
            jobs.append((u, paras))
        conn.commit()
        if not jobs:
            break

        print(f"  dispatching {len(jobs)} units, concurrency={opts.concurrency}")
        made_progress = False
        rate_limited = False
        with ThreadPoolExecutor(max_workers=opts.concurrency) as ex:
            fut2unit = {}
            for u, paras in jobs:
                texts = [p[1] for p in paras]
                fut = ex.submit(translate_robust, texts, sp, opts.model, opts.unit_timeout)
                fut2unit[fut] = (u, paras)
            for fut in as_completed(fut2unit):
                u, paras = fut2unit[fut]
                try:
                    zhs = fut.result()
                    for (par_id, en, sha), zh in zip(paras, zhs):
                        conn.execute(
                            "INSERT OR REPLACE INTO translations(sha, zh, qa_state) "
                            "VALUES(?,?, 'untested')", (sha, zh))
                    conn.execute("UPDATE units SET state='done', attempts=attempts+1, "
                                 "updated_at=?, error=NULL WHERE id=?", (time.time(), u["id"]))
                    conn.commit()
                    made_progress = True
                    print(f"    ✓ {u['name']} ({len(paras)} paras)")
                except Exception as e:
                    msg = str(e)[:300]
                    if re.search(r"rate.?limit|overloaded|429|usage limit", msg, re.I):
                        rate_limited = True
                    conn.execute("UPDATE units SET state='failed', attempts=attempts+1, "
                                 "updated_at=?, error=? WHERE id=?", (time.time(), msg, u["id"]))
                    conn.commit()
                    print(f"    ✗ {u['name']}: {msg}")

        if rate_limited:
            print(f"  rate-limited → backing off {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        elif not made_progress:
            # every remaining job failed without rate-limit → avoid a tight loop
            print("  no progress this pass; remaining units exhausted retries")
            break
        else:
            backoff = 5

    done = conn.execute("SELECT COUNT(*) c FROM units WHERE state='done'").fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM units").fetchone()["c"]
    failed = conn.execute(
        "SELECT COUNT(*) c FROM units WHERE state='failed' AND attempts>=?",
        (opts.max_attempts,)).fetchone()["c"]
    print(f"✓ translate: {done}/{total} units done, {failed} units stuck (need attention)")


# ── status ──────────────────────────────────────────────────────────────────
def cmd_status(conn, opts):
    print(f"book       : {ACTIVE_SLUG}")
    total = conn.execute("SELECT COUNT(*) c FROM units").fetchone()["c"]
    if not total:
        print("no units — run `extract` first"); return
    by = collections.Counter(
        r["state"] for r in conn.execute("SELECT state FROM units").fetchall())
    npar = conn.execute("SELECT COUNT(*) c FROM paragraphs").fetchone()["c"]
    ntr = conn.execute("SELECT COUNT(*) c FROM translations").fetchone()["c"]
    print(f"units      : {dict(by)}  (total {total})")
    print(f"paragraphs : {npar:,}   translated/cached: {ntr:,}")
    qa = collections.Counter(
        r["qa_state"] for r in conn.execute("SELECT qa_state FROM translations").fetchall())
    if qa:
        print(f"qa         : {dict(qa)}")


# ── inject (English never mutated — we only ADD <p class="zh"> siblings) ─────
def cmd_inject(conn, opts):
    if meta_get(conn, "source_type") == "pdf":
        return  # PDF has no source XHTML to inject into; rendering happens in repackage
    tags = [t.strip() for t in meta_get(conn, "tags", "p").split(",") if t.strip()]
    single = opts.single_translate
    files = [r["file"] for r in conn.execute(
        "SELECT DISTINCT file FROM paragraphs ORDER BY file")]
    if opts.test_file:
        name = opts.test_file if opts.test_file.endswith(".html") else opts.test_file + ".html"
        files = [f for f in files if f.endswith("/" + name)]
    ins = skipped = 0
    for rel in files:
        path = os.path.join(WORKDIR, rel)
        if not os.path.exists(path):
            continue
        prows = conn.execute(
            "SELECT idx, sha FROM paragraphs WHERE file=? ORDER BY idx", (rel,)).fetchall()
        if not prows:
            continue
        root, doctype = parse_xhtml(path)
        # idempotent: drop any previously injected zh elements first
        for z in list(root.iter()):
            if "zh" in (z.get("class") or "").split():
                z.getparent().remove(z)
        els = list(iter_translatable(root, tags))
        if len(els) != len(prows):
            print(f"  ! {rel}: element mismatch ({len(els)} vs {len(prows)}) — skipped")
            continue
        for el, r in zip(els, prows):
            tr = conn.execute("SELECT zh FROM translations WHERE sha=?", (r["sha"],)).fetchone()
            zh = tr["zh"] if tr else ""
            # Only drop truly unusable output (empty / AI meta-text junk). QA-failed
            # translations are still included AND listed in the report for human review.
            if not zh.strip() or _META.search(zh):
                skipped += 1
                continue
            if single:                                   # ZH-only: replace English in place
                for c in list(el):
                    el.remove(c)
                el.text = zh
            else:                                          # bilingual: append a same-tag ZH sibling
                new = etree.Element(el.tag)
                new.set("class", "zh")
                new.text = zh
                orig_tail = el.tail
                el.addnext(new)
                el.tail = "\n"
                new.tail = orig_tail
            ins += 1
        if not single:
            ensure_zh_style(root, opts.translation_style)
        out = etree.tostring(root, xml_declaration=True, encoding="utf-8", doctype=doctype)
        with open(path, "wb") as f:
            f.write(out)
    print(f"✓ inject: +{ins} {'zh-only replacements' if single else 'zh elements'}, "
          f"{skipped} skipped (untranslated/failed)")


# ── repackage ────────────────────────────────────────────────────────────────
def write_epub(srcdir, out):
    """Zip a directory into a spec-compliant EPUB: mimetype first and STORED."""
    mimetype = os.path.join(srcdir, "mimetype")
    with zipfile.ZipFile(out, "w") as z:
        if os.path.exists(mimetype):
            z.write(mimetype, "mimetype", compress_type=zipfile.ZIP_STORED)
        for root, _, files in os.walk(srcdir):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, srcdir)
                if rel == "mimetype":
                    continue
                z.write(full, rel, compress_type=zipfile.ZIP_DEFLATED)


_CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_BILINGUAL_CSS = """body { font-family: Georgia, "Songti SC", serif; line-height: 1.6; }
h2 { font-weight: normal; margin: 1.4em 0 0.6em; }
p { margin: 0 0 0.2em; text-indent: 2em; }
.zh { margin-bottom: 0.9em; }
"""

_XHTML_DOC = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
{body}
</body>
</html>
"""

_NAV_DOC = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh">
<head><meta charset="utf-8"/><title>{title}</title></head>
<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>
{items}
</ol></nav></body>
</html>
"""

_OPF_DOC = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{uid}</dc:identifier>
<dc:title>{title} ({edition})</dc:title>
<dc:language>zh</dc:language>
<dc:language>en</dc:language>
<meta property="dcterms:modified">{modified}</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="css" href="styles.css" media-type="text/css"/>
{manifest}
</manifest>
<spine>
{spine}
</spine>
</package>
"""


def edition_label(single_translate):
    """English tag naming the language(s) of a generated edition. Used for the output
    filename and the EPUB dc:title so generated names stay ASCII-only.
    Bilingual EN+ZH by default; ZH-only under --single-translate."""
    return "ZH" if single_translate else "Bilingual EN-ZH"


def build_bilingual_epub(conn, opts, out_path):
    """Render a fresh EPUB3 from cached paragraphs + translations. Used when the source
    is a PDF (no original EPUB to inject into): one XHTML per chapter, alternating
    <p>EN</p><p class="zh">ZH</p> (or zh-only under --single-translate), plus
    mimetype/container/opf/nav/css scaffolding."""
    title = meta_get(conn, "title", "") or ACTIVE_SLUG
    pdf_titles = json.loads(meta_get(conn, "pdf_titles", "{}"))
    files = [r["file"] for r in conn.execute(
        "SELECT DISTINCT file FROM paragraphs ORDER BY file")]

    if os.path.exists(WORKDIR):
        shutil.rmtree(WORKDIR)
    oebps = os.path.join(WORKDIR, "OEBPS")
    os.makedirs(os.path.join(WORKDIR, "META-INF"))
    os.makedirs(oebps)
    with open(os.path.join(WORKDIR, "mimetype"), "w") as f:
        f.write("application/epub+zip")
    with open(os.path.join(WORKDIR, "META-INF", "container.xml"), "w", encoding="utf-8") as f:
        f.write(_CONTAINER_XML)
    with open(os.path.join(oebps, "styles.css"), "w", encoding="utf-8") as f:
        f.write(_BILINGUAL_CSS + f".zh {{ {opts.translation_style} }}\n")

    chapters, total_zh = [], 0
    for i, rel in enumerate(files, 1):
        rows = conn.execute("SELECT en, sha FROM paragraphs WHERE file=? ORDER BY idx",
                            (rel,)).fetchall()
        ch_title = pdf_titles.get(rel) or f"Part {i}"
        body = [f"<h2>{html.escape(ch_title)}</h2>"]
        for r in rows:
            tr = conn.execute("SELECT zh FROM translations WHERE sha=?", (r["sha"],)).fetchone()
            zh = tr["zh"] if tr else ""
            usable = bool(zh.strip()) and not _META.search(zh)
            if usable:
                total_zh += 1
            if opts.single_translate:
                body.append(f"<p>{html.escape(zh if usable else r['en'])}</p>")
            else:
                body.append(f"<p>{html.escape(r['en'])}</p>")
                if usable:
                    body.append(f'<p class="zh">{html.escape(zh)}</p>')
        fname = f"chap-{i:03d}.xhtml"
        with open(os.path.join(oebps, fname), "w", encoding="utf-8") as f:
            f.write(_XHTML_DOC.format(title=html.escape(ch_title), body="\n".join(body)))
        chapters.append((fname, ch_title))

    nav_items = "\n".join(f'<li><a href="{f}">{html.escape(t)}</a></li>' for f, t in chapters)
    with open(os.path.join(oebps, "nav.xhtml"), "w", encoding="utf-8") as f:
        f.write(_NAV_DOC.format(title=html.escape(title), items=nav_items))
    manifest = "\n".join(f'<item id="c{i}" href="{f}" media-type="application/xhtml+xml"/>'
                         for i, (f, _t) in enumerate(chapters))
    spine = "\n".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    uid = "urn:ebook-bilingual:" + sha1(title + ACTIVE_SLUG)[:24]
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(oebps, "content.opf"), "w", encoding="utf-8") as f:
        f.write(_OPF_DOC.format(title=html.escape(title), uid=uid,
                                edition=edition_label(opts.single_translate),
                                manifest=manifest, spine=spine, modified=modified))
    write_epub(WORKDIR, out_path)
    print(f"✓ build: {len(chapters)} chapters, {total_zh} zh paragraphs → {out_path}")


def cmd_repackage(conn, opts):
    epub = meta_get(conn, "epub")
    base = os.path.splitext(os.path.basename(epub))[0]
    out = os.path.join(os.path.dirname(epub),
                       f"{base} - {edition_label(opts.single_translate)}.epub")
    if meta_get(conn, "source_type") == "pdf":
        build_bilingual_epub(conn, opts, out)
        return
    # EPUB source: .zh styling was injected per-file (<style> in each <head>) during inject
    write_epub(WORKDIR, out)
    print(f"✓ repackage → {out}")


# ── QA: L1 deterministic / L2 semantic / L3 self-repair ──────────────────────
_LATIN = re.compile(r"[A-Za-z]")
_HAN = re.compile(r"[一-鿿]")
_META = re.compile(r"作为(一个)?(AI|人工智能|语言模型)|以下是.*翻译|译文如下|抱歉|I (cannot|can't|'m sorry)|```")


def check_l1(en, zh, glossary):
    """Cheap deterministic checks that catch the tell-tale signs of omission,
    hallucination, and non-translation. Returns a list of flag strings."""
    flags = []
    zs = zh.strip()
    if not zs:
        return ["empty"]
    han = len(_HAN.findall(zh))
    latin = len(_LATIN.findall(zh))
    if zs == en.strip():
        flags.append("identical")
    if han and latin > 0.6 * han:          # mostly English left in the "translation"
        flags.append("too_much_latin")
    ewords = len(en.split())
    if ewords >= 12:
        ratio = han / ewords
        if ratio < 0.8:
            flags.append(f"too_short({ratio:.2f})")
        elif ratio > 2.8:
            flags.append(f"too_long({ratio:.2f})")
    en_nums = {n for n in re.findall(r"\d+", en) if len(n) >= 2}
    missing = [n for n in en_nums if n not in zh]
    if missing:
        flags.append("num_missing:" + ",".join(sorted(missing)[:4]))
    if _META.search(zh):
        flags.append("meta_leak")
    return flags


def qa_judge(pairs, model, timeout):
    """Independent semantic back-check of EN↔ZH pairs. Returns list of verdict dicts."""
    blocks = [f"[{i}]\nEN: {en}\nZH: {zh}" for i, (en, zh) in enumerate(pairs)]
    payload = (f"\n{SEG}\n").join(blocks)
    sp = (
        "You are a bilingual QA reviewer for an English→Chinese book translation. The pairs "
        f"are separated by a line containing exactly {SEG}. For each EN/ZH pair, compare the "
        "Chinese against the English source and detect: omission (English content missing from "
        "Chinese), hallucination (Chinese content NOT supported by the English), and serious "
        "mistranslation. Be strict. Return ONLY a JSON array with one object per pair IN ORDER: "
        '{"faithful":1-5,"missing":true|false,"hallucinated":true|false}. No commentary, no markdown.'
    )
    raw = claude_call(sp, payload, model, timeout)
    arr = json.loads(strip_fences(raw))
    if not isinstance(arr, list) or len(arr) != len(pairs):
        raise ValueError(f"qa length mismatch: {len(arr) if isinstance(arr,list) else '?'} vs {len(pairs)}")
    return arr


def _en_for(conn, sha):
    r = conn.execute("SELECT en FROM paragraphs WHERE sha=? LIMIT 1", (sha,)).fetchone()
    return r["en"] if r else ""


def cmd_qa(conn, opts):
    glossary = load_glossary(conn)
    rows = conn.execute("SELECT sha, zh FROM translations").fetchall()
    if not rows:
        sys.exit("no translations — run `translate` first")

    # L1: deterministic, 100%
    l1ok = l1flag = 0
    for r in rows:
        flags = check_l1(_en_for(conn, r["sha"]), r["zh"], glossary)
        conn.execute("UPDATE translations SET qa_state=?, qa_reason=? WHERE sha=?",
                     ("l1flag" if flags else "l1ok", ";".join(flags)[:300] or None, r["sha"]))
        l1flag += bool(flags); l1ok += (not flags)
    conn.commit()
    print(f"  L1: {l1ok} ok, {l1flag} flagged")

    # L2: all flagged + a random sample of the rest
    flagged = [r["sha"] for r in conn.execute("SELECT sha FROM translations WHERE qa_state='l1flag'")]
    oks = [r["sha"] for r in conn.execute("SELECT sha FROM translations WHERE qa_state='l1ok'")]
    k = int(len(oks) * opts.qa_sample)
    sample = random.sample(oks, k) if 0 < k < len(oks) else (oks if opts.qa_sample >= 1 else [])
    targets = flagged + sample
    print(f"  L2: judging {len(targets)} ({len(flagged)} flagged + {len(sample)} sampled @ {opts.qa_sample:.0%})")

    passed = failed = 0
    BATCH = 8
    batches = [targets[i:i + BATCH] for i in range(0, len(targets), BATCH)]
    with ThreadPoolExecutor(max_workers=opts.concurrency) as ex:
        futs = {}
        for b in batches:
            pairs = [(_en_for(conn, s), conn.execute(
                "SELECT zh FROM translations WHERE sha=?", (s,)).fetchone()["zh"]) for s in b]
            futs[ex.submit(qa_judge, pairs, opts.model, opts.unit_timeout)] = b
        for fut in as_completed(futs):
            b = futs[fut]
            try:
                verds = fut.result()
            except Exception as e:
                # don't block the pipeline on a QA hiccup; leave as l1-state
                print(f"    qa batch error: {str(e)[:120]}")
                continue
            for s, v in zip(b, verds):
                fa = v.get("faithful", 5)
                # only fail on low faithfulness, or mid-faithfulness WITH a missing/halluc
                # flag — a 4-5 score is fine even if the judge ticked a minor concern
                bad = fa <= 2 or ((v.get("missing") or v.get("hallucinated")) and fa <= 3)
                conn.execute(
                    "UPDATE translations SET qa_state=?, qa_reason=? WHERE sha=?",
                    ("failed" if bad else "passed",
                     f"faithful={v.get('faithful')},missing={v.get('missing')},halluc={v.get('hallucinated')}",
                     s))
                passed += (not bad); failed += bool(bad)
            conn.commit()
    print(f"  L2: {passed} passed, {failed} failed")

    # L3: self-repair failed paragraphs (re-translate with awareness, re-check L1)
    sp_tr = build_translation_prompt(glossary, meta_get(conn, "title", ""))
    failed_rows = conn.execute("SELECT sha FROM translations WHERE qa_state='failed'").fetchall()
    repaired = 0
    for r in failed_rows:
        sha = r["sha"]; en = _en_for(conn, sha)
        try:
            zh2 = translate_paragraphs([en], sp_tr, opts.model, opts.unit_timeout)[0]
        except Exception:
            continue
        if not check_l1(en, zh2, glossary):
            conn.execute("UPDATE translations SET zh=?, qa_state='repaired', qa_reason='L3 repaired' WHERE sha=?",
                         (zh2, sha))
            repaired += 1
    conn.commit()
    print(f"  L3: repaired {repaired}/{len(failed_rows)}")
    write_qa_report(conn)


def write_qa_report(conn):
    qa = collections.Counter(
        r["qa_state"] for r in conn.execute("SELECT qa_state FROM translations"))
    failed = conn.execute(
        "SELECT sha, qa_reason, zh FROM translations WHERE qa_state='failed'").fetchall()
    title = meta_get(conn, "title", "") or ACTIVE_SLUG
    path = os.path.join(RUN_DIR, "qa-report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# QA 报告 — {title} 中英对照\n\n")
        f.write(f"去重译文段: {sum(qa.values())}\n\n## 状态分布\n\n")
        for k, v in sorted(qa.items()):
            f.write(f"- {k}: {v}\n")
        f.write(f"\n## 未通过 / 待人工复核: {len(failed)}\n\n")
        for r in failed[:300]:
            f.write(f"- `{r['sha'][:8]}` {r['qa_reason']}\n"
                    f"  - EN: {(_en_for(conn, r['sha']) or '')[:160]}\n"
                    f"  - ZH: {r['zh'][:160]}\n")
    print(f"✓ QA report → {path}")


# ── run (chain everything; idempotent / resumable) ───────────────────────────
def cmd_run(conn, opts):
    print(f"book: {ACTIVE_SLUG}")
    if conn.execute("SELECT COUNT(*) c FROM units").fetchone()["c"] == 0 or opts.epub:
        cmd_extract(conn, opts)
    if not load_glossary(conn):
        cmd_glossary(conn, opts)
    cmd_translate(conn, opts)
    cmd_qa(conn, opts)
    cmd_inject(conn, opts)
    cmd_repackage(conn, opts)
    print("✓ run complete")


# ── CLI ─────────────────────────────────────────────────────────────────────
def build_argparser():
    ap = argparse.ArgumentParser(description="EPUB → bilingual (EN+ZH) converter")
    ap.add_argument("--epub", help="path to source EPUB (defines/uses a run)")
    ap.add_argument("--pdf", help="path to source PDF, text-based (defines/uses a run)")
    ap.add_argument("--book", help="run slug under runs/<slug>/ (default: last active run)")
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--unit-words", type=int, default=DEFAULTS["unit_words"])
    ap.add_argument("--concurrency", type=int, default=DEFAULTS["concurrency"])
    ap.add_argument("--unit-timeout", type=int, default=DEFAULTS["unit_timeout"])
    ap.add_argument("--max-attempts", type=int, default=DEFAULTS["max_attempts"])
    ap.add_argument("--qa-sample", type=float, default=DEFAULTS["qa_sample"])
    ap.add_argument("--min-words", type=int, default=DEFAULTS["min_words"],
                    help="min body words for a spine doc to be translated")
    ap.add_argument("--skip", default=DEFAULT_SKIP,
                    help="comma-separated filename substrings to exclude")
    ap.add_argument("--no-auto-skip", action="store_true",
                    help="disable content-based front/back-matter detection")
    ap.add_argument("--tags", default="p,h1,h2,h3,h4,h5,h6,li,blockquote",
                    help="EPUB element tags to translate (comma-separated)")
    ap.add_argument("--translation-style", default="color: #777; font-size: 0.92em;",
                    help="CSS applied to the Chinese text")
    ap.add_argument("--single-translate", action="store_true",
                    help="output Chinese only (replace English) instead of bilingual")
    ap.add_argument("--test-file", help="limit translate/inject to files matching this")
    ap.add_argument("command",
                    choices=["extract", "glossary", "translate", "qa",
                             "inject", "repackage", "run", "status"])
    return ap


def main():
    opts = build_argparser().parse_args()
    resolve_run(opts)
    conn = db_connect()
    db_init(conn)
    dispatch = {
        "extract": cmd_extract, "glossary": cmd_glossary, "translate": cmd_translate,
        "qa": cmd_qa, "inject": cmd_inject, "repackage": cmd_repackage,
        "run": cmd_run, "status": cmd_status,
    }
    dispatch[opts.command](conn, opts)


if __name__ == "__main__":
    main()
