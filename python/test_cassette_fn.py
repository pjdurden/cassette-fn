"""Tests for cassette_fn, ported one for one from the JavaScript test suite
at ../test/tape.test.js."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from cassette_fn import CassetteError, tape


class CassetteFnTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="cassette-fn-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # 1. record mode: calls through, save() writes the documented cassette format
    def test_record_mode_writes_documented_format(self) -> None:
        # nested dir exercises recursive mkdir in save()
        dir_path = os.path.join(self.tmp_dir, "nested", "cassettes")
        call_count = 0

        def fn(x: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"y": x * 2}

        t = tape(dir=dir_path, name="rec1", mode="record")
        wrapped = t.wrap(fn)

        result = wrapped(5)
        self.assertEqual(call_count, 1)
        self.assertEqual(result, {"y": 10})

        t.save()
        file_path = os.path.join(dir_path, "rec1.json")
        self.assertTrue(os.path.exists(file_path))

        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["name"], "rec1")
        keys = list(parsed["entries"].keys())
        self.assertEqual(len(keys), 1)
        self.assertEqual(len(parsed["entries"][keys[0]]), 1)
        self.assertEqual(parsed["entries"][keys[0]][0]["args"], [5])
        self.assertEqual(parsed["entries"][keys[0]][0]["result"], {"y": 10})

    # 2. auto mode with an existing cassette replays without calling through
    def test_auto_mode_with_existing_cassette_replays(self) -> None:
        rec = tape(dir=self.tmp_dir, name="auto1", mode="record")
        rec_wrapped = rec.wrap(lambda x: {"y": x * 2})
        rec_wrapped(5)
        rec.save()

        called = 0

        def fn(x: int) -> dict:
            nonlocal called
            called += 1
            return {"y": x * 2}

        auto = tape(dir=self.tmp_dir, name="auto1", mode="auto")
        auto_wrapped = auto.wrap(fn)

        result = auto_wrapped(5)
        self.assertEqual(called, 0)
        self.assertEqual(result, {"y": 10})
        self.assertEqual(auto.stats().mode, "replay")

    # 3. auto mode with no existing cassette resolves to record
    def test_auto_mode_with_no_existing_cassette_resolves_to_record(self) -> None:
        called = 0

        def fn(x: int) -> dict:
            nonlocal called
            called += 1
            return {"y": x * 3}

        auto = tape(dir=self.tmp_dir, name="auto2", mode="auto")
        auto_wrapped = auto.wrap(fn)

        result = auto_wrapped(4)
        self.assertEqual(called, 1)
        self.assertEqual(result, {"y": 12})
        self.assertEqual(auto.stats().mode, "record")

    # 4. replay mode with a missing key throws, naming the key and cassette path
    def test_replay_missing_key_names_key_and_path(self) -> None:
        rec = tape(dir=self.tmp_dir, name="miss1", mode="record")
        rec.wrap(lambda x: x)(1)
        rec.save()

        replay = tape(dir=self.tmp_dir, name="miss1", mode="replay")

        def should_never_run() -> None:
            raise AssertionError("should never be called in replay mode")

        wrapped = replay.wrap(should_never_run)

        cassette_path = os.path.join(self.tmp_dir, "miss1.json")
        with self.assertRaises(CassetteError) as ctx:
            wrapped(999)
        self.assertIn("no recording found for key", str(ctx.exception))
        self.assertIn(cassette_path, str(ctx.exception))

    # 5. replayed values are deep-cloned: mutating one replay does not affect the next
    def test_replayed_values_are_deep_cloned(self) -> None:
        rec = tape(dir=self.tmp_dir, name="clone1", mode="record")
        rec.wrap(lambda x: {"a": 1, "nested": {"b": x}})(1)
        rec.save()

        replay = tape(dir=self.tmp_dir, name="clone1", mode="replay")
        wrapped = replay.wrap(lambda x: {"a": 1, "nested": {"b": x}})

        r1 = wrapped(1)
        r1["nested"]["b"] = 999
        r2 = wrapped(1)
        self.assertEqual(r2["nested"]["b"], 1)

    # 6. recorded args are deep-cloned: mutating the caller object after the
    #    call does not corrupt the cassette (regression test for the JS bug)
    def test_recorded_args_are_deep_cloned(self) -> None:
        rec = tape(dir=self.tmp_dir, name="argclone1", mode="record")
        wrapped = rec.wrap(lambda obj: {"echo": obj["q"]})

        arg_obj = {"q": "original"}
        wrapped(arg_obj)
        arg_obj["q"] = "mutated-after-call"
        rec.save()

        file_path = os.path.join(self.tmp_dir, "argclone1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        only_key = next(iter(parsed["entries"]))
        self.assertEqual(parsed["entries"][only_key][0]["args"], [{"q": "original"}])
        self.assertEqual(parsed["entries"][only_key][0]["result"], {"echo": "original"})

    # 7. a recorded thrown error is re-thrown on replay with the same message and name
    def test_recorded_error_rethrown_on_replay(self) -> None:
        rec = tape(dir=self.tmp_dir, name="err1", mode="record")

        def raise_boom() -> None:
            err = ValueError("boom")
            raise err

        rec_wrapped = rec.wrap(raise_boom)
        with self.assertRaises(ValueError) as ctx:
            rec_wrapped()
        self.assertEqual(str(ctx.exception), "boom")
        rec.save()

        replay = tape(dir=self.tmp_dir, name="err1", mode="replay")
        replay_wrapped = replay.wrap(lambda: "unused")
        with self.assertRaises(RuntimeError) as ctx2:
            replay_wrapped()
        self.assertEqual(str(ctx2.exception), "boom")
        self.assertEqual(getattr(ctx2.exception, "name", None), "ValueError")

    # 8. two identical calls record two ordered entries; replay in order, then repeat the last
    def test_two_identical_calls_replay_in_order_then_repeat(self) -> None:
        rec = tape(dir=self.tmp_dir, name="order1", mode="record")
        counter = 0

        def fn(x: int) -> dict:
            nonlocal counter
            n = counter
            counter += 1
            return {"n": n, "x": x}

        rec_wrapped = rec.wrap(fn)
        rec_wrapped(7)
        rec_wrapped(7)
        rec.save()

        cassette_path = os.path.join(self.tmp_dir, "order1.json")
        with open(cassette_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        only_key = next(iter(parsed["entries"]))
        self.assertEqual(len(parsed["entries"][only_key]), 2)

        replay = tape(dir=self.tmp_dir, name="order1", mode="replay")

        def unused() -> None:
            raise AssertionError("unused")

        replay_wrapped = replay.wrap(unused)
        first = replay_wrapped(7)
        second = replay_wrapped(7)
        third = replay_wrapped(7)  # list exhausted, repeats the last entry

        self.assertEqual(first["n"], 0)
        self.assertEqual(second["n"], 1)
        self.assertEqual(third["n"], 1)

    # 9. normalize strips a volatile field so a different value at that field still hits
    def test_normalize_strips_volatile_field(self) -> None:
        def normalize(args, kwargs):
            payload = args[0]
            rest = {k: v for k, v in payload.items() if k != "ts"}
            return [rest], kwargs

        rec = tape(dir=self.tmp_dir, name="norm1", mode="record", normalize=normalize)
        rec.wrap(lambda payload: {"echo": payload["q"]})({"q": "hi", "ts": 111})
        rec.save()

        called = 0

        def fn(payload):
            nonlocal called
            called += 1
            return {"echo": payload["q"]}

        replay = tape(dir=self.tmp_dir, name="norm1", mode="replay", normalize=normalize)
        replay_wrapped = replay.wrap(fn)

        result = replay_wrapped({"q": "hi", "ts": 999})
        self.assertEqual(called, 0)
        self.assertEqual(result, {"echo": "hi"})
        self.assertEqual(replay.stats().hits, 1)

    # 10. replay mode save() is a no-op and never overwrites the cassette
    def test_replay_save_is_noop(self) -> None:
        rec = tape(dir=self.tmp_dir, name="replaysave1", mode="record")
        rec.wrap(lambda x: x * 2)(5)
        rec.save()

        file_path = os.path.join(self.tmp_dir, "replaysave1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            before = f.read()
        mtime_before = os.stat(file_path).st_mtime_ns

        replay = tape(dir=self.tmp_dir, name="replaysave1", mode="replay")

        def should_never_run(x: int) -> int:
            raise AssertionError("should never be called in replay mode")

        replay.wrap(should_never_run)(5)
        replay.save()

        with open(file_path, "r", encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(after, before)
        self.assertEqual(os.stat(file_path).st_mtime_ns, mtime_before)

    # 11. off mode records nothing and save() writes no file
    def test_off_mode_records_nothing(self) -> None:
        called = 0

        def fn(x: int) -> int:
            nonlocal called
            called += 1
            return x + 1

        t = tape(dir=self.tmp_dir, name="off1", mode="off")
        wrapped = t.wrap(fn)

        result = wrapped(3)
        self.assertEqual(called, 1)
        self.assertEqual(result, 4)

        t.save()
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "off1.json")))
        stats = t.stats()
        self.assertEqual((stats.hits, stats.misses, stats.recorded, stats.mode), (0, 0, 0, "off"))

    # 12. stats() tracks hits and misses correctly, including a replay miss caught by the caller
    def test_stats_tracks_hits_and_misses(self) -> None:
        rec = tape(dir=self.tmp_dir, name="stats1", mode="record")
        rec_wrapped = rec.wrap(lambda x: x * 10)
        rec_wrapped(1)
        rec_wrapped(2)
        stats = rec.stats()
        self.assertEqual((stats.hits, stats.misses, stats.recorded, stats.mode), (0, 2, 2, "record"))
        rec.save()

        replay = tape(dir=self.tmp_dir, name="stats1", mode="replay")
        replay_wrapped = replay.wrap(lambda x: x * 10)

        replay_wrapped(1)  # hit
        with self.assertRaises(CassetteError):
            replay_wrapped(999)  # miss, but recorded before throwing
        replay_wrapped(1)  # hit (repeat of the single recorded entry)

        stats2 = replay.stats()
        self.assertEqual((stats2.hits, stats2.misses, stats2.recorded, stats2.mode), (2, 1, 0, "replay"))

    # 13. a malformed cassette file throws a clear Error naming the path at construction
    def test_malformed_cassette_raises_at_construction(self) -> None:
        os.makedirs(self.tmp_dir, exist_ok=True)
        file_path = os.path.join(self.tmp_dir, "broken.json")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")

        with self.assertRaises(CassetteError) as ctx:
            tape(dir=self.tmp_dir, name="broken", mode="replay")
        self.assertIn(file_path, str(ctx.exception))

    # 14. keys() reflects the current in-memory cassette
    def test_keys_reflects_in_memory_cassette(self) -> None:
        t = tape(dir=self.tmp_dir, name="keys1", mode="record")
        self.assertEqual(t.keys(), [])
        wrapped = t.wrap(lambda x: x)
        wrapped(1)
        wrapped(2)
        self.assertEqual(len(t.keys()), 2)

    # 15. a call with only keyword arguments records and replays correctly
    def test_kwargs_only_records_and_replays(self) -> None:
        rec = tape(dir=self.tmp_dir, name="kwonly1", mode="record")
        rec_wrapped = rec.wrap(lambda **kwargs: {"model": kwargs["model"], "max_tokens": kwargs["max_tokens"]})
        result = rec_wrapped(model="claude-opus-5", max_tokens=1024)
        self.assertEqual(result, {"model": "claude-opus-5", "max_tokens": 1024})
        rec.save()

        called = False

        def fn(**kwargs: object) -> None:
            nonlocal called
            called = True
            raise AssertionError("should never be called in replay mode")

        replay = tape(dir=self.tmp_dir, name="kwonly1", mode="replay")
        replay_wrapped = replay.wrap(fn)
        result2 = replay_wrapped(model="claude-opus-5", max_tokens=1024)
        self.assertFalse(called)
        self.assertEqual(result2, {"model": "claude-opus-5", "max_tokens": 1024})
        self.assertEqual(replay.stats().hits, 1)

    # 16. a mix of positional and keyword arguments records and replays correctly
    def test_mixed_positional_and_keyword_records_and_replays(self) -> None:
        def fn(prompt: str, model: str = "default", temperature: float = 0) -> dict:
            return {"prompt": prompt, "model": model, "temperature": temperature}

        rec = tape(dir=self.tmp_dir, name="mixed1", mode="record")
        rec_wrapped = rec.wrap(fn)
        recorded_result = rec_wrapped("say hi", model="claude-opus-5", temperature=0.5)
        rec.save()

        called = False

        def should_not_run(prompt: str, model: str = "default", temperature: float = 0) -> None:
            nonlocal called
            called = True
            raise AssertionError("should never be called in replay mode")

        replay = tape(dir=self.tmp_dir, name="mixed1", mode="replay")
        replay_wrapped = replay.wrap(should_not_run)
        replayed_result = replay_wrapped("say hi", model="claude-opus-5", temperature=0.5)
        self.assertFalse(called)
        self.assertEqual(replayed_result, recorded_result)
        self.assertEqual(replay.stats().hits, 1)

    # 17. f(a=1, b=2) and f(b=2, a=1) produce the same key; the second call is a replay hit
    def test_kwargs_order_independent_key(self) -> None:
        rec = tape(dir=self.tmp_dir, name="kworder1", mode="record")
        rec_wrapped = rec.wrap(lambda **kwargs: {"a": kwargs["a"], "b": kwargs["b"]})
        rec_wrapped(a=1, b=2)
        rec.save()

        # Confirm only one key was recorded and hand-verify order-independence
        # against the raw cassette on disk as well as the replay path below.
        file_path = os.path.join(self.tmp_dir, "kworder1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        self.assertEqual(len(parsed["entries"]), 1)

        called = False

        def fn(**kwargs: object) -> None:
            nonlocal called
            called = True
            raise AssertionError("should never be called in replay mode")

        replay = tape(dir=self.tmp_dir, name="kworder1", mode="replay")
        replay_wrapped = replay.wrap(fn)
        result = replay_wrapped(b=2, a=1)  # reversed order from how it was recorded
        self.assertFalse(called)
        self.assertEqual(result, {"a": 1, "b": 2})
        self.assertEqual(replay.stats().hits, 1)

    # 18. mutating a dict/list passed as a keyword argument after the call does not corrupt the cassette
    def test_kwargs_are_deep_cloned_at_record_time(self) -> None:
        rec = tape(dir=self.tmp_dir, name="kwmut1", mode="record")
        wrapped = rec.wrap(lambda **kwargs: {"echo": list(kwargs["messages"])})

        messages = [{"role": "user", "content": "hi"}]
        wrapped(messages=messages)
        # Mutate the list and a nested dict inside it after the call returns,
        # exactly like a caller reusing/mutating a `messages=[...]` list.
        messages.append({"role": "user", "content": "appended-after-call"})
        messages[0]["content"] = "mutated-after-call"
        rec.save()

        file_path = os.path.join(self.tmp_dir, "kwmut1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        only_key = next(iter(parsed["entries"]))
        stored_kwargs = parsed["entries"][only_key][0]["kwargs"]
        self.assertEqual(stored_kwargs, {"messages": [{"role": "user", "content": "hi"}]})

    # 19. an old-format cassette entry with no "kwargs" key still replays
    def test_old_format_entry_without_kwargs_key_replays(self) -> None:
        # A positional-only call never gets a "kwargs" key written (this is
        # exactly what a hand-written or JS-recorded cassette looks like),
        # so recording one and inspecting it doubles as the fixture.
        rec = tape(dir=self.tmp_dir, name="oldfmt1", mode="record")
        rec.wrap(lambda x: {"y": x * 2})(7)
        rec.save()

        file_path = os.path.join(self.tmp_dir, "oldfmt1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        only_key = next(iter(parsed["entries"]))
        self.assertNotIn("kwargs", parsed["entries"][only_key][0])

        called = False

        def fn(x: int) -> None:
            nonlocal called
            called = True
            raise AssertionError("should never be called in replay mode")

        replay = tape(dir=self.tmp_dir, name="oldfmt1", mode="replay")
        result = replay.wrap(fn)(7)
        self.assertFalse(called)
        self.assertEqual(result, {"y": 14})

    # 20. the positional-only key is unchanged: matches the literal hex key
    # produced by the real JavaScript implementation for the same call
    # (verified by hand: wrapped(5, {"q": "hello world", "ts": 111}) recorded
    # with `tape` from ../index.js produced this exact key).
    def test_positional_only_key_matches_js_reference(self) -> None:
        rec = tape(dir=self.tmp_dir, name="jsparity1", mode="record")
        rec.wrap(lambda x, payload: {"y": x * 2, "echoed": payload["q"]})(5, {"q": "hello world", "ts": 111})
        rec.save()

        file_path = os.path.join(self.tmp_dir, "jsparity1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        (only_key,) = parsed["entries"].keys()
        self.assertEqual(only_key, "742d2e2e6d3841b1")

    # 21. a non-ASCII argument (accents, CJK, an astral-plane emoji) produces
    # the same key as the real JavaScript implementation. json.dumps defaults
    # to ensure_ascii=True, which escapes non-ASCII codepoints and would
    # silently break cross-implementation replay for any call whose
    # arguments are not plain ASCII (e.g. most real LLM prompts); this
    # asserts the fix (ensure_ascii=False) rather than merely the type check.
    def test_unicode_argument_key_matches_js_reference(self) -> None:
        text = "héllo wörld — café 日本語 emoji \U0001f389 test"
        rec = tape(dir=self.tmp_dir, name="unicode1", mode="record")
        rec.wrap(lambda x: {"echo": x})(text)
        rec.save()

        file_path = os.path.join(self.tmp_dir, "unicode1.json")
        with open(file_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        (only_key,) = parsed["entries"].keys()
        # Verified by hand against the real JS implementation: recording
        # `wrapped(text)` with `tape` from ../index.js produced this exact
        # key.
        self.assertEqual(only_key, "d818548c9ae2df14")


if __name__ == "__main__":
    unittest.main()
