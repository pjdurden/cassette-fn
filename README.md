# agent-cassette

Record real LLM calls once, replay them deterministically in tests, while everything
around the call, including your tool execution and control flow, keeps running for real.

## The problem

Testing anything that calls an LLM usually means one of three bad options: pay for a
real API call on every test run and accept nondeterminism, hand-roll a mock object
that quietly drifts from the real provider response shape, or reach for an HTTP-level
VCR library. The HTTP option looks appealing but it freezes the whole agent loop: it
intercepts the request before your code ever sees it, so on replay your tool-calling
logic, retries, and control flow never actually execute, and the test ends up
verifying the recording instead of your code. `agent-cassette` records at the function
boundary you choose instead, so only the one call that actually talks to a provider
is faked.

## Install

```
npm i agent-cassette
```

## Usage

```js
import { tape } from 'agent-cassette';

// The one function that actually talks to a provider.
async function callModel(prompt) {
  const res = await fetch('https://api.example.com/v1/complete', {
    method: 'POST',
    body: JSON.stringify({ prompt }),
  });
  return res.json();
}

const t = tape({ dir: '.tapes', name: 'greeting-test' });
const model = t.wrap(callModel);

// First run (no cassette yet): calls through for real, records the result.
// Every run after that (cassette exists): replays from disk, callModel never runs.
const result = await model('say hi');

await t.save(); // writes .tapes/greeting-test.json (no-op in replay/off mode)
console.log(t.stats()); // { hits: 0, misses: 1, recorded: 1, mode: 'record' }
```

Commit the `.tapes/*.json` file next to your tests. Delete it and re-run once (with a
real API key available) whenever you need to re-record.

## API

### `tape(options?) -> Tape`

- `options.dir` (`string`) - cassette directory. Default `'.tapes'`.
- `options.name` (`string`) - cassette file basename. File is `<dir>/<name>.json`.
  Default `'default'`.
- `options.mode` (`'auto' | 'record' | 'replay' | 'off'`) - default `'auto'`.
  - `'auto'`: resolved once, at `tape()` construction, to `'replay'` if the cassette
    file already exists on disk, otherwise to `'record'`. This is a one-time decision
    for the whole `Tape` instance, not re-checked on every call.
  - `'record'`: always call through to the wrapped function and (over)write the
    cassette on `save()`.
  - `'replay'`: never call through. A lookup miss throws an `Error` naming the missing
    key and the cassette path.
  - `'off'`: pass every call straight through, record nothing, `save()` is a no-op.
  - If `process.env.AGENT_CASSETTE_MODE` is set, it overrides `options.mode` entirely.
- `options.normalize` (`(args: any[]) => any`) - applied to a call's argument array
  before it is hashed into a lookup key, so callers can strip volatile fields
  (timestamps, request ids, an `apiKey`) that would otherwise make every call miss.
  Default: identity.

Returns a `Tape`.

### `tape.wrap(fn) -> wrappedFn`

Wraps any `async` function. Each call:

1. Computes `key = sha256(JSON.stringify(normalize(args))).hex.slice(0, 16)`.
2. In replay mode: looks up `key`, returns a deep clone of the stored result (so a
   caller mutating the returned value can never corrupt the cassette). Repeated
   identical calls replay the recorded entries for that key in the order they were
   recorded; once the list is exhausted, further calls keep repeating the last entry.
   A recorded thrown call is replayed as a freshly constructed `Error` with the same
   `message` and `name`. A missing key throws immediately, naming the key and the
   cassette path.
3. In record mode: calls `fn(...args)` for real and appends `{ args, result }` (or, on
   a thrown error, `{ args, error: { message, name } }`) to the ordered list under
   `key`, then returns (or rethrows) the real outcome.
4. In `'off'` mode: calls `fn(...args)` directly, no key is computed, nothing is
   recorded.

### `tape.save() -> Promise<void>`

Writes the cassette to disk, creating `dir` recursively if needed. No-op in `'replay'`
and `'off'` mode.

### `tape.stats() -> { hits, misses, recorded, mode }`

- `hits` - calls served from the cassette.
- `misses` - calls not found in the cassette: every record-mode call (nothing is ever
  read back from the cache in record mode), plus replay-mode lookup misses. A replay
  miss increments `misses` before it throws, so a caller that catches the error still
  sees an accurate count.
- `recorded` - entries written into the in-memory cassette during this session
  (always `0` in replay and off mode, since neither writes new entries).
- `mode` - the **resolved** runtime mode (`'record' | 'replay' | 'off'`), never the
  literal `'auto'` input, since the whole point of resolving it once is to know which
  concrete behavior the instance is running.

### `tape.keys() -> string[]`

Keys currently present in the in-memory cassette (loaded entries in replay mode,
accumulated entries so far in record mode).

## Cassette file format

Human-diffable on purpose, so cassettes commit cleanly to git:

```json
{
  "version": 1,
  "name": "my-test",
  "entries": {
    "<key>": [ { "args": [ "..." ], "result": { "...": "..." } } ]
  }
}
```

An entry for a call that threw looks like `{ "args": [...], "error": { "message":
"...", "name": "..." } }` instead of `"result"`. Written with
`JSON.stringify(data, null, 2)`.

## How it works

`agent-cassette` wraps one function you choose, the one that actually makes a network call
to a provider. It hashes the (optionally normalized) argument list into a short key,
and stores or replays call outcomes under that key in a plain JSON file. Everything
that calls the wrapped function, your agent loop, tool execution, retries, is real
code running for real on every test run; only the wrapped function itself is faked on
replay.

Limits, honestly:

- The replay match key is the full argument list (after `normalize`). Two calls that
  differ only in a field you did not strip via `normalize` will not match, and you'll
  get a new recording (`'record'`/`'auto'`) or a miss (`'replay'`).
- `'auto'` mode's replay-vs-record decision is made once, at construction, from
  whether the cassette file exists at that moment. It does not re-check mid-run, and
  it does not merge new calls into an existing cassette; re-recording means deleting
  the file (or using `mode: 'record'`) and running again.
- No HTTP interception and no provider SDK integration. You choose the function
  boundary; if you wrap too high (e.g. a whole multi-step agent function) you lose
  the fine-grained determinism this library is for, and if you wrap too low you may
  end up wrapping something that is not the actual network call.
- Streaming responses are deliberately unsupported in 0.1.0. `tape.wrap` expects a
  plain `async function` that resolves once with a full result; wrapping a function
  that returns a stream or async iterator will record the stream object itself, not
  its eventual contents, and will not behave correctly on replay.

## License

MIT
