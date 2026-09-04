/** Requested mode. 'auto' resolves once, at tape() construction, to 'record' or 'replay'. */
export type TapeMode = 'auto' | 'record' | 'replay' | 'off';

/** Resolved runtime mode as reported by stats(): never 'auto'. */
export type ResolvedTapeMode = 'record' | 'replay' | 'off';

export interface TapeOptions {
  /** Cassette directory. Default '.tapes'. */
  dir?: string;
  /** Cassette file basename (file is `<dir>/<name>.json`). Default 'default'. */
  name?: string;
  /**
   * 'auto' | 'record' | 'replay' | 'off'. Default 'auto'.
   * Overridden by process.env.CASSETTE_FN_MODE when set.
   */
  mode?: TapeMode;
  /**
   * Applied to the call's args array before hashing, so callers can strip
   * volatile fields (timestamps, request ids, an apiKey). Default: identity.
   */
  normalize?: (args: any[]) => any;
}

export interface TapeStats {
  /** Calls served from the cassette. */
  hits: number;
  /** Calls not found in the cassette (every record-mode call, and replay lookup misses). */
  misses: number;
  /** Entries written into the in-memory cassette this session. */
  recorded: number;
  /** The resolved runtime mode ('record' | 'replay' | 'off'), not the literal 'auto' input. */
  mode: ResolvedTapeMode;
}

export interface Tape {
  /**
   * Wrap an async function so calls are recorded to, or replayed from, the cassette.
   */
  wrap<Args extends any[], Result>(
    fn: (...args: Args) => Promise<Result>
  ): (...args: Args) => Promise<Result>;

  /** Write the cassette to disk. No-op in 'replay' and 'off' mode. */
  save(): Promise<void>;

  /** Current hit/miss/recorded counters and the resolved mode. */
  stats(): TapeStats;

  /** Keys currently present in the (in-memory) cassette. */
  keys(): string[];
}

/**
 * Create a Tape: a function-boundary VCR for async calls.
 */
export function tape(options?: TapeOptions): Tape;
