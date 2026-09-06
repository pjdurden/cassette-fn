# cassette-fn

Record real calls once, replay them deterministically in tests, while everything
around the call, including your control flow, keeps running for real. This is the
Python port of the JavaScript `cassette-fn` package.

## The problem

Testing anything that calls an LLM (or any other slow, expensive, or nondeterministic
function) usually means one of three bad options: pay for a real API call on every
test run and accept nondeterminism, hand-roll a mock object that quietly drifts from
the real provider response shape, or reach for an HTTP-level VCR library. The HTTP
option looks appealing but it freezes the whole call site: it intercepts the request
before your code ever sees it, so on replay your retry logic and control flow never
actually execute, and the test ends up verifying the recording instead of your code.
`cassette_fn` records at the function boundary you choose instead, so only the one
call that actually talks to a provider is faked.

## Install

```
pip install cassette-fn
```

## Usage

```python
from cassette_fn import tape

# The one function that actually talks to a provider. Real LLM SDK calls are
# keyword-based, e.g. client.messages.create(model=..., max_tokens=..., messages=...);
# wrap() records and replays both positional and keyword arguments.
def call_model(prompt, model="claude-opus-5", max_tokens=1024):
    response = requests.post(
        "https://api.example.com/v1/complete",
        json={"prompt": prompt, "model": model, "max_tokens": max_tokens},
    )
    return response.json()

t = tape(dir=".tapes", name="greeting-test")
model = t.wrap(call_model)

# First run (no cassette yet): calls through for real, records the result.
# Every run after that (cassette exists): replays from disk, call_model never runs.
result = model("say hi", model="claude-opus-5", max_tokens=1024)

t.save()  # writes .tapes/greeting-test.json (no-op in replay/off mode)
print(t.stats())  # Stats(hits=0, misses=1, recorded=1, mode='record')
```

Commit the `.tapes/*.json` file next to your tests. Delete it and re-run once (with a
real API key available) whenever you need to re-record.

Before committing a cassette, read it. `normalize` controls what is written to disk
(see `normalize` below): if your wrapped call carries an API key, an auth header, or
any other secret, strip it in `normalize` and confirm it is actually gone from the
saved file, rather than assuming it is. A cassette is plain, readable JSON for
exactly this reason.

## API

### `tape(*, dir=".tapes", name="default", mode="auto", normalize=None) -> Tape`

- `dir` (`str`) - cassette directory. Default `".tapes"`.
- `name` (`str`) - cassette file basename. File is `<dir>/<name>.json`. Default
  `"default"`.
- `mode` (`"auto" | "record" | "replay" | "off"`) - default `"auto"`.
  - `"auto"`: resolved once, at `tape()` construction, to `"replay"` if the cassette
    file already exists on disk, otherwise to `"record"`. This is a one-time decision
    for the whole `Tape` instance, not re-checked on every call.
  - `"record"`: always call through to the wrapped function and (over)write the
    cassette on `save()`.
  - `"replay"`: never call through. A lookup miss raises a `CassetteError` naming the
    missing key and the cassette path.
  - `"off"`: pass every call straight through, record nothing, `save()` is a no-op.
  - If the `CASSETTE_FN_MODE` environment variable is set, it overrides `mode`
    entirely. This is the same environment variable name the JavaScript version
    reads, so both implementations honor one setting.
- `normalize` (`Callable[[list, dict], tuple]`) - applied to a call's positional
  argument list and keyword argument dict before they are hashed into a lookup key
  **and before they are written to the cassette**. It receives `(args, kwargs)` and
  must return `(normalized_args, normalized_kwargs)`. Default: identity, i.e. `lambda
  args, kwargs: (args, kwargs)`. This is a load-bearing detail, not a convenience:
  since the *normalized* value, never the raw one, is what gets hashed and stored,
  `normalize` is the one place to
  - strip a volatile field (a timestamp, a request id) that would otherwise make an
    identical call miss on replay,
  - strip or mask a secret (an API key, an auth token) before it is ever written to
    disk, so it does not end up in a cassette file you commit, and
  - replace a non-serializable argument (an SDK client object, a file handle, a
    thread lock) with a serializable stand-in, since the wrapped function itself
    still receives the real, un-normalized arguments and is called normally; only
    what gets recorded is normalized.

Returns a `Tape`.

### `Tape.wrap(fn) -> Callable`

Wraps any ordinary synchronous callable, called with any mix of positional and
keyword arguments (e.g. `wrapped("say hi", model="claude-opus-5", max_tokens=1024)`),
since essentially every real LLM SDK call is keyword-based. Each call:

1. Computes `normalized_args, normalized_kwargs = normalize(args, kwargs)`, then:
   - if there are no keyword arguments, `key = sha256(json.dumps(normalized_args,
     separators=(",", ":"), ensure_ascii=False)).hexdigest()[:16]`. This is exactly
     the JavaScript implementation's key computation for the same positional
     arguments, so a cassette recorded by one implementation replays in the other
     (see "How it works").
   - if there are keyword arguments, they are folded in with their keys sorted so
     that argument order never affects the key: `key =
     sha256(json.dumps([normalized_args, sorted_kwargs], separators=(",", ":"),
     ensure_ascii=False)).hexdigest()[:16]`. `f(a=1, b=2)` and `f(b=2, a=1)` produce
     the same key.
2. In replay mode: looks up `key`, returns a deep copy of the stored result (so a
   caller mutating the returned value can never corrupt the cassette). Repeated
   identical calls replay the recorded entries for that key in the order they were
   recorded; once the list is exhausted, further calls keep repeating the last entry.
   A recorded thrown call is replayed as a freshly constructed `RuntimeError` with the
   same message (see "How it works" for why `RuntimeError` and not the original
   exception type). A missing key raises `CassetteError` immediately, naming the key
   and the cassette path.
3. In record mode: calls `fn(*args, **kwargs)` for real, with the original,
   un-normalized arguments, then appends `{"args": ..., "kwargs": ..., "result":
   ...}` (or, on a raised exception, `{"args": ..., "kwargs": ..., "error":
   {"message": ..., "name": ...}}`) to the ordered list under `key`, then returns (or
   re-raises) the real outcome. **The `args` and `kwargs` written to the entry are
   `normalized_args` and `normalized_kwargs`, the same values used to compute `key`,
   not the raw arguments the caller passed in.** The `"kwargs"` key is omitted
   entirely when the normalized kwargs are empty, so a purely positional call, or one
   whose kwargs were fully stripped by `normalize`, produces the same entry shape as
   the JavaScript version's cassettes. `args`, `kwargs`, and `result` are all
   deep-copied at record time, so mutating the caller's argument objects (including a
   `messages=[...]` list) after the call, or the returned result, can never corrupt
   the cassette.
4. In `"off"` mode: calls `fn(*args, **kwargs)` directly, no key is computed, nothing
   is recorded.

### `Tape.save() -> None`

Writes the cassette to disk, creating `dir` recursively if needed. No-op in
`"replay"` and `"off"` mode.

### `Tape.stats() -> Stats`

A frozen dataclass with fields:

- `hits` (`int`) - calls served from the cassette.
- `misses` (`int`) - calls not found in the cassette: every record-mode call (nothing
  is ever read back from the cache in record mode), plus replay-mode lookup misses. A
  replay miss increments `misses` before it raises, so a caller that catches the error
  still sees an accurate count.
- `recorded` (`int`) - entries written into the in-memory cassette during this session
  (always `0` in replay and off mode, since neither writes new entries).
- `mode` (`str`) - the resolved runtime mode (`"record" | "replay" | "off"`), never
  the literal `"auto"` input, since the whole point of resolving it once is to know
  which concrete behavior the instance is running.

### `Tape.keys() -> list[str]`

Keys currently present in the in-memory cassette (loaded entries in replay mode,
accumulated entries so far in record mode).

## Cassette file format

For a purely positional call, unchanged from the JavaScript version, so a cassette
file recorded by either implementation can be replayed by the other. Human-diffable
on purpose, so cassettes commit cleanly to git:

```json
{
  "version": 1,
  "name": "my-test",
  "entries": {
    "<key>": [ { "args": [ "..." ], "result": { "...": "..." } } ]
  }
}
```

An entry for a call made with keyword arguments carries a `"kwargs"` object as well:

```json
{
  "args": [ "say hi" ],
  "kwargs": { "model": "claude-opus-5", "max_tokens": 1024 },
  "result": { "...": "..." }
}
```

The `"kwargs"` key is only present when the call actually had keyword arguments; an
older entry (or one recorded from a purely positional call) has no `"kwargs"` key at
all, and still loads and replays correctly. An entry for a call that raised looks
like `{"args": [...], "error": {"message": "...", "name": "..."}}` (with `"kwargs"`
added the same way, when present) instead of `"result"`. Written with
`json.dump(data, indent=2, ensure_ascii=False)`.

## How it works

`cassette_fn` wraps one function you choose, the one that actually makes a network
call to a provider. It hashes the (optionally normalized) argument list into a short
key, and stores or replays call outcomes under that key in a plain JSON file.
Everything that calls the wrapped function is real code running for real on every
test run; only the wrapped function itself is faked on replay.

The key is computed with `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`
rather than the default `json.dumps(...)`, for two reasons that both matter for
cross-implementation cassette portability:

- The default separators insert a space after `,` and `:`; JavaScript's
  `JSON.stringify` never does, so the default would produce a different serialized
  string, and therefore a different key, for the same arguments.
- `json.dumps` defaults to `ensure_ascii=True`, which escapes every non-ASCII
  codepoint (`\uXXXX`); `JSON.stringify` never escapes non-ASCII. Left at the default,
  any argument containing an accent, a curly quote, CJK text, or an emoji, which is
  the common case for real LLM prompts, would silently hash to a different key in
  Python than in JavaScript. `ensure_ascii=False` fixes this; it has no effect on
  ASCII-only arguments, since there is nothing to escape either way.

Both were verified by hand, not just read from the code: a cassette recorded with the
real JavaScript implementation for a plain ASCII call was replayed successfully with
this Python implementation, and separately a cassette recorded for a call containing
accented Latin characters, a punctuation dash, CJK text, and an emoji was also
replayed successfully, in both cases a hit (not a miss), confirming the two
implementations produce identical keys for identical arguments, ASCII or not. The
same construction extends naturally to keyword arguments; see `Tape.wrap` above.

`normalize` runs once per call, on the raw `(args, kwargs)`, and its output,
`(normalized_args, normalized_kwargs)`, is used for both the lookup key and what gets
written to the cassette entry's `"args"`/`"kwargs"`. The wrapped function itself is
always called with the original, un-normalized arguments, since it may need the real
value (a real client, a real API key) to do its job. This means `normalize` is not
just a hashing convenience: it is the mechanism for keeping a secret out of a
committed cassette (strip or mask it in `normalize` and it never reaches disk), and
for wrapping a call that takes a non-serializable argument at all (an SDK client
object, a file handle, a thread lock all fail `copy.deepcopy`; replace one with a
serializable placeholder in `normalize` and the raw value is never copied). Because
this is security-relevant, do not just trust that `normalize` works: read the saved
cassette file, as suggested under Usage above.

A recorded exception is reconstructed on replay as a Python `RuntimeError` whose
message matches the original. The original exception's type name is preserved as a
`.name` attribute on the reconstructed `RuntimeError` (mirroring how the JavaScript
version sets `err.name`), since Python has no general way to reconstruct an arbitrary
exception class from a stored type name the way JavaScript can construct a plain
`Error` and overwrite its `.name`. This is the one spot where this port intentionally
does not reproduce the original exception type, only its message and recorded name.

Errors raised by `cassette_fn` itself (an invalid mode, a missing or unparseable
cassette file, a malformed cassette structure, a replay lookup miss) are raised as
`CassetteError`, a `RuntimeError` subclass, so `except RuntimeError` catches both
`cassette_fn`'s own errors and a reconstructed recorded exception.

Limits, honestly:

- The replay match key is the full argument list (after `normalize`). Two calls that
  differ only in a field you did not strip via `normalize` will not match, and you'll
  get a new recording (`"record"`/`"auto"`) or a miss (`"replay"`).
- `"auto"` mode's replay-vs-record decision is made once, at construction, from
  whether the cassette file exists at that moment. It does not re-check mid-run, and
  it does not merge new calls into an existing cassette; re-recording means deleting
  the file (or using `mode="record"`) and running again.
- No HTTP interception and no provider SDK integration. You choose the function
  boundary; if you wrap too high (e.g. a whole multi-step agent function) you lose
  the fine-grained determinism this library is for, and if you wrap too low you may
  end up wrapping something that is not the actual network call.
- Unlike the JavaScript version, which wraps `async` functions, this port wraps
  ordinary synchronous callables. Async support is intentionally not provided; if you
  need to record around an `async` call site, call the async function yourself and
  wrap a synchronous function that drives it (e.g. one that calls
  `asyncio.run(...)` internally), or wrap only the synchronous part of the call that
  actually needs recording.
- `Tape.wrap` expects a plain callable that returns once with a full result. Wrapping
  a function that returns a generator or other lazy/streaming object will record the
  object itself, not its eventual contents, and will not behave correctly on replay.

## Related

The JavaScript version of this package lives at the repository root:
[`cassette-fn` (JavaScript)](https://github.com/pjdurden/cassette-fn).

## License

MIT
