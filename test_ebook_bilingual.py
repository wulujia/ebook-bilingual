"""Unit tests for the format-agnostic pure logic (no network / no Claude needed).

    python3 test_ebook_bilingual.py
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import types
import unittest
from lxml import etree

import ebook_bilingual as E


class TestReconstruct(unittest.TestCase):
    def test_wrap_join_and_sentence_end_split(self):
        raw = (
            "word word word word word word word word word word word word.\n"  # full → wrap
            "short end.\n"                                                     # short + '.' → end
            "word word word word word word word word word word word word.\n"  # full → wrap
            "final short end.\n"                                              # short + '.' → end
        )
        paras = E.reconstruct_paragraphs(raw)
        self.assertEqual(len(paras), 2)
        self.assertTrue(paras[0].endswith("short end."))

    def test_dehyphenate(self):
        raw = (
            "word word word word word word word word word word word word ex-\n"
            "ample.\n"
        )
        paras = E.reconstruct_paragraphs(raw)
        self.assertEqual(len(paras), 1)
        self.assertIn("example.", paras[0])
        self.assertNotIn("ex- ample", paras[0])

    def test_dehyphenate_soft_hyphen(self):
        # PDFs with justified text break words on a SOFT hyphen (U+00AD); pdftotext
        # keeps it (often with a stray space). Unicode says: drop it and rejoin.
        raw = (
            "word word word word word word word word word word word word devel­\n"
            "ops fully into one word here.\n"
        )
        paras = E.reconstruct_paragraphs(raw)
        self.assertEqual(len(paras), 1)
        self.assertIn("develops", paras[0])
        self.assertNotIn("­", paras[0])
        self.assertNotIn("devel ops", paras[0])

    def test_merge_across_page_break(self):
        raw = (
            "word word word word word word word word word word word word abc\n"
            "\f"
            "continued across the page break ending here.\n"
        )
        self.assertEqual(len(E.reconstruct_paragraphs(raw)), 1)

    def test_drops_page_numbers(self):
        raw = (
            "12\nA real content paragraph long enough to count as a paragraph.\n"
            "\f"
            "Another real content paragraph that is also nice and long here.\n13\n"
        )
        paras = E.reconstruct_paragraphs(raw)
        self.assertTrue(all("12" not in p and "13" not in p for p in paras))

    def test_strips_running_header_with_varying_pagenum(self):
        # The running head recurs as "PHRASE <page>" with a changing page number, so an
        # exact-line frequency count never catches it. It must be dropped by the
        # page-number-normalized phrase, wherever it lands in the page's line stream.
        raw = "\f".join(
            f"A genuine sentence of body text number {n} that runs the full width and wraps.\n"
            f"Author Name and Coauthor {n}"          # running header, page number varies
            for n in range(1, 9)                     # 8 pages → over the frequency threshold
        )
        joined = " ".join(E.reconstruct_paragraphs(raw))
        self.assertNotIn("Author Name and Coauthor", joined)
        self.assertIn("body text number 1", joined)   # real content survives

    def test_running_header_between_hyphenated_split_rejoins(self):
        # When the header sits between a word split across a page break (tele- / phone), dropping
        # it must let the halves rejoin into one word.
        pages = [f"filler filler filler filler filler filler filler filler line {n}.\n"
                 f"Running Head Here {n}" for n in range(1, 7)]
        pages.append("filler filler filler filler filler filler filler filler the tele-\n"
                     "Running Head Here 7")
        pages.append("phone rang and the conversation carried on for a good while longer here.")
        joined = " ".join(E.reconstruct_paragraphs("\f".join(pages)))
        self.assertIn("telephone", joined)
        self.assertNotIn("Running Head Here", joined)


class TestHeadKey(unittest.TestCase):
    def test_strips_leading_and_trailing_page_numbers(self):
        self.assertEqual(E._head_key("Author Name and Coauthor 5"), "author name and coauthor")
        self.assertEqual(E._head_key("xiv Just for Fun"), "just for fun")
        self.assertEqual(E._head_key("12 Just for Fun"), "just for fun")

    def test_does_not_strip_english_words_made_of_roman_letters(self):
        # 'did', 'mill', 'mid' are all roman-numeral letters but are NOT valid numerals —
        # they must survive so a real sentence isn't mis-keyed (or mis-dropped) as a header.
        self.assertEqual(E._head_key("did you know that"), "did you know that")
        self.assertEqual(E._head_key("mill of the gods"), "mill of the gods")


class TestTranslatableSelection(unittest.TestCase):
    XML = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><div class="body">'
        '<h2>Title</h2>'
        '<p>Hello <sup>1</sup> world.</p>'
        '<blockquote><p>Quote inside.</p></blockquote>'   # blockquote skipped, inner <p> kept
        '<p class="illus"><img/></p>'                     # image paragraph skipped
        '<p><br/></p>'                                    # empty spacer skipped
        '</div></body></html>'
    )

    def test_leaf_selection(self):
        root = etree.fromstring(self.XML.encode())
        els = list(E.iter_translatable(root, ["p", "h2", "blockquote"]))
        self.assertEqual([e.tag.split("}")[-1] for e in els], ["h2", "p", "p"])

    def test_visible_text_skips_sup(self):
        root = etree.fromstring(self.XML.encode())
        p = next(e for e in E.iter_translatable(root, ["p"]) if "Hello" in E.visible_text(e))
        self.assertIn("Hello", E.visible_text(p))
        self.assertIn("world", E.visible_text(p))
        self.assertNotIn("1", E.visible_text(p))   # superscript footnote marker dropped


class TestHelpers(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(E.slugify("/path/My Book (2020).epub"), "my-book-2020")

    def test_edition_label(self):
        # ASCII-only language tags for generated filenames / titles (no CJK)
        self.assertEqual(E.edition_label(False), "Bilingual EN-ZH")
        self.assertEqual(E.edition_label(True), "ZH")
        self.assertTrue(E.edition_label(False).isascii())

    def test_check_l1(self):
        self.assertIn("empty", E.check_l1("hello", "", {}))
        self.assertTrue(any("num_missing" in f for f in
                            E.check_l1("In 1888 he was born.", "他出生了。", {})))
        self.assertEqual(E.check_l1("The cat sat on the mat quietly.",
                                    "猫安静地坐在垫子上。", {}), [])


class TestParseJsonObject(unittest.TestCase):
    """The glossary reply is expected to be a single JSON object, but LLM output is flaky.
    parse_json_object tolerates fences and stray prose around the object; a truly unparseable
    reply raises so the caller can retry or degrade."""

    def test_clean_object(self):
        self.assertEqual(E.parse_json_object('{"Toad": "蟾蜍"}'), {"Toad": "蟾蜍"})

    def test_fenced_object(self):
        self.assertEqual(E.parse_json_object('```json\n{"Rat": "河鼠"}\n```'),
                         {"Rat": "河鼠"})

    def test_slices_out_surrounding_prose(self):
        # model tacks a preamble/epilogue around the object — take the outermost {...}
        raw = 'Here is the glossary:\n{"Mole": "鼹鼠"}\nHope this helps!'
        self.assertEqual(E.parse_json_object(raw), {"Mole": "鼹鼠"})

    def test_unparseable_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            E.parse_json_object("no json here at all")


class TestRunsMigration(unittest.TestCase):
    """Run state moved out of the repo in 0.4.0 (sync engines corrupt live SQLite WAL
    files). migrate_legacy_runs must move book dirs, drop redundant interim symlinks,
    never overwrite destination state, and remove the legacy dir only when emptied."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.legacy = os.path.join(self._tmp.name, "legacy")
        self.dest = os.path.join(self._tmp.name, "dest")
        os.makedirs(self.legacy)
        os.makedirs(self.dest)

    def tearDown(self):
        self._tmp.cleanup()

    def _book(self, root, slug, marker="x"):
        d = os.path.join(root, slug)
        os.makedirs(d)
        with open(os.path.join(d, "cache.sqlite"), "w") as f:
            f.write(marker)
        return d

    def test_moves_books_and_removes_emptied_legacy(self):
        self._book(self.legacy, "book-a")
        with open(os.path.join(self.legacy, "active.txt"), "w") as f:
            f.write("book-a")
        with contextlib.redirect_stdout(io.StringIO()):
            n = E.migrate_legacy_runs(self.legacy, self.dest)
        self.assertEqual(n, 2)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "book-a", "cache.sqlite")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "active.txt")))
        self.assertFalse(os.path.exists(self.legacy))     # emptied → removed

    def test_never_overwrites_destination_dir(self):
        self._book(self.legacy, "book-a", marker="OLD")
        self._book(self.dest, "book-a", marker="NEW")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            n = E.migrate_legacy_runs(self.legacy, self.dest)
        self.assertEqual(n, 0)
        with open(os.path.join(self.dest, "book-a", "cache.sqlite")) as f:
            self.assertEqual(f.read(), "NEW")             # destination untouched
        self.assertTrue(os.path.isdir(os.path.join(self.legacy, "book-a")))  # kept, not lost
        self.assertIn("book-a", err.getvalue())           # and reported
        self.assertTrue(os.path.isdir(self.legacy))       # legacy kept while non-empty

    def test_drops_symlinks_that_point_into_destination(self):
        real = self._book(self.dest, "book-b")
        os.symlink(real, os.path.join(self.legacy, "book-b"))
        E.migrate_legacy_runs(self.legacy, self.dest)
        self.assertFalse(os.path.lexists(os.path.join(self.legacy, "book-b")))
        self.assertTrue(os.path.isdir(real))              # target untouched
        self.assertFalse(os.path.exists(self.legacy))     # emptied → removed

    def test_stale_duplicate_file_is_dropped(self):
        for root, txt in ((self.legacy, "stale"), (self.dest, "fresh")):
            with open(os.path.join(root, "active.txt"), "w") as f:
                f.write(txt)
        E.migrate_legacy_runs(self.legacy, self.dest)
        with open(os.path.join(self.dest, "active.txt")) as f:
            self.assertEqual(f.read(), "fresh")
        self.assertFalse(os.path.exists(self.legacy))

    def test_noop_when_no_legacy_dir(self):
        self.assertEqual(E.migrate_legacy_runs(os.path.join(self._tmp.name, "nope"),
                                               self.dest), 0)


class TestGlossaryContexts(unittest.TestCase):
    """The CAP_SEQ regex can't tell a name from a capitalized exclamation — 'O, Joy!'
    once pinned Joy=>乔伊 as a character. glossary_contexts attaches one sample usage
    per candidate so the model can judge by context."""

    def test_window_around_first_occurrence(self):
        paras = ["Nothing here.",
                 "It was the Mole who first noticed the river that fine morning in spring."]
        ctx = E.glossary_contexts(paras, ["Mole"], width=12)
        self.assertEqual(ctx["Mole"], "It was the Mole who first n…")

    def test_no_ellipsis_at_text_edges(self):
        ctx = E.glossary_contexts(["Toad laughed."], ["Toad"], width=40)
        self.assertEqual(ctx["Toad"], "Toad laughed.")

    def test_exclamation_context_is_visible(self):
        # exactly the false-positive case: the sample must expose 'O, Joy!' as noise
        ctx = E.glossary_contexts(['He cried "O, Joy! O, bliss!" and danced.'], ["Joy"])
        self.assertIn("O, Joy!", ctx["Joy"])

    def test_absent_candidate_maps_to_empty(self):
        self.assertEqual(E.glossary_contexts(["some text"], ["Ghost"]), {"Ghost": ""})


class TestGlossaryResilience(unittest.TestCase):
    """cmd_glossary must survive flaky model replies: retry the call up to 3 times, and if
    every attempt yields unparseable output, degrade to an EMPTY glossary rather than abort —
    the glossary is a consistency aid, and one bad reply must not kill a whole-book run.
    The fake claude_call stands in only for the model; the retry/degrade loop under test is real."""

    def setUp(self):
        self._real_call = E.claude_call
        self._real_gpath = E.GLOSSARY_PATH
        self._tmp = tempfile.TemporaryDirectory()
        E.GLOSSARY_PATH = os.path.join(self._tmp.name, "glossary.json")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        E.db_init(self.conn)
        # one repeated proper noun so the candidate list is non-empty
        for i in range(3):
            self.conn.execute(
                "INSERT INTO paragraphs(file, idx, en, sha, unit_id) VALUES(?,?,?,?,1)",
                ("c1", i, "Toad walked out. Toad laughed. Toad of Toad Hall.", f"sha{i}"))
        self.conn.commit()
        self.opts = types.SimpleNamespace(model="sonnet", unit_timeout=1)

    def tearDown(self):
        E.claude_call = self._real_call
        E.GLOSSARY_PATH = self._real_gpath
        self._tmp.cleanup()

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            E.cmd_glossary(self.conn, self.opts)
        return out.getvalue(), err.getvalue()

    def test_all_attempts_bad_degrades_to_empty_glossary(self):
        calls = []
        E.claude_call = lambda *a, **k: calls.append(1) or "utter { garbage : nonsense"
        _, err = self._run()                              # must NOT raise
        self.assertEqual(len(calls), 3)                   # retried exactly 3 times
        self.assertIn("empty glossary", err)
        with open(E.GLOSSARY_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})            # empty glossary persisted
        self.assertEqual(E.meta_get(self.conn, "glossary"), "{}")

    def test_recovers_on_third_attempt(self):
        replies = iter(["not json", '["array", "not object"]',
                        'Here you go:\n{"Toad": "蟾蜍"}\nEnjoy!'])
        E.claude_call = lambda *a, **k: next(replies)
        self._run()
        self.assertEqual(json.loads(E.meta_get(self.conn, "glossary")), {"Toad": "蟾蜍"})
        with open(E.GLOSSARY_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"Toad": "蟾蜍"})


class TestConciseError(unittest.TestCase):
    """Worker subprocess failures must surface the REAL cause. subprocess.TimeoutExpired and
    CalledProcessError stringify with the full command FIRST, and our `claude -p` command embeds
    a multi-thousand-char system prompt — so a blind str(e)[:N] is always just the command dump
    and drops the actual reason. concise_error pulls out the structured fields instead."""

    HUGE = "SYSTEM-PROMPT-" + "X" * 5000          # stands in for the embedded system prompt
    CMD = ["claude", "-p", "--system-prompt", HUGE]

    def test_timeout_says_seconds_not_command_dump(self):
        msg = E.concise_error(subprocess.TimeoutExpired(cmd=self.CMD, timeout=240))
        self.assertIn("timed out", msg)
        self.assertIn("240", msg)                 # the actual reason, past char 300 in str(e)
        self.assertNotIn("X" * 100, msg)          # the command dump must not leak
        self.assertLess(len(msg), 80)

    def test_called_process_error_keeps_code_and_stderr_tail(self):
        e = subprocess.CalledProcessError(
            returncode=2, cmd=self.CMD,
            stderr="warning noise\nfatal: the real reason at the end")
        msg = E.concise_error(e)
        self.assertIn("2", msg)                              # exit code preserved
        self.assertIn("the real reason at the end", msg)     # stderr tail preserved
        self.assertNotIn("X" * 100, msg)                     # command dump must not leak

    def test_called_process_error_falls_back_to_output(self):
        # claude can fail with its message on stdout and an empty stderr — use output then.
        e = subprocess.CalledProcessError(returncode=1, cmd=self.CMD,
                                          output="boom from stdout", stderr="")
        self.assertIn("boom from stdout", E.concise_error(e))

    def test_generic_exception_keeps_tail_not_head(self):
        e = RuntimeError("HEAD-noise " + "z" * 400 + " real-cause-at-the-end")
        msg = E.concise_error(e, limit=120)
        self.assertIn("real-cause-at-the-end", msg)   # tail kept, not a blind leading slice
        self.assertNotIn("HEAD-noise", msg)
        self.assertIn("RuntimeError", msg)            # type label aids triage


class TestReadingStyle(unittest.TestCase):
    """The injected <style> now carries a whole-book font stack and a size normalization
    (some conversions ship unreadably tiny absolute sizes). The .zh rule is class-only,
    so the normalization must exclude .zh elements or its !important would win."""

    def _opts(self, **kw):
        base = dict(base_font="Noto Sans SC", no_font_normalize=False,
                    translation_style="color: #777;")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_default_has_font_stack_normalization_and_zh(self):
        css = E.reading_style_css(self._opts())
        self.assertIn('font-family: "Noto Sans SC", "PingFang SC"', css)
        self.assertIn("font-size: 1em !important", css)
        self.assertIn(":not(.zh)", css)               # normalization must not beat .zh
        self.assertIn(".zh { color: #777; }", css)

    def test_empty_base_font_keeps_book_fonts(self):
        css = E.reading_style_css(self._opts(base_font=""))
        self.assertNotIn("font-family", css)
        self.assertIn("font-size: 1em !important", css)

    def test_no_font_normalize_skips_size_rules(self):
        css = E.reading_style_css(self._opts(no_font_normalize=True))
        self.assertNotIn("!important", css)
        self.assertIn(".zh { color: #777; }", css)

    def test_ensure_zh_style_injects_and_updates_in_place(self):
        ns = "http://www.w3.org/1999/xhtml"
        root = etree.fromstring(
            f'<html xmlns="{ns}"><head></head><body><p>x</p></body></html>'.encode())
        E.ensure_zh_style(root, "body { font-size: 1em; }")
        E.ensure_zh_style(root, "body { font-size: 2em; }")   # re-inject: update, not append
        styles = root.findall(f".//{{{ns}}}style")
        self.assertEqual(len(styles), 1)
        self.assertEqual(styles[0].text, "body { font-size: 2em; }")
        self.assertEqual(styles[0].get("class"), "bilingual-zh")


class TestLargeFontClasses(unittest.TestCase):
    """InDesign/publisher EPUBs typeset chapter titles as styled paragraphs
    (<p class="Heading-1">, <p class="Title">, drop caps) rather than <h1..h6>. The 0.4.x
    size normalization forces every <p> to 1em, which flattens those headings to body size.
    large_font_classes reads the book's own CSS and collects the classes it sized above 1em,
    so reading_style_css can exclude them from the :not(...) chain and leave headings alone."""

    def test_over_1em_thresholds_by_unit(self):
        # sized ABOVE the 1em / 100% / 16px / 12pt baseline → a heading, must be preserved
        for val, unit in [("1.667", "em"), ("2.5", "rem"), ("150", "%"), ("24", "px"), ("18", "pt")]:
            self.assertTrue(E.font_size_over_1em(val, unit), f"{val}{unit} should be large")
        # at or below the baseline → body text, safe to normalize up to 1em
        for val, unit in [("1", "em"), ("0.833", "em"), ("100", "%"), ("16", "px"), ("12", "pt")]:
            self.assertFalse(E.font_size_over_1em(val, unit), f"{val}{unit} should be body-size")

    def test_collects_only_classes_sized_above_1em(self):
        css = (
            ".Heading-1 { font-size: 1.667em; }\n"
            ".Title { color: #000; font-size: 2.5em; }\n"   # font-size not first in the block
            ".body-text { font-size: 0.833em; }\n"          # tiny body text → NOT preserved
            ".note { color: gray; }\n"                       # no font-size → NOT preserved
        )
        self.assertEqual(E.large_font_classes(css), {"Heading-1", "Title"})

    def test_grouped_and_descendant_selectors_contribute_every_class(self):
        css = (".ch-title, p.chapNo { font-size: 200%; }\n"
               ".drop span._idGenDropcap-1 { font-size: 2.846em; }")
        self.assertEqual(E.large_font_classes(css),
                         {"ch-title", "chapNo", "drop", "_idGenDropcap-1"})

    def test_real_heading_tags_do_not_pollute_the_set(self):
        # <h1..h6> already keep their scale; a book that uses real heading tags yields no
        # exclusions, so the blanket rule is emitted unchanged.
        self.assertEqual(E.large_font_classes("p { font-size: 1em; } h1 { font-size: 2em; }"), set())

    def test_reading_style_excludes_big_classes_on_every_selector(self):
        opts = types.SimpleNamespace(base_font="", no_font_normalize=False,
                                     translation_style="color: #777;")
        css = E.reading_style_css(opts, {"Title", "Heading-1"})   # excluded in sorted order
        self.assertIn("p:not(.zh):not(.Heading-1):not(.Title)", css)
        self.assertIn("li:not(.zh):not(.Heading-1):not(.Title)", css)
        self.assertIn("blockquote:not(.zh):not(.Heading-1):not(.Title)", css)

    def test_reading_style_without_big_classes_keeps_blanket_rule(self):
        # default (no publisher heading classes, e.g. the generated PDF path) is unchanged
        opts = types.SimpleNamespace(base_font="", no_font_normalize=False,
                                     translation_style="color: #777;")
        self.assertIn("p:not(.zh), li:not(.zh), blockquote:not(.zh)",
                      E.reading_style_css(opts))


class TestPathSkip(unittest.TestCase):
    SKIP = [s.strip().lower() for s in E.DEFAULT_SKIP.split(",") if s.strip()]

    def test_zlibrary_content_files_not_skipped(self):
        # Regression: Calibre/Z-Library split every chapter into index_split_<n>.html. The
        # back-matter 'index' token must not substring-match them, or the whole book is skipped
        # (discover_targets returned 0 content docs on "The Mountain in the Sea").
        for n in ("index_split_001.html", "index_split_042.html", "index_split_110.html"):
            self.assertFalse(E.path_skipped(n, self.SKIP), n)

    def test_real_front_back_matter_still_skipped(self):
        for n in ("titlepage.xhtml", "cover.html", "toc.ncx", "copyright.xhtml",
                  "bibliography.html", "acknowledgments.xhtml"):
            self.assertTrue(E.path_skipped(n, self.SKIP), n)


class TestEmptyExtractionGuard(unittest.TestCase):
    """Extraction that finds nothing translatable must fail loudly, not print a misleading
    '✓ extract done … 0' and go on to build an empty bilingual book."""

    def _opts(self):
        return types.SimpleNamespace(min_words=150, skip=E.DEFAULT_SKIP,
                                     no_auto_skip=False, unit_words=2500)

    def test_zero_paragraphs_aborts(self):
        with self.assertRaises(SystemExit):
            E._print_extract_summary(0, 0, 0, self._opts())

    def test_nonempty_extraction_does_not_abort(self):
        with contextlib.redirect_stdout(io.StringIO()):
            E._print_extract_summary(5, 1, 100, self._opts())   # must not raise

    def test_hint_names_the_knobs(self):
        msg = E.no_content_hint(self._opts())
        for knob in ("--min-words", "--skip", "--no-auto-skip"):
            self.assertIn(knob, msg)


class TestMatterDetection(unittest.TestCase):
    NS = "http://www.w3.org/1999/xhtml"

    def _doc(self, body):
        return etree.fromstring(f'<html xmlns="{self.NS}"><body>{body}</body></html>'.encode())

    def test_index_by_title(self):
        self.assertTrue(E.looks_like_matter(self._doc("<h2>Index</h2>"), ["Adams, 12", "Brand, 5, 9"]))

    def test_copyright_by_body(self):
        root = self._doc("<h2>Penguin Books</h2>")
        self.assertTrue(E.looks_like_matter(root, ["All rights reserved. ISBN 0-14-000000-0.", "x"]))

    def test_index_by_structure(self):
        paras = [f"Term{i}, {i * 3}, {i * 5}" for i in range(20)]   # short entries with page numbers
        self.assertTrue(E.looks_like_matter(self._doc(""), paras))

    def test_keeps_short_chapter_with_dates_and_footnotes(self):
        # Regression (Isaacson "Leonardo da Vinci" ch.19 "Personal Turmoil"): a SHORT chapter
        # thick with notebook quotes, dates and footnote markers is 60%+ short digit-bearing
        # lines — which used to look exactly like an index and got the whole chapter auto-skipped.
        # Real prose paragraphs (long, no page numbers) are the tell that it's a chapter.
        prose = ("Leonardo sublimated whatever emotions he felt about his mother's arrival and her "
                 "death, recording in his notebook only the bare expenses of her funeral with the "
                 "same notarial precision he gave to everything else that he ever cared to observe.")
        short = ["On the 16th day of July.", "Caterina came in 1493.", "He gave 20 soldi to her.",
                 "She died in June 1494.", "1", "2", "3", "She was 60 years old."]
        paras = [prose] * 5 + short * 2          # 16 short + 5 long → 76% short, but real prose
        self.assertFalse(E.looks_like_matter(self._doc("<h2>Chapter 19</h2>"), paras))

    def test_keeps_real_chapter(self):
        root = self._doc("<h2>Chapter 1</h2>")
        paras = ["This is a long narrative paragraph with plenty of words and no page numbers "
                 "describing how buildings adapt over the decades after they are built and used."]
        self.assertFalse(E.looks_like_matter(root, paras))

    def test_keeps_long_chapter_despite_printed_in(self):
        # Regression ("The Power Broker" ch.42, "Tavern in the Town", ~11k words): narrative
        # prose about a newspaper controversy naturally says a story was "printed in" the paper,
        # which used to trip the copyright/colophon detector and auto-skip the whole chapter.
        # A copyright page is a few hundred words at most, so the body check is length-gated.
        sentence = ("The charge that had been printed in the morning paper raced through every "
                    "tavern in the town, and the men who ran the city read it over breakfast and "
                    "understood at once that the long fight was now finally out in the open. ")
        paras = [sentence] * 12                                  # ~500 words of real body text
        root = self._doc("<h2>42. Tavern in the Town</h2>")
        self.assertFalse(E.looks_like_matter(root, paras))

    def test_short_colophon_still_flagged(self):
        # The length gate must not weaken real colophon detection: a short copyright page that
        # says "all rights reserved" / "printed and bound in" is still front/back matter.
        root = self._doc("<h2>Vintage Books</h2>")              # neutral head -> exercises body path
        paras = ["All rights reserved. Printed and bound in the United States of America.",
                 "Published by Vintage Books, a division of Penguin Random House."]
        self.assertTrue(E.looks_like_matter(root, paras))

    def test_kobo_style_comment_is_skipped(self):
        # Regression (qntm "There Is No Antimemetics Division", and any kobo/Calibre EPUB):
        # every document embeds a "<!-- kobo-style -->" comment. looks_like_matter scans
        # root.iter() for a heading, and a comment node's .tag is a callable (not a string),
        # so the namespace-stripping .split("}") crashed with AttributeError before the heading
        # was ever reached. The comment must be skipped: real headings are still detected.
        matter = self._doc("<!-- kobo-style --><h2>Index</h2>")
        self.assertTrue(E.looks_like_matter(matter, ["Adams, 12", "Brand, 5, 9"]))
        chapter = self._doc("<!-- kobo-style --><h2>Chapter 1</h2>")
        self.assertFalse(E.looks_like_matter(chapter, [
            "This is a long narrative paragraph with plenty of words and no page numbers "
            "describing how the antimemetics division forgets the very thing it hunts."]))

    def test_matter_via_head_param(self):
        # The PDF front-end has no XHTML root; it passes the section title via head=.
        idx = ["POSIX, 79", "BASIC, 7, 10", "C, 45, 52"] + \
              [f"Term{i}, {i}, {i * 2}" for i in range(15)]
        self.assertTrue(E.looks_like_matter(None, idx, head="Index"))
        self.assertTrue(E.looks_like_matter(
            None, ["All rights reserved. ISBN 0-06-000000-0.", "x"], head="Copyright"))
        self.assertFalse(E.looks_like_matter(
            None, ["A perfectly ordinary narrative paragraph with many words and no page "
                   "numbers at all, just prose that should clearly be translated as body."],
            head="One"))


class TestNumberDespacing(unittest.TestCase):
    def test_collapses_spaced_digits(self):
        self.assertEqual(E.despace_numbers("in 1 9 1 7 and"), "in 1917 and")
        self.assertEqual(E.despace_numbers("until 1 1 5 5 on"), "until 1155 on")

    def test_collapses_thousands_with_comma(self):
        self.assertEqual(E.despace_numbers("the 3 5 0 ,000 speakers"), "the 350,000 speakers")

    def test_leaves_prose_and_decimals_untouched(self):
        self.assertEqual(E.despace_numbers("about 5 percent today"), "about 5 percent today")
        self.assertEqual(E.despace_numbers("version 1.0 shipped"), "version 1.0 shipped")
        self.assertEqual(E.despace_numbers("earned Ph.D . 's"), "earned Ph.D . 's")


class TestPdfHeadingDetection(unittest.TestCase):
    def test_rejects_fragments(self):
        # single letters, speaker tags, bare roman numerals, lone all-caps tech terms —
        # all the noise that shredded "Just for Fun" into 78 fake chapters.
        for frag in ["I", "L:", "C.", "II.", "IV.", "XII .", "MINIX", "LINUX", "DAVID:"]:
            self.assertFalse(E._is_heading(frag), f"should reject {frag!r}")

    def test_accepts_real_titles(self):
        for title in ["BIRTH OF A NERD", "KING OF THE BALL", "Chapter 3", "Part Two"]:
            self.assertTrue(E._is_heading(title), f"should accept {title!r}")


class TestRawModeSelection(unittest.TestCase):
    def test_shred_fraction_flags_glyph_split_text(self):
        # some PDFs typeset sidebars in a font pdftotext's default mode shreds glyph-by-glyph;
        # a high single-char-token fraction is the signal to fall back to `pdftotext -raw`.
        clean = "About my first memory of Linus doing something remarkable today here now."
        shred = "About my fi rst m e m o ry of L i n u s d o i n g"
        self.assertLess(E.shred_fraction(clean), 0.05)
        self.assertGreater(E.shred_fraction(shred), 0.30)

    def test_shred_fraction_ignores_legit_a_and_I(self):
        # 'a' and 'I' are real one-letter English words — never count them as shredding
        self.assertEqual(E.shred_fraction("I saw a cat and a dog and I left"), 0.0)


class TestBackMatterTrim(unittest.TestCase):
    BODY = ("This is a perfectly ordinary narrative paragraph with plenty of flowing words and "
            "no page numbers at all, just prose about the meaning of life, software and people.")

    def test_trims_trailing_index_and_backcover(self):
        index = [
            "Adams, Douglas, 28 advocacy newsgroups, 15 Atari, 132 audiocassettes, 216-17",
            "Boies, David, 187 Bourne Shell, 82-83 Brecht, Bertolt, 32 browsers, 156,158,201",
            "POSIX standards, 79-80 Prince of Persia, 49 Red Hat, 130,160,201 Sun, 173,174-75",
        ]
        backcover = ["Some people are born to lead millions.", "USA $14.99", "' ' ' \\ \\ \\ ---"]
        body = [self.BODY] * 30
        self.assertEqual(E.trim_trailing_matter(body + index + backcover), body)

    def test_keeps_body_with_occasional_dates(self):
        # a date / version number here and there must NOT look like an index
        body = [("In 1991 Linus released version 0.01 of the kernel to a handful of testers, and "
                 "over the next 3 years the project grew into something nobody had expected at all.")] * 30
        self.assertEqual(E.trim_trailing_matter(body), body)

    def test_short_book_untouched(self):
        self.assertEqual(E.trim_trailing_matter([self.BODY] * 5), [self.BODY] * 5)


class TestGlueRepair(unittest.TestCase):
    def test_resplit_of_before_capital(self):
        # `pdftotext -raw` occasionally glues 'of'/'off' to a following capitalized word
        self.assertEqual(E.resplit_glued("the city ofVasa today"), "the city of Vasa today")
        self.assertEqual(E.resplit_glued("fell offThe roof"), "fell off The roof")

    def test_leaves_real_words_untouched(self):
        for w in ["often", "offer", "office", "offspring", "software", "OfficeMax"]:
            self.assertEqual(E.resplit_glued(w), w)


class TestMetaFilter(unittest.TestCase):
    def test_flags_real_ai_refusals(self):
        for junk in [
            "抱歉，我无法完成这个翻译请求。",
            "很抱歉，我不能提供这段内容的翻译。",
            "抱歉，我没有办法处理这段文字。",
            "抱歉，但我不会翻译这种内容。",
            "作为一个AI语言模型，我不便回答。",
            "以下是这段话的翻译：",
            "译文如下",
            "I'm sorry, but I cannot help with that.",
            "```\n译文\n```",
        ]:
            self.assertTrue(E._META.search(junk), f"should flag junk: {junk!r}")

    def test_keeps_dialogue_containing_sorry(self):
        # "抱歉" in ordinary quoted dialogue must NOT be treated as refusal junk
        for ok in [
            "“这么早打扰您真是抱歉，”那名记者说，“但我想请您评论诺贝尔奖。”",
            "他低声道歉：“非常抱歉，那天我说得太重了。”",
            "她耸耸肩，说不必抱歉。",
        ]:
            self.assertFalse(E._META.search(ok), f"should NOT flag dialogue: {ok!r}")


class TestStatusDashboard(unittest.TestCase):
    def test_active_run_is_marked(self):
        self.assertIn("→", E.fmt_run_line("bookA", 5, 5, 100, 100, active=True))
        self.assertNotIn("→", E.fmt_run_line("bookB", 5, 2, 100, 40, active=False))

    def test_done_vs_in_progress(self):
        self.assertIn("done", E.fmt_run_line("a", 83, 83, 2418, 2415))
        self.assertNotIn("done", E.fmt_run_line("a", 83, 30, 2418, 650))
        self.assertIn("30/83", E.fmt_run_line("a", 83, 30, 2418, 650))

    def test_qa_failure_tail_only_when_present(self):
        self.assertIn("qa✗3", E.fmt_run_line("a", 83, 83, 2418, 2415, qa_failed=3))
        self.assertNotIn("qa✗", E.fmt_run_line("a", 83, 83, 2418, 2418, qa_failed=0))

    def test_slug_and_counts_present(self):
        line = E.fmt_run_line("my-book-slug", 10, 4, 500, 200)
        self.assertIn("my-book-slug", line)
        self.assertIn("4/10", line)
        self.assertIn("200", line)
        self.assertIn("500", line)


class TestQAIncremental(unittest.TestCase):
    """QA must be idempotent: re-running it on an already-judged book does no work and spends
    no claude calls. Only freshly (re)translated rows are 'untested' (cmd_translate resets
    qa_state on every write), so they alone get re-checked; a row left 'l1flag' by an
    interrupted run still needs L2. Everything decided ('l1ok'/'passed'/'failed'/'repaired')
    is skipped."""

    def test_finished_book_has_no_work(self):
        rows = [("a", "l1ok"), ("b", "passed"), ("c", "failed"), ("d", "repaired")]
        self.assertEqual(E.qa_worklist(rows), ([], []))

    def test_fresh_and_interrupted_rows_are_the_work(self):
        rows = [("a", "untested"), ("b", "passed"), ("c", "l1flag"),
                ("d", "l1ok"), ("e", "untested")]
        l1_todo, l2_flagged = E.qa_worklist(rows)
        self.assertEqual(l1_todo, ["a", "e"])    # only (re)translated rows get re-checked
        self.assertEqual(l2_flagged, ["c"])      # an interrupted run's flag still needs L2


class TestQABatchResilience(unittest.TestCase):
    """L2's judge occasionally returns the wrong number of verdicts for a batch (it merges or
    splits a pair). Discarding all N verdicts strands every pair in the batch at 'l1flag'
    forever (re-runs re-batch them the same way and hit the same miscount). qa_judge_robust
    bisects on a length mismatch — mirroring translate_robust — so one poison pair can't strand
    its neighbours; a lone pair that still can't be judged gets a conservative verdict that
    routes it to L3 repair instead of a silent pass. The fake judge stands in only for the
    model call (where the miscount originates); the bisect/reassembly under test is real."""

    def setUp(self):
        self._real_judge = E.qa_judge

    def tearDown(self):
        E.qa_judge = self._real_judge

    def test_bisects_around_poison_pair_and_preserves_order(self):
        poison = ("BAD-EN", "BAD-ZH")
        sizes = []

        def fake_judge(pairs, model, timeout):
            sizes.append(len(pairs))
            if len(pairs) > 1 and poison in pairs:        # the model miscounts multi-item batches
                raise ValueError(f"qa length mismatch: {len(pairs) - 1} vs {len(pairs)}")
            return [{"faithful": 1 if p == poison else 5,
                     "missing": False, "hallucinated": False} for p in pairs]

        E.qa_judge = fake_judge
        pairs = [(f"en{i}", f"zh{i}") for i in range(8)]
        pairs[3] = poison
        out = E.qa_judge_robust(pairs, "model", 1)
        self.assertEqual(len(out), 8)                       # nothing dropped
        self.assertEqual(out[3]["faithful"], 1)             # poison verdict lands in its own slot
        self.assertTrue(all(out[i]["faithful"] == 5 for i in range(8) if i != 3))  # order preserved
        self.assertEqual(max(sizes), 8)                     # first attempt was the whole batch
        self.assertIn(1, sizes)                             # bisected all the way to the singleton

    def test_unjudgeable_singleton_fails_safe_not_silent_pass(self):
        def always_miscounts(pairs, model, timeout):
            raise ValueError("qa length mismatch: 0 vs 1")

        E.qa_judge = always_miscounts
        out = E.qa_judge_robust([("x", "y")], "model", 1)
        self.assertEqual(len(out), 1)
        v = out[0]
        bad = v.get("faithful", 5) <= 2 or ((v.get("missing") or v.get("hallucinated"))
                                            and v.get("faithful", 5) <= 3)
        self.assertTrue(bad)        # cmd_qa marks it 'failed' → L3 repair, never a silent 'passed'


class TestPackedChapters(unittest.TestCase):
    """Kindle-style books pack many chapters into one XHTML file ('The Wind in the
    Willows' ships 12 chapters as 2 docs → the rebuilt TOC had 2 entries). doc_chapters
    finds the in-document chapter markers; expand_packed_chapters turns them into
    per-chapter anchored entries, injecting element ids idempotently."""
    NS = "http://www.w3.org/1999/xhtml"

    PACKED = (
        '<p align="justify"><font size="5">CHAPTER</font> <font size="5"><b>1</b></font></p>'
        '<p class="zh">第一章</p>'
        '<span id="filepos0000004171"></span>'
        '<p><font size="5">THE RIVER BANK</font></p>'
        '<p class="zh">河岸</p>'
        '<p>The Mole had been working very hard all the morning, spring-cleaning his home.</p>'
        '<p><font size="5">CHAPTER</font> <font size="5"><b>2</b></font></p>'
        '<p class="zh">第二章</p>'
        '<p><font size="5">THE OPEN ROAD</font></p>'
        '<p>Ratty, said the Mole suddenly, one bright summer morning by the river bank.</p>'
    )

    def _doc(self, body):
        return etree.fromstring(f'<html xmlns="{self.NS}"><body>{body}</body></html>'.encode())

    def test_finds_split_marker_and_title(self):
        # marker text spans two <font> tags; title is a separate block past the zh sibling
        chs = E.doc_chapters(self._doc(self.PACKED))
        self.assertEqual([c[0] for c in chs],
                         ["CHAPTER 1 — THE RIVER BANK", "CHAPTER 2 — THE OPEN ROAD"])

    def test_inline_title_and_word_numbers(self):
        chs = E.doc_chapters(self._doc(
            '<h2>Chapter 1. The River Bank</h2><p>prose</p>'
            '<h2>Chapter Twelve</h2><p>prose</p>'))
        self.assertEqual([c[0] for c in chs], ["Chapter 1. The River Bank", "Chapter Twelve"])

    def test_quote_split_title_is_joined(self):
        # the Willows source typesets ch11's quoted title across TWO blocks, and pads the
        # gap with <br>/<span> nodes and .zh siblings — real markup, not a clean sketch
        # (a raw-sibling scan cap exhausted on the padding and truncated the title)
        chs = E.doc_chapters(self._doc(
            '<p><font size="5">CHAPTER</font> <font size="5"><b>11</b></font></p>'
            '<p class="zh">第十一章</p>'
            '<br/><br/><br/><br/>'
            '<span id="filepos0000347259"></span>'
            '<p><font size="5">“LIKE SUMMER TEMPEST</font></p>'
            '<p class="zh">"如夏日雷雨</p>'
            '<span id="filepos0000347351"></span>'
            '<p><font size="5">CAME HIS TEARS”</font></p>'
            '<p class="zh">涌来的泪水"</p>'
            '<p>The Rat put out a neat little brown paw and gripped Toad firmly.</p>'
            '<p><font size="5">CHAPTER</font> <font size="5"><b>12</b></font></p>'
            '<p><font size="5">THE RETURN OF ULYSSES</font></p>'))
        self.assertEqual(chs[0][0], "CHAPTER 11 — “LIKE SUMMER TEMPEST CAME HIS TEARS”")
        self.assertEqual(chs[1][0], "CHAPTER 12 — THE RETURN OF ULYSSES")

    def test_prose_mentioning_a_chapter_is_not_a_marker(self):
        chs = E.doc_chapters(self._doc(
            '<p>Chapter 3 was the hardest to write.</p>'
            '<p>In Chapter 4 everything changed.</p>'))
        self.assertEqual(chs, [])

    def test_chapter_summaries_heading_is_not_a_marker(self):
        # number words are an explicit list, so 'Summaries' must not read as a number
        self.assertEqual(E.doc_chapters(self._doc('<h2>Chapter Summaries</h2>')), [])

    def test_expand_injects_anchors_idempotently(self):
        root = self._doc(self.PACKED)
        docs = [("c1.html", root)]
        entries = [("THE RIVER BANK", "c1.html")]
        final, dirty = E.expand_packed_chapters(docs, entries)
        self.assertEqual(dirty, {"c1.html"})
        self.assertEqual([h for _, h in final], ["c1.html#ebz-ch001", "c1.html#ebz-ch002"])
        again, _ = E.expand_packed_chapters(docs, entries)   # ids reused, not re-minted
        self.assertEqual(final, again)

    def test_doc_without_packed_chapters_keeps_doc_level_entry(self):
        root = self._doc('<h2>A Lone Chapter</h2><p>Just prose here, nothing packed.</p>')
        final, dirty = E.expand_packed_chapters([("c2.html", root)],
                                                [("A Lone Chapter", "c2.html")])
        self.assertEqual(final, [("A Lone Chapter", "c2.html")])
        self.assertEqual(dirty, set())


class TestTocRebuild(unittest.TestCase):
    """Rebuilding the EPUB navigation (toc.ncx <navMap>) so chapters are reachable.
    B (preferred): parse the book's own contents page — best titles, correct targets.
    A (fallback):  use each document's own heading when there is no contents page."""
    NS = "http://www.w3.org/1999/xhtml"

    def _doc(self, body):
        return etree.fromstring(f'<html xmlns="{self.NS}"><body>{body}</body></html>'.encode())

    # ── A: per-document heading extraction ───────────────────────────────────
    def test_heading_prefers_h_tag(self):
        root = self._doc('<h2>35. "RM"</h2><p>Body paragraph of the chapter here.</p>')
        self.assertEqual(E.doc_heading(root), '35. "RM"')

    def test_heading_falls_back_to_short_first_paragraph(self):
        # Z-Library's Chap11 typesets the chapter title in a <p>, not an <h*>.
        root = self._doc('<p>11. The Majesty of the Law</p><p>The referendum was still on…</p>')
        self.assertEqual(E.doc_heading(root), "11. The Majesty of the Law")

    def test_heading_empty_when_first_paragraph_is_body(self):
        # part0018 is a continuation fragment: it begins mid-narrative, so it has no title
        # and must NOT become a TOC entry. A long first <p> is body text, not a heading.
        root = self._doc('<p>This, of course, need not in itself have been an insurmountable '
                         'handicap to a man of his manifold talents and relentless drive.</p>')
        self.assertEqual(E.doc_heading(root), "")

    def test_heading_empty_for_numeric_or_imageonly_page(self):
        # Index page starts with a stray page number "11"; image plate page has no text.
        self.assertEqual(E.doc_heading(self._doc('<p>11</p><p>Battery, the: 645, 646</p>')), "")
        self.assertEqual(E.doc_heading(self._doc('<div><img/></div>')), "")

    # ── B: in-book contents page parsing ─────────────────────────────────────
    def test_contents_toc_extracts_filters_and_dedups(self):
        page = self._doc(
            '<p><a href="../Text/Chap1.html">1.\tLine of Succession</a></p>'
            '<p><a href="../Text/Chap2.html#mid">2. Robert Moses at Yale</a></p>'
            '<p><a href="../Text/Chap1.html">(again)</a></p>'      # same target → first kept only
            '<p><a href="../Images/plate.jpg">a photo</a></p>'     # not a spine doc → dropped
            '<p><a href="../Text/Chap9.html"></a></p>')            # empty label → dropped
        keep = {"Text/Chap1.html", "Text/Chap2.html", "Text/Chap9.html"}
        self.assertEqual(
            E.contents_toc(page, "Text/CONTENTS.html", keep),
            [("1. Line of Succession", "Text/Chap1.html"),
             ("2. Robert Moses at Yale", "Text/Chap2.html")])

    # ── B preferred / A fallback selection across the spine ──────────────────
    def test_pick_toc_uses_named_contents_page(self):
        contents = self._doc('<p><a href="Chap1.html">1. A</a></p>'
                             '<p><a href="Chap2.html">2. B</a></p>'
                             '<p><a href="CONTENTS.html">self link</a></p>')   # self-ref dropped
        docs = [("Text/CONTENTS.html", contents),
                ("Text/Chap1.html", self._doc('<h2>noise</h2>')),
                ("Text/Chap2.html", self._doc('<h2>noise</h2>'))]
        keep = {"Text/CONTENTS.html", "Text/Chap1.html", "Text/Chap2.html"}
        # returns (entries, the contents page's own rel) so the caller can exclude that page
        self.assertEqual(E.pick_toc_from_spine(docs, keep),
                         ([("1. A", "Text/Chap1.html"), ("2. B", "Text/Chap2.html")],
                          "Text/CONTENTS.html"))

    def test_pick_toc_empty_without_a_contents_page(self):
        # plain chapters with at most a stray cross-link — none is a contents page, so B yields
        # (no entries, no page) and the caller falls back to A.
        c1 = self._doc('<h2>1. A</h2><p><a href="Chap2.html">see chapter 2</a></p>')
        docs = [("Text/Chap1.html", c1), ("Text/Chap2.html", self._doc('<h2>2. B</h2>'))]
        self.assertEqual(E.pick_toc_from_spine(docs, {"Text/Chap1.html", "Text/Chap2.html"}),
                         ([], None))

    def test_merge_recovers_mislinked_chapter_and_drops_clutter(self):
        # The Power Broker case: the contents page links "49. The Last Stand" to the wrong file
        # (Chap48), so dedup drops it and Chap49 is absent from B. The spine walk recovers it from
        # its own NUMBERED heading, in place — but does NOT pull in the contents page itself, nor
        # non-chapter clutter (a dedication) that B deliberately left out.
        docs = [("Text/CONTENTS.html", self._doc('<h2>Table of Contents</h2>')),  # the page itself
                ("Text/part0000.html", self._doc('<p>FOR INA</p>')),              # dedication
                ("Text/Chap48.html", self._doc('<h2>48. Old Lion</h2>')),
                ("Text/Chap49.html", self._doc('<h2>49. The Last Stand</h2>')),
                ("Text/Chap50.html", self._doc('<h2>50. Old</h2>'))]
        contents = [("48. Old Lion, Young Mayor", "Text/Chap48.html"),
                    ("50. Old", "Text/Chap50.html")]              # 49 mislinked → not in B
        self.assertEqual(
            E.merge_toc(docs, contents, "Text/CONTENTS.html"),
            [("48. Old Lion, Young Mayor", "Text/Chap48.html"),   # B title preferred
             ("49. The Last Stand", "Text/Chap49.html"),          # recovered (numbered), in place
             ("50. Old", "Text/Chap50.html")])                    # CONTENTS + 'FOR INA' dropped

    def test_merge_pure_fallback_lists_every_titled_doc(self):
        # No contents page → A only: take every doc's heading, numbered or not (a Foreword counts),
        # skipping only titleless continuation fragments.
        frag = self._doc('<p>A long continuation paragraph of body text that is plainly not a '
                         'title and runs well past a dozen words before it finally ends here.</p>')
        docs = [("Text/fwd.html", self._doc('<h2>Foreword</h2>')),
                ("Text/Chap1.html", self._doc('<h2>1. A</h2>')),
                ("Text/part1.html", frag)]
        self.assertEqual(E.merge_toc(docs, [], None),
                         [("Foreword", "Text/fwd.html"), ("1. A", "Text/Chap1.html")])

    def test_merge_fallback_drops_author_other_works_card(self):
        # Isaacson "Benjamin Franklin" (z-library scan): the CONTENTS page is plain text with no
        # <a> links, so B yields nothing and A lists every titled doc. part0001.html is an "also
        # by the author" ad card — the author's OTHER books, each typeset as an italicized title
        # with no heading of its own. doc_heading() returns the first title, so the very first
        # navPoint used to read "Kissinger: A Biography" inside a Franklin book. A must recognize
        # an other-works card and leave it out (the markup is Calibre's real adCardPage nesting).
        adcard = self._doc(
            '<div class="adCardPage">'
            '<p class="adCardText"><span><span class="italic"><span>Kissinger: A Biography</span></span></span></p>'
            '<p class="adCardText1"><span><span class="italic"><span>The Wise Men: Six Friends</span></span></span></p>'
            '<p class="adCardText1"><span>(with Evan Thomas)</span></p>'
            '<p class="adCardText"><span><span class="italic"><span>Pro and Con</span></span></span></p>'
            '</div>')
        docs = [("text/part0001.html", adcard),
                ("text/part0003.html", self._doc('<h2>Foreword</h2><p>Real foreword prose.</p>')),
                ("text/part0007.html", self._doc('<h2>Chapter One</h2><p>Body.</p>'))]
        self.assertEqual(E.merge_toc(docs, [], None),
                         [("Foreword", "text/part0003.html"),
                          ("Chapter One", "text/part0007.html")])

    def test_other_works_card_detected(self):
        # No heading, dominated by italicized book-title lines — under both Calibre's
        # <span class="italic"> and the standard <i>/<em> markup other publishers ship.
        self.assertTrue(E.looks_like_other_works(self._doc(
            '<p><span class="italic">Kissinger: A Biography</span></p>'
            '<p><span class="italic">The Wise Men</span></p>'
            '<p>(with Evan Thomas)</p>')))
        self.assertTrue(E.looks_like_other_works(self._doc(
            '<p><i>Steve Jobs</i></p><p><em>The Innovators</em></p>')))

    def test_other_works_detection_spares_real_pages(self):
        # No false positives: a titled chapter / foreword, a one-line dedication, and the tricky
        # case of a real chapter whose body merely *mentions* an italicized title are NOT cards.
        for body in (
            '<h2>Foreword</h2><p>Real prose here.</p>',
            '<h2>1. A</h2><p>Body.</p>',
            '<p>FOR INA</p>',                                       # dedication, single short line
            '<p>The printer devoured <i>Plutarch\'s Lives</i> and a dozen other volumes that '
            'shaped the relentless curiosity of his later years.</p>'
            '<p>He recalled those evenings ever after as the start of his education.</p>',
        ):
            self.assertFalse(E.looks_like_other_works(self._doc(body)), body)

    def test_other_works_detection_spares_italic_dedication(self):
        # The Power Broker part0000: a dedication is ALSO short italic lines with no heading, so it
        # is structurally a card — but it addresses people, not works. Reject it. (Verbatim markup,
        # empty <br/> spacers and all.)
        ded = self._doc(
            '<div class="body">'
            '<p><b><i><br/></i></b></p><p><b><i><br/></i></b></p>'
            '<p><b><i> FOR INA</i></b></p>'
            '<p><b><i> and for  DR. JANET G. TRAVELL</i></b></p>'
            '</div>')
        self.assertFalse(E.looks_like_other_works(ded))

    # ── ncx <navMap> rewrite ─────────────────────────────────────────────────
    def test_set_navmap_replaces_points_keeps_navinfo(self):
        ncx = etree.fromstring(
            f'<ncx xmlns="{E.NCX_NS}"><navMap>'
            '<navInfo><text>Book navigation</text></navInfo>'
            '<navPoint id="old" playOrder="1"><navLabel><text>Front Cover</text></navLabel>'
            '<content src="Text/leaf0001.html"/></navPoint>'
            '</navMap></ncx>'.encode())
        E.set_navmap(ncx, [("1. Line of Succession", "Text/Chap1.html"),
                           ("2. Robert Moses at Yale", "Text/Chap2.html")])
        ns = E.NCX_NS
        pts = ncx.findall(f"{{{ns}}}navMap/{{{ns}}}navPoint")
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0].find(f"{{{ns}}}navLabel/{{{ns}}}text").text, "1. Line of Succession")
        self.assertEqual(pts[0].find(f"{{{ns}}}content").get("src"), "Text/Chap1.html")
        self.assertEqual(pts[1].get("playOrder"), "2")
        self.assertIsNotNone(ncx.find(f"{{{ns}}}navMap/{{{ns}}}navInfo"))    # preserved
        self.assertNotIn("old", [p.get("id") for p in pts])                  # junk point gone


if __name__ == "__main__":
    unittest.main()
