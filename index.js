import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';

const VALID_MODES = new Set(['auto', 'record', 'replay', 'off']);

/**
 * Deep-clone a JSON-serializable value so a caller cannot mutate stored
 * cassette data (or vice versa) through a shared reference.
 * @param {any} value
 * @returns {any}
 */
function deepClone(value) {
  if (typeof structuredClone === 'function') {
    return structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value));
}

/**
 * Identity normalizer: used when no `options.normalize` is supplied.
 * @param {any[]} args
 * @returns {any[]}
 */
function identity(args) {
  return args;
}

/**
 * Compute the cassette lookup key for a call.
 * @param {any} normalizedArgs
 * @returns {string} first 16 hex chars of the sha256 of JSON.stringify(normalizedArgs)
 */
function computeKey(normalizedArgs) {
  return createHash('sha256').update(JSON.stringify(normalizedArgs)).digest('hex').slice(0, 16);
}

/**
 * @typedef {'auto' | 'record' | 'replay' | 'off'} TapeMode
 *
 * @typedef {Object} TapeOptions
 * @property {string} [dir] - Cassette directory. Default '.tapes'.
 * @property {string} [name] - Cassette file basename. Default 'default'.
 * @property {TapeMode} [mode] - Default 'auto'. Overridden by process.env.CASSETTE_FN_MODE if set.
 * @property {(args: any[]) => any} [normalize] - Applied to call args before hashing. Default identity.
 *
 * @typedef {Object} TapeStats
 * @property {number} hits - Calls served from the cassette.
 * @property {number} misses - Calls not found in the cassette (record-mode calls, and replay lookup misses).
 * @property {number} recorded - Entries written into the in-memory cassette this session.
 * @property {string} mode - The resolved runtime mode ('record' | 'replay' | 'off'), not the literal 'auto' input.
 *
 * @typedef {Object} Tape
 * @property {(fn: (...args: any[]) => Promise<any>) => (...args: any[]) => Promise<any>} wrap
 * @property {() => Promise<void>} save
 * @property {() => TapeStats} stats
 * @property {() => string[]} keys
 */

/**
 * Create a Tape: a function-boundary VCR for async calls (e.g. LLM provider calls).
 *
 * Mode resolution happens once, here, at construction time:
 *   - 'auto' resolves to 'replay' if the cassette file already exists on disk,
 *     otherwise it resolves to 'record'. This decision is NOT re-checked per call.
 *   - Any other mode ('record' | 'replay' | 'off') is used as given.
 *   - process.env.CASSETTE_FN_MODE, if set, overrides options.mode entirely.
 *
 * If the resolved mode is 'replay', the cassette file is loaded synchronously right
 * here (readFileSync). A missing or unparseable cassette throws immediately, naming
 * the path, so failures surface at setup time rather than on the first replayed call.
 *
 * @param {TapeOptions} [options]
 * @returns {Tape}
 */
export function tape(options = {}) {
  const dir = options.dir ?? '.tapes';
  const name = options.name ?? 'default';
  const normalize = options.normalize ?? identity;
  const cassettePath = join(dir, `${name}.json`);

  const requestedMode = process.env.CASSETTE_FN_MODE || options.mode || 'auto';
  if (!VALID_MODES.has(requestedMode)) {
    throw new Error(
      `cassette-fn: invalid mode "${requestedMode}". Expected one of: ${[...VALID_MODES].join(', ')}`
    );
  }

  const resolvedMode =
    requestedMode === 'auto' ? (existsSync(cassettePath) ? 'replay' : 'record') : requestedMode;

  /** @type {Record<string, Array<{args: any[], result?: any, error?: {message: string, name: string}}>>} */
  let entries = {};

  if (resolvedMode === 'replay') {
    let raw;
    try {
      raw = readFileSync(cassettePath, 'utf8');
    } catch (err) {
      throw new Error(`cassette-fn: cassette file not found at "${cassettePath}" (${err.message})`);
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      throw new Error(`cassette-fn: cassette at "${cassettePath}" is not valid JSON (${err.message})`);
    }
    if (!parsed || typeof parsed !== 'object' || typeof parsed.entries !== 'object' || parsed.entries === null) {
      throw new Error(`cassette-fn: cassette at "${cassettePath}" is malformed: missing "entries" object`);
    }
    entries = parsed.entries;
  }

  // Per-Tape-instance replay cursor: how many times each key has been consumed.
  // Deliberately NOT persisted to the cassette file, which stays a pure recording.
  /** @type {Map<string, number>} */
  const replayCursor = new Map();

  let hits = 0;
  let misses = 0;
  let recorded = 0;

  /**
   * Wrap an async function so calls are recorded to (or replayed from) the cassette.
   * @param {(...args: any[]) => Promise<any>} fn
   * @returns {(...args: any[]) => Promise<any>}
   */
  function wrap(fn) {
    return async function wrapped(...args) {
      if (resolvedMode === 'off') {
        return fn(...args);
      }

      const key = computeKey(normalize(args));

      if (resolvedMode === 'replay') {
        const list = entries[key];
        if (!list || list.length === 0) {
          misses += 1;
          throw new Error(
            `cassette-fn: no recording found for key "${key}" in cassette "${cassettePath}"`
          );
        }
        const consumed = replayCursor.get(key) ?? 0;
        const index = Math.min(consumed, list.length - 1);
        replayCursor.set(key, consumed + 1);
        hits += 1;

        const entry = list[index];
        if (entry.error) {
          const err = new Error(entry.error.message);
          err.name = entry.error.name;
          throw err;
        }
        return deepClone(entry.result);
      }

      // resolvedMode === 'record'
      if (!entries[key]) entries[key] = [];
      try {
        const result = await fn(...args);
        entries[key].push({ args: deepClone(args), result: deepClone(result) });
        recorded += 1;
        misses += 1;
        return result;
      } catch (err) {
        entries[key].push({ args: deepClone(args), error: { message: err.message, name: err.name } });
        recorded += 1;
        misses += 1;
        throw err;
      }
    };
  }

  /**
   * Write the cassette to disk. No-op in 'replay' and 'off' mode.
   * @returns {Promise<void>}
   */
  async function save() {
    if (resolvedMode === 'replay' || resolvedMode === 'off') {
      return;
    }
    await mkdir(dirname(cassettePath), { recursive: true });
    const data = { version: 1, name, entries };
    await writeFile(cassettePath, JSON.stringify(data, null, 2), 'utf8');
  }

  /**
   * @returns {TapeStats}
   */
  function stats() {
    return { hits, misses, recorded, mode: resolvedMode };
  }

  /**
   * @returns {string[]}
   */
  function keys() {
    return Object.keys(entries);
  }

  return { wrap, save, stats, keys };
}
