"""Unit tests for the format-agnostic pure logic (no network / no Claude needed).

    python3 test_ebook_bilingual.py
"""
import contextlib
import io
import subprocess
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


if __name__ == "__main__":
    unittest.main()
