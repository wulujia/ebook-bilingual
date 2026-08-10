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


# A realistic FAILED `claude -p --output-format json` payload: the CLI exits non-zero but
# still prints well-formed JSON, with the human-readable reason MID-payload (.error/.result)
# and usage boilerplate at the END — the shape that made tail-slicing report boilerplate.
AUTH_PAYLOAD = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "duration_ms": 631,
    "num_turns": 1,
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    "error": "authentication_failed", "session_id": "abc", "total_cost_usd": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
    "modelUsage": {"claude-sonnet": {"inputTokens": 0, "outputTokens": 0, "costUSD": 0}},
})


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

    def test_auth_failure_fails_fast_with_login_hint(self):
        # An expired login is NOT a flaky reply: every retry fails identically, and degrading
        # to an empty glossary would burn a whole-book run — abort with the fix instead.
        calls = []
        def fail(*a, **k):
            calls.append(1)
            raise subprocess.CalledProcessError(1, ["claude", "-p"],
                                                output=AUTH_PAYLOAD, stderr="")
        E.claude_call = fail
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertEqual(len(calls), 1)                   # no pointless retries
        self.assertIn("claude /login", str(cm.exception))


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

    def test_called_process_error_pulls_reason_out_of_json_payload(self):
        # A failed `claude -p --output-format json` puts the real reason MID-payload and
        # boilerplate at the end — the old tail-slice reported only the boilerplate.
        e = subprocess.CalledProcessError(returncode=1, cmd=self.CMD,
                                          output=AUTH_PAYLOAD, stderr="")
        msg = E.concise_error(e)
        self.assertIn("exited 1", msg)
        self.assertIn("authentication_failed", msg)
        self.assertIn("OAuth session expired", msg)
        self.assertNotIn("modelUsage", msg)           # boilerplate tail must not win

    def test_json_payload_beats_stderr_noise(self):
        e = subprocess.CalledProcessError(returncode=1, cmd=self.CMD,
                                          output=AUTH_PAYLOAD, stderr="npm warn old node")
        msg = E.concise_error(e)
        self.assertIn("authentication_failed", msg)
        self.assertNotIn("npm warn", msg)


class TestClaudeJsonError(unittest.TestCase):
    """`claude -p --output-format json` reports failures INSIDE the JSON on stdout: the result
    event carries {is_error, error, result:"human text"} mid-payload while the payload tail is
    usage boilerplate. In a real session the tail-slice masked an expired OAuth login as
    rate-limiting. claude_json_error must pull out the real reason, and return None for
    anything that is not such a failure payload (callers then fall back to the tail slice)."""

    def test_extracts_error_type_and_result_text(self):
        self.assertEqual(
            E.claude_json_error(AUTH_PAYLOAD),
            "authentication_failed: Failed to authenticate: OAuth session expired "
            "and could not be refreshed")

    def test_array_payload_uses_result_event(self):
        payload = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "result", "is_error": True, "error": "api_error", "result": "boom"},
        ])
        self.assertEqual(E.claude_json_error(payload), "api_error: boom")

    def test_error_type_alone_still_reported(self):
        payload = json.dumps({"type": "result", "is_error": True,
                              "error": "rate_limit_exceeded", "usage": {}})
        self.assertEqual(E.claude_json_error(payload), "rate_limit_exceeded")

    def test_error_subtype_stands_in_for_missing_result_text(self):
        # older CLIs emit e.g. subtype=error_max_turns with no .result text at all
        payload = json.dumps({"type": "result", "subtype": "error_max_turns",
                              "is_error": True, "usage": {}})
        self.assertEqual(E.claude_json_error(payload), "error_max_turns")

    def test_success_payload_is_not_an_error(self):
        payload = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                              "result": "你好，世界", "usage": {}})
        self.assertIsNone(E.claude_json_error(payload))

    def test_non_json_and_empty_return_none(self):
        self.assertIsNone(E.claude_json_error("Invalid API key · Please run /login"))
        self.assertIsNone(E.claude_json_error(""))
        self.assertIsNone(E.claude_json_error(None))


class TestAuthFailFast(unittest.TestCase):
    """An expired/broken login fails every retry identically — it must be recognized so
    callers abort with a `claude /login` hint instead of burning retries as rate-limiting."""

    def test_matches_extracted_and_plain_cli_forms(self):
        self.assertTrue(E.is_auth_error(
            "exited 1: authentication_failed: Failed to authenticate: OAuth session "
            "expired and could not be refreshed"))
        self.assertTrue(E.is_auth_error("Invalid API key · Please run /login"))

    def test_ignores_rate_limits_and_timeouts(self):
        self.assertFalse(E.is_auth_error("exited 1: rate_limit_exceeded"))
        self.assertFalse(E.is_auth_error("Claude AI usage limit reached|1712345678"))
        self.assertFalse(E.is_auth_error("timed out after 240s"))
        self.assertFalse(E.is_auth_error(""))


class TestTranslateAuthFailFast(unittest.TestCase):
    """cmd_translate classified every worker failure as rate-limit (infinite backoff) or
    generic (retried until max_attempts). An auth failure is neither — the pool must abort
    the run with a `claude /login` hint after the in-flight pass, keeping progress saved.
    The fake translate_robust stands in only for the worker; the pool logic under test is real."""

    def setUp(self):
        self._real_tr = E.translate_robust
        self._real_gpath = E.GLOSSARY_PATH
        self._tmp = tempfile.TemporaryDirectory()
        E.GLOSSARY_PATH = os.path.join(self._tmp.name, "glossary.json")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        E.db_init(self.conn)
        self.conn.execute("INSERT INTO units(id, name, file) VALUES(1, 'c1', 'c1.html')")
        self.conn.execute("INSERT INTO paragraphs(file, idx, en, sha, unit_id) "
                          "VALUES('c1.html', 0, 'Hello.', 'sha0', 1)")
        self.conn.commit()
        self.opts = types.SimpleNamespace(model="sonnet", unit_timeout=1, concurrency=2,
                                          max_attempts=3, test_file=None)

    def tearDown(self):
        E.translate_robust = self._real_tr
        E.GLOSSARY_PATH = self._real_gpath
        self._tmp.cleanup()

    def test_auth_failure_aborts_with_login_hint_instead_of_retrying(self):
        calls = []
        def fail(*a, **k):
            calls.append(1)
            raise subprocess.CalledProcessError(1, ["claude", "-p"],
                                                output=AUTH_PAYLOAD, stderr="")
        E.translate_robust = fail
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            E.cmd_translate(self.conn, self.opts)
        self.assertIn("claude /login", str(cm.exception))
        self.assertEqual(len(calls), 1)                    # one pass, no backoff loop
        u = self.conn.execute("SELECT * FROM units WHERE id=1").fetchone()
        self.assertEqual(u["state"], "failed")             # progress recorded for the rerun
        self.assertIn("authentication_failed", u["error"])  # the REAL reason persisted


class TestSanitizeWorkerEnv(unittest.TestCase):
    """Workers must not inherit Claude Code session vars. The desktop app injects
    ANTHROPIC_BASE_URL plus CLAUDECODE / CLAUDE_CODE_* into everything it spawns; a child
    `claude -p` inheriting that base URL fails every call with "OAuth session expired"
    even though the user's own CLI login is valid. Outside a Claude Code session the env
    must pass through untouched so a user-set gateway ANTHROPIC_BASE_URL keeps working."""

    def test_outside_claude_code_env_passes_through(self):
        env = {"PATH": "/bin", "ANTHROPIC_BASE_URL": "https://my-gateway.example"}
        self.assertEqual(E.sanitize_worker_env(env), env)

    def test_inside_claude_code_drops_injected_base_url(self):
        env = {"PATH": "/bin", "CLAUDECODE": "1",
               "ANTHROPIC_BASE_URL": "http://localhost:54545"}
        clean = E.sanitize_worker_env(env)
        self.assertEqual(clean, {"PATH": "/bin"})

    def test_claude_code_prefix_alone_is_a_session_marker(self):
        # Detection must not depend on CLAUDECODE specifically; any CLAUDE_CODE_* counts.
        env = {"CLAUDE_CODE_SESSION_ID": "abc", "ANTHROPIC_BASE_URL": "http://x"}
        self.assertEqual(E.sanitize_worker_env(env), {})

    def test_keep_base_url_escape_hatch(self):
        # A user routing claude through their own gateway INSIDE Claude Code keeps the
        # base URL; the session-metadata vars are still meaningless for the child.
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_SCOPES": "s",
               "ANTHROPIC_BASE_URL": "https://my-gateway.example"}
        clean = E.sanitize_worker_env(env, keep_base_url=True)
        self.assertEqual(clean, {"ANTHROPIC_BASE_URL": "https://my-gateway.example"})

    def test_unrelated_claude_vars_survive(self):
        # CLAUDE_CONFIG_DIR is a user-facing CLI setting (where auth lives), NOT session
        # metadata — dropping it would break worker auth in a brand-new way.
        env = {"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/h"}
        self.assertEqual(E.sanitize_worker_env(env),
                         {"CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/h"})

    def test_input_mapping_is_not_mutated(self):
        env = {"CLAUDECODE": "1", "ANTHROPIC_BASE_URL": "http://x"}
        E.sanitize_worker_env(env)
        self.assertEqual(env, {"CLAUDECODE": "1", "ANTHROPIC_BASE_URL": "http://x"})

    def test_session_predicate(self):
        self.assertTrue(E.in_claude_code_session({"CLAUDECODE": "1"}))
        self.assertTrue(E.in_claude_code_session({"CLAUDE_CODE_ENTRYPOINT": "cli"}))
        self.assertFalse(E.in_claude_code_session({"CLAUDE_CONFIG_DIR": "/cfg"}))
        self.assertFalse(E.in_claude_code_session({"PATH": "/bin"}))

    def test_claude_call_spawns_worker_with_sanitized_env(self):
        # The wiring, not just the helper: claude_call must hand subprocess.run a cleaned
        # env (bug was passing a raw os.environ copy through). No network: run is stubbed.
        captured = {}

        def fake_run(cmd, **kw):
            captured["env"] = kw["env"]
            return types.SimpleNamespace(stdout=json.dumps({"result": "ok"}))

        real_run, real_environ = E.subprocess.run, E.os.environ
        E.subprocess.run = fake_run
        E.os.environ = {"PATH": "/bin", "CLAUDECODE": "1",
                        "CLAUDE_CODE_SESSION_ID": "abc",
                        "ANTHROPIC_BASE_URL": "http://localhost:54545"}
        try:
            self.assertEqual(E.claude_call("sp", "payload", "sonnet", 1), "ok")
        finally:
            E.subprocess.run, E.os.environ = real_run, real_environ
        self.assertNotIn("ANTHROPIC_BASE_URL", captured["env"])
        self.assertNotIn("CLAUDECODE", captured["env"])
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", captured["env"])
        self.assertEqual(captured["env"]["PATH"], "/bin")
        self.assertEqual(captured["env"]["MAX_THINKING_TOKENS"], "0")


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

    def test_flags_narrated_decision_preambles(self):
        # The incident class: a worker that saw host-injected plugin context (SessionStart
        # hooks / output styles) narrates its decision before the actual translation.
        for junk in [
            "我发现在文学翻译问答中，skills 框架不太适用。我将直接执行翻译。第三章　我目睹四名同伴溺水",
            "好的，我来直接翻译：第三章",
            "直接输出翻译：第三章　我目睹四名同伴溺水",
            "现在开始翻译。第三章",
            "明白了，输出译文：第三章",
            "I'll translate the chapter heading: 第三章",
            "Let me translate this title: 第三章",
            "Here is the translation: 第三章",
        ]:
            self.assertTrue(E._META.search(junk), f"should flag junk: {junk!r}")

    def test_keeps_faithful_prose_with_first_person_futures(self):
        # Real sentences from a translated memoir: 我将/我发现 in narration, and even
        # "直接翻译" as an object of discussion, must NOT match — the patterns are
        # anchored to announcement verbs precisely so ordinary prose survives.
        for ok in [
            "我知道第三击必须一击必中，否则我将永远失去这份猎物。",
            "但还没等我做出决定，我发现自己已经朝着那只筏子游去。",
            "这个词无法直接翻译，他只好加了一个脚注。",
            "在莫比尔的时候，我曾在一本忘了名字的杂志上读到过一个故事。",
        ]:
            self.assertFalse(E._META.search(ok), f"should NOT flag prose: {ok!r}")


class TestMetaFilterInputRequests(unittest.TestCase):
    """A worker handed one short sentence sometimes reads the payload as an empty request
    and answers by asking for the text ("请提供需要翻译的英文段落。"). check_l1's length
    bounds cannot catch that — a one-line request has a perfectly plausible han/word
    ratio — so _META has to, or the request ships as the paragraph's translation."""

    def test_flags_requests_for_source_text(self):
        for junk in [
            "我们准备好了？请提供需要翻译的英文段落。",
            "请提供需要翻译的段落。",
            "好的，请给出需要翻译的原文。",
            "等待您提供需要翻译的内容。",
        ]:
            self.assertTrue(E._META.search(junk), f"should flag junk: {junk!r}")

    def test_flags_narrated_task_framing(self):
        # The worker describing the job instead of doing it — including the case where it
        # reasoned about host CLAUDE.md persona instructions it should never have seen.
        for junk in [
            "好，我会翻译这些段落。",
            "我先仔细阅读并翻译这些段落。",
            "中文翻译：",
            "我先确认一下——用户在系统提示里设定了助手角色，但这是一个翻译任务。",
        ]:
            self.assertTrue(E._META.search(junk), f"should flag junk: {junk!r}")

    def test_keeps_prose_asking_for_things(self):
        # 请提供 / 给出 / 会翻译 in ordinary narration must survive: the patterns are
        # anchored to a translation-request object, not to the polite verb alone.
        for ok in [
            "海关官员说：“请提供您的护照和入境卡。”",
            "他请我给出一个明确的答复，我却支吾其词。",
            "医生要求她提供过去三年的体检报告。",
            "她说自己会翻译成本地方言讲给祖母听。",
        ]:
            self.assertFalse(E._META.search(ok), f"should NOT flag prose: {ok!r}")


class TestMetaPreambleStripping(unittest.TestCase):
    """Workers sometimes prepend commentary to an otherwise perfect translation, split off
    by a blank line. inject dropped the whole row on a _META hit, so those paragraphs
    shipped English-only. The translation is recoverable: a source paragraph is a single
    block, so any extra block in the output is not translation."""

    def test_strips_labelled_preamble(self):
        self.assertEqual(E.strip_meta_preamble("CHAPTER 6", "以下是翻译：\n\n第六章"), "第六章")

    def test_strips_narrated_decision_preamble(self):
        self.assertEqual(
            E.strip_meta_preamble("He delivered papers from the age of eight.",
                                  "好，我会翻译这些段落。\n\n他从八岁起就开始送报纸。"),
            "他从八岁起就开始送报纸。")

    def test_strips_echoed_english_heading(self):
        en = "SYSTEMIZING OUR LEARNING FROM MISTAKES"
        self.assertEqual(E.strip_meta_preamble(en, en + "\n将我们从错误中学习的过程系统化"),
                         "将我们从错误中学习的过程系统化")

    def test_strips_trailing_translator_note(self):
        self.assertEqual(
            E.strip_meta_preamble("He was no longer CEO.",
                                  "他不再担任首席执行官。\n\n（注：此处人名按常规译法处理。）"),
            "他不再担任首席执行官。")

    def test_keeps_clean_translation_untouched(self):
        for en, zh in [("He was no longer CEO.", "他不再担任首席执行官。"),
                       ("CHAPTER 6", "第六章")]:
            self.assertEqual(E.strip_meta_preamble(en, zh), zh)

    def test_refusal_with_no_translation_is_not_rescued(self):
        # Nothing but meta-text: there is no translation to recover, so it must stay
        # recognizable for the caller's _META gate to drop it.
        zh = "抱歉，我无法完成这个翻译请求。"
        self.assertEqual(E.strip_meta_preamble("He was no longer CEO.", zh), zh)

    def test_multi_block_source_is_left_alone(self):
        # Only single-block sources license the "extra block = not translation" rule.
        en, zh = "First block.\n\nSecond block.", "第一段。\n\n以下是翻译："
        self.assertEqual(E.strip_meta_preamble(en, zh), zh)


class TestWorkerMemoryIsolation(unittest.TestCase):
    """--settings {disableAllHooks} stops hook-injected context but NOT CLAUDE.md
    auto-discovery: workers still read the user's ~/.claude/CLAUDE.md and one narrated
    that persona into a translation. --bare and CLAUDE_CONFIG_DIR both break
    OAuth/keychain auth ("Not logged in"); --setting-sources project plus a memory-free
    cwd isolates the worker and keeps auth working."""

    def _capture(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["kw"] = cmd, kwargs
            return types.SimpleNamespace(stdout='{"result": "好"}', stderr="", returncode=0)

        real = E.subprocess.run
        E.subprocess.run = fake_run
        try:
            E.claude_call("sys", "payload", "some-model", 5)
        finally:
            E.subprocess.run = real
        return seen

    def test_claude_call_passes_isolation_args(self):
        real = E.worker_isolation_args
        E.worker_isolation_args = lambda: ["--setting-sources", "project"]
        try:
            cmd = self._capture()["cmd"]
        finally:
            E.worker_isolation_args = real
        self.assertIn("--setting-sources", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project")

    def test_claude_call_runs_in_a_memory_free_directory(self):
        cwd = self._capture()["kw"]["cwd"]
        self.assertTrue(os.path.isdir(cwd))
        self.assertEqual(os.listdir(cwd), [])

    def test_user_settings_with_apikeyhelper_keep_their_settings(self):
        # Dropping user settings would drop apiKeyHelper-based auth. Working auth beats
        # memory isolation: those users keep --setting-sources off.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"apiKeyHelper": "/bin/echo key"}, f)
            self.assertTrue(E.user_settings_carry_auth(p))
            self.assertEqual(E.worker_isolation_args(p), [])

    def test_ordinary_user_settings_are_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"model": "sonnet"}, f)
            self.assertFalse(E.user_settings_carry_auth(p))
            self.assertEqual(E.worker_isolation_args(p), ["--setting-sources", "project"])

    def test_missing_or_broken_settings_file_is_not_auth(self):
        self.assertFalse(E.user_settings_carry_auth("/nonexistent/settings.json"))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertFalse(E.user_settings_carry_auth(p))


class TestShortSegmentLengthGuard(unittest.TestCase):
    """The han/word ratio test starts at 12 English words, so a chapter title carrying a
    leaked meta-commentary preamble used to sail through L1. Segments under 12 words now
    get an absolute bound (han > 4*words+10) no faithful title rendering approaches."""

    TITLE_EN = "3Watching Four of My Shipmates Drown"       # 6 words → bound 34 han

    def test_real_titles_stay_clean(self):
        for en, zh in [
            (self.TITLE_EN, "第三章　我目睹四名同伴溺水"),
            ("BOOKS BY", "作者作品"),
            ("CHRONICLE OF A DEATH FORETOLD", "《一桩事先张扬的凶杀案》"),
            ("The Story of This Story", "这本故事的由来"),
            ("U.S.S.R.", "苏维埃社会主义共和国联盟"),   # 12-han expansion of one word
        ]:
            self.assertEqual(E.check_l1(en, zh, {}), [], en)

    def test_unknown_preamble_tripped_by_length_alone(self):
        # Phrased to dodge every _META pattern — only the absolute bound catches it.
        zh = ("关于这段文字我需要先说明一下整体的处理思路和方法，然后再给出最终的结果，"
              "具体内容如下所示。第三章　我目睹四名同伴溺水")
        flags = E.check_l1(self.TITLE_EN, zh, {})
        self.assertTrue(any(f.startswith("too_long_abs") for f in flags), flags)
        self.assertNotIn("meta_leak", flags)

    def test_incident_preamble_flagged_as_meta(self):
        zh = ("我发现在文学翻译问答中，skills 框架不太适用。我将直接执行翻译。"
              "第三章　我目睹四名同伴溺水")
        self.assertIn("meta_leak", E.check_l1(self.TITLE_EN, zh, {}))


class TestWorkerIsolation(unittest.TestCase):
    """claude -p workers must not load user-level hooks/output styles: a globally
    installed plugin's SessionStart hook injected its framework docs into worker
    sessions, and the model's narrated reaction shipped inside a translation.
    claude_call pins --settings {disableAllHooks, outputStyle} on every call."""

    def test_claude_call_disables_hooks_and_output_style(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return types.SimpleNamespace(stdout='{"result": "好"}', stderr="", returncode=0)

        real = E.subprocess.run
        E.subprocess.run = fake_run
        try:
            out = E.claude_call("sys prompt", "payload", "some-model", 5)
        finally:
            E.subprocess.run = real
        self.assertEqual(out, "好")
        cmd = seen["cmd"]
        self.assertIn("--settings", cmd)
        cfg = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertIs(cfg["disableAllHooks"], True)
        self.assertEqual(cfg["outputStyle"], "default")


class TestInjectQaGate(unittest.TestCase):
    """qa_state='failed' rows (L2 judged unfaithful, L3 unrepaired) must never reach the
    book: inject keeps the English original and reports the exclusion loudly — they were
    previously injected silently while the summary claimed '0 skipped'."""

    XHTML = ('<?xml version="1.0" encoding="utf-8"?>\n'
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head>'
             '<body><p>Good paragraph here.</p><p>Bad paragraph here.</p></body></html>')

    def _inject(self, tmp, single):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        E.db_init(conn)
        rel = "OEBPS/c1.xhtml"
        os.makedirs(os.path.join(tmp, "OEBPS"))
        with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
            f.write(self.XHTML)
        conn.execute("INSERT INTO paragraphs(file, idx, en, sha, unit_id) VALUES(?,?,?,?,?)",
                     (rel, 0, "Good paragraph here.", "sha-good", 1))
        conn.execute("INSERT INTO paragraphs(file, idx, en, sha, unit_id) VALUES(?,?,?,?,?)",
                     (rel, 1, "Bad paragraph here.", "sha-bad", 1))
        conn.execute("INSERT INTO translations(sha, zh, qa_state) VALUES(?,?,?)",
                     ("sha-good", "好的段落在此。", "passed"))
        conn.execute("INSERT INTO translations(sha, zh, qa_state) VALUES(?,?,?)",
                     ("sha-bad", "这是幻觉译文。", "failed"))
        E.meta_set(conn, "source_type", "epub")
        opts = types.SimpleNamespace(single_translate=single, test_file=None,
                                     translation_style="color:#777;", base_font="",
                                     no_font_normalize=False)
        old = E.WORKDIR
        E.WORKDIR = tmp
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                E.cmd_inject(conn, opts)
        finally:
            E.WORKDIR = old
        with open(os.path.join(tmp, rel), encoding="utf-8") as f:
            return f.read(), buf.getvalue()

    def test_failed_row_not_injected_bilingual(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, log = self._inject(tmp, single=False)
        self.assertIn("好的段落在此。", out)
        self.assertNotIn("幻觉译文", out)
        self.assertIn("Bad paragraph here.", out)       # English original kept
        self.assertIn("+1", log)
        self.assertIn("QA-failed", log)                 # loud, not silent
        self.assertIn("English kept", log)

    def test_failed_row_keeps_english_in_single_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, log = self._inject(tmp, single=True)
        self.assertIn("好的段落在此。", out)            # passed row replaced in place
        self.assertNotIn("Good paragraph here.", out)
        self.assertIn("Bad paragraph here.", out)       # failed row: English stays
        self.assertNotIn("幻觉译文", out)
        self.assertIn("QA-failed", log)


class TestInjectRescuesPreambledTranslation(unittest.TestCase):
    """A leaked preamble used to cost the whole paragraph: inject saw _META, dropped the
    row, and shipped English-only even though the translation sat right after the blank
    line. inject now strips the commentary and injects the recovered translation; a row
    that is nothing BUT commentary is still dropped."""

    XHTML = ('<?xml version="1.0" encoding="utf-8"?>\n'
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head>'
             '<body><p>He was no longer CEO.</p><p>Nothing to see here.</p></body></html>')

    def _inject(self, tmp):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        E.db_init(conn)
        rel = "OEBPS/c1.xhtml"
        os.makedirs(os.path.join(tmp, "OEBPS"))
        with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
            f.write(self.XHTML)
        conn.execute("INSERT INTO paragraphs(file, idx, en, sha, unit_id) VALUES(?,?,?,?,?)",
                     (rel, 0, "He was no longer CEO.", "sha-pre", 1))
        conn.execute("INSERT INTO paragraphs(file, idx, en, sha, unit_id) VALUES(?,?,?,?,?)",
                     (rel, 1, "Nothing to see here.", "sha-junk", 1))
        conn.execute("INSERT INTO translations(sha, zh, qa_state) VALUES(?,?,?)",
                     ("sha-pre", "以下是翻译：\n\n他不再担任首席执行官。", "passed"))
        conn.execute("INSERT INTO translations(sha, zh, qa_state) VALUES(?,?,?)",
                     ("sha-junk", "抱歉，我无法完成这个翻译请求。", "passed"))
        E.meta_set(conn, "source_type", "epub")
        opts = types.SimpleNamespace(single_translate=False, test_file=None,
                                     translation_style="color:#777;", base_font="",
                                     no_font_normalize=False)
        old = E.WORKDIR
        E.WORKDIR = tmp
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                E.cmd_inject(conn, opts)
        finally:
            E.WORKDIR = old
        with open(os.path.join(tmp, rel), encoding="utf-8") as f:
            return f.read(), buf.getvalue()

    def test_preambled_row_ships_the_recovered_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = self._inject(tmp)
        self.assertIn("他不再担任首席执行官。", out)
        self.assertNotIn("以下是翻译", out)

    def test_pure_commentary_row_is_still_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, log = self._inject(tmp)
        self.assertNotIn("抱歉", out)
        self.assertIn("Nothing to see here.", out)      # English original kept
        self.assertIn("+1", log)                        # only the rescued row injected


class TestTranslateStripsPreambleBeforeStoring(unittest.TestCase):
    """Strip at the source too, so QA judges and the qa-report see the real translation
    rather than commentary the reader would never have gotten anyway."""

    def test_stored_translation_has_no_preamble(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        E.db_init(conn)
        conn.execute("INSERT INTO units(id, name, file) VALUES(1,'u1','OEBPS/c1.xhtml')")
        conn.execute("INSERT INTO paragraphs(file, idx, en, sha, unit_id) VALUES(?,?,?,?,?)",
                     ("OEBPS/c1.xhtml", 0, "He was no longer CEO.", "sha-pre", 1))
        E.meta_set(conn, "title", "T")
        opts = types.SimpleNamespace(model="m", unit_timeout=5, concurrency=1,
                                     max_attempts=2, test_file=None)
        real, old_gp = E.translate_robust, E.GLOSSARY_PATH
        E.translate_robust = lambda texts, sp, model, timeout: [
            "好，我会翻译这些段落。\n\n他不再担任首席执行官。"]
        try:
            with tempfile.TemporaryDirectory() as d:
                E.GLOSSARY_PATH = os.path.join(d, "glossary.json")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    E.cmd_translate(conn, opts)
        finally:
            E.translate_robust, E.GLOSSARY_PATH = real, old_gp
        zh = conn.execute("SELECT zh FROM translations WHERE sha='sha-pre'").fetchone()["zh"]
        self.assertEqual(zh, "他不再担任首席执行官。")


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


class TestNormalizeOpfLanguage(unittest.TestCase):
    """A repackaged EPUB must declare Chinese as its primary language so Kobo renders the
    (usually Chinese) shelf title with a CJK font instead of dropping glyphs to tofu (柳林□声)."""

    @staticmethod
    def _opf(langs):
        lang_els = "".join(f"<dc:language>{c}</dc:language>" for c in langs)
        return etree.fromstring(
            (f'<package xmlns="{E.OPF_NS}" version="2.0">'
             f'<metadata xmlns:dc="{E.DC_NS}">'
             f'<dc:identifier>bookid</dc:identifier>'
             f'<dc:title>柳林风声:THE WIND IN THE WILLOWS</dc:title>'
             f'{lang_els}</metadata><manifest/><spine/></package>').encode())

    def _langs(self, root):
        return [e.text for e in
                root.findall(f"{{{E.OPF_NS}}}metadata/{{{E.DC_NS}}}language")]

    def test_source_en_becomes_zh_then_en(self):
        root = self._opf(["en"])
        self.assertTrue(E.normalize_opf_language(root, False))
        self.assertEqual(self._langs(root), ["zh", "en"])
        # Kobo keys off the FIRST dc:language; zh must serialize ahead of en.
        xml = etree.tostring(root, encoding="unicode")
        self.assertLess(xml.index("<dc:language>zh"), xml.index("<dc:language>en"))

    def test_single_translate_declares_only_zh(self):
        root = self._opf(["en"])
        self.assertTrue(E.normalize_opf_language(root, True))
        self.assertEqual(self._langs(root), ["zh"])

    def test_dedupes_and_reorders_existing_languages(self):
        root = self._opf(["en", "en"])
        self.assertTrue(E.normalize_opf_language(root, False))
        self.assertEqual(self._langs(root), ["zh", "en"])

    def test_already_zh_primary_is_noop(self):
        root = self._opf(["zh", "en"])
        self.assertFalse(E.normalize_opf_language(root, False))
        self.assertEqual(self._langs(root), ["zh", "en"])

    def test_title_is_left_untouched(self):
        root = self._opf(["en"])
        E.normalize_opf_language(root, False)
        title = root.find(f"{{{E.OPF_NS}}}metadata/{{{E.DC_NS}}}title")
        self.assertEqual(title.text, "柳林风声:THE WIND IN THE WILLOWS")


if __name__ == "__main__":
    unittest.main()
