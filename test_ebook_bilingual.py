"""Unit tests for the format-agnostic pure logic (no network / no Claude needed).

    python3 test_ebook_bilingual.py
"""
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

    def test_check_l1(self):
        self.assertIn("empty", E.check_l1("hello", "", {}))
        self.assertTrue(any("num_missing" in f for f in
                            E.check_l1("In 1888 he was born.", "他出生了。", {})))
        self.assertEqual(E.check_l1("The cat sat on the mat quietly.",
                                    "猫安静地坐在垫子上。", {}), [])


if __name__ == "__main__":
    unittest.main()
