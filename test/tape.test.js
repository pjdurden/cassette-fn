import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, existsSync, readFileSync, writeFileSync, mkdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { tape } from '../index.js';

/**
 * Run `fn(dir)` against a fresh temp directory, then remove it, even on failure.
 * @param {(dir: string) => Promise<void>} fn
 */
async function withTmpDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'cassette-fn-test-'));
  try {
    await fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('record mode: calls through, save() writes the documented cassette format', async () => {
  await withTmpDir(async (root) => {
    const dir = join(root, 'nested', 'cassettes'); // exercises recursive mkdir in save()
    let callCount = 0;
    const t = tape({ dir, name: 'rec1', mode: 'record' });
    const wrapped = t.wrap(async (x) => {
      callCount += 1;
      return { y: x * 2 };
    });

    const result = await wrapped(5);
    assert.equal(callCount, 1);
    assert.deepEqual(result, { y: 10 });

    await t.save();
    const filePath = join(dir, 'rec1.json');
    assert.equal(existsSync(filePath), true);

    const parsed = JSON.parse(readFileSync(filePath, 'utf8'));
    assert.equal(parsed.version, 1);
    assert.equal(parsed.name, 'rec1');
    const keys = Object.keys(parsed.entries);
    assert.equal(keys.length, 1);
    assert.equal(parsed.entries[keys[0]].length, 1);
    assert.deepEqual(parsed.entries[keys[0]][0].args, [5]);
    assert.deepEqual(parsed.entries[keys[0]][0].result, { y: 10 });
  });
});

test('auto mode with an existing cassette replays without calling through', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'auto1', mode: 'record' });
    const recWrapped = rec.wrap(async (x) => ({ y: x * 2 }));
    await recWrapped(5);
    await rec.save();

    let called = 0;
    const auto = tape({ dir, name: 'auto1', mode: 'auto' });
    const autoWrapped = auto.wrap(async (x) => {
      called += 1;
      return { y: x * 2 };
    });

    const result = await autoWrapped(5);
    assert.equal(called, 0);
    assert.deepEqual(result, { y: 10 });
    assert.equal(auto.stats().mode, 'replay');
  });
});

test('auto mode with no existing cassette resolves to record', async () => {
  await withTmpDir(async (dir) => {
    let called = 0;
    const auto = tape({ dir, name: 'auto2', mode: 'auto' });
    const autoWrapped = auto.wrap(async (x) => {
      called += 1;
      return { y: x * 3 };
    });

    const result = await autoWrapped(4);
    assert.equal(called, 1);
    assert.deepEqual(result, { y: 12 });
    assert.equal(auto.stats().mode, 'record');
  });
});

test('replay mode with a missing key throws, naming the key and cassette path', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'miss1', mode: 'record' });
    await rec.wrap(async (x) => x)(1);
    await rec.save();

    const replay = tape({ dir, name: 'miss1', mode: 'replay' });
    const wrapped = replay.wrap(async () => {
      throw new Error('should never be called in replay mode');
    });

    const cassettePath = join(dir, 'miss1.json');
    await assert.rejects(
      () => wrapped(999),
      (err) => {
        assert.match(err.message, /no recording found for key/);
        assert.ok(err.message.includes(cassettePath), 'error should name the cassette path');
        return true;
      }
    );
  });
});

test('replayed values are deep-cloned: mutating one replay does not affect the next', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'clone1', mode: 'record' });
    await rec.wrap(async (x) => ({ a: 1, nested: { b: x } }))(1);
    await rec.save();

    const replay = tape({ dir, name: 'clone1', mode: 'replay' });
    const wrapped = replay.wrap(async (x) => ({ a: 1, nested: { b: x } }));

    const r1 = await wrapped(1);
    r1.nested.b = 999;
    const r2 = await wrapped(1);
    assert.equal(r2.nested.b, 1);
  });
});

test('recorded args are deep-cloned: mutating the caller object after the call does not corrupt the cassette', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'argclone1', mode: 'record' });
    const wrapped = rec.wrap(async (obj) => ({ echo: obj.q }));

    const argObj = { q: 'original' };
    await wrapped(argObj);
    argObj.q = 'mutated-after-call';
    await rec.save();

    const filePath = join(dir, 'argclone1.json');
    const parsed = JSON.parse(readFileSync(filePath, 'utf8'));
    const [onlyKey] = Object.keys(parsed.entries);
    assert.deepEqual(parsed.entries[onlyKey][0].args, [{ q: 'original' }]);
    assert.deepEqual(parsed.entries[onlyKey][0].result, { echo: 'original' });
  });
});

test('a recorded thrown error is re-thrown on replay with the same message and name', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'err1', mode: 'record' });
    const recWrapped = rec.wrap(async () => {
      const err = new Error('boom');
      err.name = 'CustomError';
      throw err;
    });
    await assert.rejects(recWrapped(), { message: 'boom', name: 'CustomError' });
    await rec.save();

    const replay = tape({ dir, name: 'err1', mode: 'replay' });
    const replayWrapped = replay.wrap(async () => 'unused');
    await assert.rejects(replayWrapped(), (err) => {
      assert.equal(err.message, 'boom');
      assert.equal(err.name, 'CustomError');
      return true;
    });
  });
});

test('two identical calls record two ordered entries; replay in order, then repeat the last', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'order1', mode: 'record' });
    let counter = 0;
    const recWrapped = rec.wrap(async (x) => ({ n: counter++, x }));
    await recWrapped(7);
    await recWrapped(7);
    await rec.save();

    const cassettePath = join(dir, 'order1.json');
    const parsed = JSON.parse(readFileSync(cassettePath, 'utf8'));
    const [onlyKey] = Object.keys(parsed.entries);
    assert.equal(parsed.entries[onlyKey].length, 2);

    const replay = tape({ dir, name: 'order1', mode: 'replay' });
    const replayWrapped = replay.wrap(async () => {
      throw new Error('unused');
    });
    const first = await replayWrapped(7);
    const second = await replayWrapped(7);
    const third = await replayWrapped(7); // list exhausted, repeats the last entry

    assert.equal(first.n, 0);
    assert.equal(second.n, 1);
    assert.equal(third.n, 1);
  });
});

test('normalize strips a volatile field so a different value at that field still hits', async () => {
  await withTmpDir(async (dir) => {
    const normalize = (args) => {
      const [payload] = args;
      const { ts, ...rest } = payload;
      return [rest];
    };

    const rec = tape({ dir, name: 'norm1', mode: 'record', normalize });
    await rec.wrap(async (payload) => ({ echo: payload.q }))({ q: 'hi', ts: 111 });
    await rec.save();

    let called = 0;
    const replay = tape({ dir, name: 'norm1', mode: 'replay', normalize });
    const replayWrapped = replay.wrap(async (payload) => {
      called += 1;
      return { echo: payload.q };
    });

    const result = await replayWrapped({ q: 'hi', ts: 999 });
    assert.equal(called, 0);
    assert.deepEqual(result, { echo: 'hi' });
    assert.equal(replay.stats().hits, 1);
  });
});

test('normalize that strips a secret field keeps that value out of the saved cassette file', async () => {
  await withTmpDir(async (dir) => {
    const normalize = (args) =>
      args.map((a) => (a && typeof a === 'object' ? { ...a, apiKey: undefined } : a));

    const t = tape({ dir, name: 'leak', mode: 'record', normalize });
    const f = t.wrap(async () => ({ ok: true }));
    await f({ prompt: 'hi', apiKey: 'sk-ant-SUPERSECRET-do-not-commit' });
    await t.save();

    const filePath = join(dir, 'leak.json');
    const fileText = readFileSync(filePath, 'utf8');
    assert.ok(
      !fileText.includes('sk-ant-SUPERSECRET-do-not-commit'),
      'the secret must not appear anywhere in the saved cassette file'
    );

    const parsed = JSON.parse(fileText);
    const [onlyKey] = Object.keys(parsed.entries);
    // JSON.stringify drops keys whose value is `undefined`, so the stored args
    // should have no apiKey property at all, not merely an empty one.
    assert.deepEqual(parsed.entries[onlyKey][0].args, [{ prompt: 'hi' }]);
    assert.ok(!('apiKey' in parsed.entries[onlyKey][0].args[0]));
  });
});

test('normalize that rewrites a value stores the rewritten value, not the original', async () => {
  await withTmpDir(async (dir) => {
    const normalize = (args) => args.map((a) => (typeof a === 'string' ? a.toUpperCase() : a));

    const t = tape({ dir, name: 'rewrite1', mode: 'record', normalize });
    await t.wrap(async (x) => ({ echo: x }))('hello');
    await t.save();

    const parsed = JSON.parse(readFileSync(join(dir, 'rewrite1.json'), 'utf8'));
    const [onlyKey] = Object.keys(parsed.entries);
    assert.deepEqual(parsed.entries[onlyKey][0].args, ['HELLO']);
  });
});

test('with no normalize supplied, stored args are unchanged (not a regression)', async () => {
  await withTmpDir(async (dir) => {
    const t = tape({ dir, name: 'nonorm1', mode: 'record' });
    await t.wrap(async (payload) => ({ echo: payload.q }))({ q: 'hi', ts: 111 });
    await t.save();

    const parsed = JSON.parse(readFileSync(join(dir, 'nonorm1.json'), 'utf8'));
    const [onlyKey] = Object.keys(parsed.entries);
    assert.deepEqual(parsed.entries[onlyKey][0].args, [{ q: 'hi', ts: 111 }]);
  });
});

test('the positional-only ASCII reference key is unchanged (JS/Python cross-implementation parity)', async () => {
  await withTmpDir(async (dir) => {
    const t = tape({ dir, name: 'jsparity1', mode: 'record' });
    await t.wrap(async (x, payload) => ({ y: x * 2, echoed: payload.q }))(5, {
      q: 'hello world',
      ts: 111,
    });
    await t.save();

    const parsed = JSON.parse(readFileSync(join(dir, 'jsparity1.json'), 'utf8'));
    const [onlyKey] = Object.keys(parsed.entries);
    // This exact hex value is the reference key checked against by the Python
    // port's test_positional_only_key_matches_js_reference. It must never change.
    assert.equal(onlyKey, '742d2e2e6d3841b1');
  });
});

test('the unicode reference key is unchanged (JS/Python cross-implementation parity)', async () => {
  await withTmpDir(async (dir) => {
    const text = 'héllo wörld — café 日本語 emoji \u{1f389} test';
    const t = tape({ dir, name: 'unicode1', mode: 'record' });
    await t.wrap(async (x) => ({ echo: x }))(text);
    await t.save();

    const parsed = JSON.parse(readFileSync(join(dir, 'unicode1.json'), 'utf8'));
    const [onlyKey] = Object.keys(parsed.entries);
    // This exact hex value is the reference key checked against by the Python
    // port's test_unicode_argument_key_matches_js_reference. It must never change.
    assert.equal(onlyKey, 'd818548c9ae2df14');
  });
});

test('replay mode save() is a no-op and never overwrites the cassette', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'replaysave1', mode: 'record' });
    await rec.wrap(async (x) => x * 2)(5);
    await rec.save();

    const filePath = join(dir, 'replaysave1.json');
    const before = readFileSync(filePath, 'utf8');
    const mtimeBefore = statSync(filePath).mtimeMs;

    const replay = tape({ dir, name: 'replaysave1', mode: 'replay' });
    await replay.wrap(async () => {
      throw new Error('should never be called in replay mode');
    })(5);
    await replay.save();

    const after = readFileSync(filePath, 'utf8');
    assert.equal(after, before);
    assert.equal(statSync(filePath).mtimeMs, mtimeBefore);
  });
});

test('off mode records nothing and save() writes no file', async () => {
  await withTmpDir(async (dir) => {
    let called = 0;
    const t = tape({ dir, name: 'off1', mode: 'off' });
    const wrapped = t.wrap(async (x) => {
      called += 1;
      return x + 1;
    });

    const result = await wrapped(3);
    assert.equal(called, 1);
    assert.equal(result, 4);

    await t.save();
    assert.equal(existsSync(join(dir, 'off1.json')), false);
    assert.deepEqual(t.stats(), { hits: 0, misses: 0, recorded: 0, mode: 'off' });
  });
});

test('stats() tracks hits and misses correctly, including a replay miss caught by the caller', async () => {
  await withTmpDir(async (dir) => {
    const rec = tape({ dir, name: 'stats1', mode: 'record' });
    const recWrapped = rec.wrap(async (x) => x * 10);
    await recWrapped(1);
    await recWrapped(2);
    assert.deepEqual(rec.stats(), { hits: 0, misses: 2, recorded: 2, mode: 'record' });
    await rec.save();

    const replay = tape({ dir, name: 'stats1', mode: 'replay' });
    const replayWrapped = replay.wrap(async (x) => x * 10);

    await replayWrapped(1); // hit
    await assert.rejects(replayWrapped(999)); // miss, but recorded before throwing
    await replayWrapped(1); // hit (repeat of the single recorded entry)

    assert.deepEqual(replay.stats(), { hits: 2, misses: 1, recorded: 0, mode: 'replay' });
  });
});

test('a malformed cassette file throws a clear Error naming the path at construction', async () => {
  await withTmpDir(async (dir) => {
    mkdirSync(dir, { recursive: true });
    const filePath = join(dir, 'broken.json');
    writeFileSync(filePath, '{ not valid json', 'utf8');

    assert.throws(
      () => tape({ dir, name: 'broken', mode: 'replay' }),
      (err) => {
        assert.ok(err instanceof Error);
        assert.ok(err.message.includes(filePath), 'error should name the cassette path');
        return true;
      }
    );
  });
});

test('keys() reflects the current in-memory cassette', async () => {
  await withTmpDir(async (dir) => {
    const t = tape({ dir, name: 'keys1', mode: 'record' });
    assert.deepEqual(t.keys(), []);
    const wrapped = t.wrap(async (x) => x);
    await wrapped(1);
    await wrapped(2);
    assert.equal(t.keys().length, 2);
  });
});
