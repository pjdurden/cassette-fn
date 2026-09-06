"""cassette-fn: a function-boundary VCR for synchronous calls.

Python port of the JavaScript ``cassette-fn`` package. The JavaScript source
(``index.js`` at the repository root) is the specification; this module
matches its observable behavior.

Difference from the JavaScript version: the JavaScript package wraps
``async`` functions. This port wraps ordinary synchronous callables instead.
Async support is intentionally not provided; see the README for details.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["tape", "Tape", "Stats", "CassetteError"]

# Order matches the JS `Set` insertion order, so the error message built from
# it reads the same in both implementations.
_VALID_MODES = ("auto", "record", "replay", "off")
_VALID_MODES_SET = frozenset(_VALID_MODES)

_ENV_VAR = "CASSETTE_FN_MODE"


class CassetteError(RuntimeError):
    """Raised for cassette-fn's own configuration/format errors.

    Covers the cases where the JavaScript version throws a plain ``Error``:
    an invalid mode, a missing or unparseable cassette file, a malformed
    cassette structure, or a replay lookup miss. It subclasses
    ``RuntimeError`` so ``except RuntimeError`` also catches it, the same
    way a reconstructed recorded exception (also a ``RuntimeError``, see
    below) would be caught by generic error handling.
    """


def _identity(args: List[Any], kwargs: Dict[str, Any]) -> Tuple[Any, Any]:
    return args, kwargs


def _compute_key(normalized_args: Any, normalized_kwargs: Any) -> str:
    """sha256 of the JSON-serialized normalized call, hex, first 16 chars.

    When there are no keyword arguments (the common case, and the only case
    the JavaScript version has), the hashed payload is exactly
    ``normalized_args`` and nothing else, so the key is byte-for-byte
    identical to what the JavaScript implementation computes for the same
    positional call. This is required for cassette files to be portable
    between the two implementations, and must not change.

    When there ARE keyword arguments, the payload becomes
    ``[normalized_args, sorted_kwargs]``, where ``sorted_kwargs`` is
    ``normalized_kwargs`` re-keyed in sorted-key order so that
    ``f(a=1, b=2)`` and ``f(b=2, a=1)`` hash identically regardless of the
    order the caller happened to pass them in. Only the top-level keyword
    names are sorted; values (including nested dicts) keep their own
    insertion order, matching how positional arguments are already hashed.

    Uses compact separators (no spaces) and ``ensure_ascii=False`` to match
    JavaScript's ``JSON.stringify`` byte-for-byte. ``JSON.stringify`` never
    escapes non-ASCII codepoints; Python's ``json.dumps`` does by default
    (``ensure_ascii=True``), which would produce a different serialized
    string, and therefore a different key, for any argument containing an
    accent, a curly quote, CJK text, or an emoji. ASCII-only input has
    nothing to escape either way, so this does not change keys for
    ASCII-only arguments.
    """
    if normalized_kwargs:
        sorted_kwargs = dict(sorted(normalized_kwargs.items(), key=lambda item: item[0]))
        payload: Any = [normalized_args, sorted_kwargs]
    else:
        payload = normalized_args
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class Stats:
    """Snapshot returned by ``Tape.stats()``.

    - hits: calls served from the cassette.
    - misses: calls not found in the cassette (every record-mode call, plus
      replay-mode lookup misses).
    - recorded: entries written into the in-memory cassette this session
      (always 0 in replay and off mode).
    - mode: the resolved runtime mode ('record' | 'replay' | 'off'), never
      the literal 'auto' input.
    """

    hits: int
    misses: int
    recorded: int
    mode: str


class Tape:
    """A function-boundary VCR for synchronous calls (e.g. LLM provider calls).

    Mode resolution happens once, at construction time:
      - 'auto' resolves to 'replay' if the cassette file already exists on
        disk, otherwise it resolves to 'record'. This decision is NOT
        re-checked per call.
      - Any other mode ('record' | 'replay' | 'off') is used as given.
      - The CASSETTE_FN_MODE environment variable, if set, overrides the
        `mode` argument entirely. This is the same environment variable name
        the JavaScript version reads, so the two implementations honor one
        setting.

    If the resolved mode is 'replay', the cassette file is loaded
    synchronously here. A missing or unparseable cassette raises immediately,
    naming the path, so failures surface at construction time rather than on
    the first replayed call.
    """

    def __init__(
        self,
        *,
        dir: str = ".tapes",
        name: str = "default",
        mode: str = "auto",
        normalize: Optional[Callable[[List[Any], Dict[str, Any]], Tuple[Any, Any]]] = None,
    ) -> None:
        self._dir = dir
        self._name = name
        self._normalize: Callable[[List[Any], Dict[str, Any]], Tuple[Any, Any]] = (
            normalize if normalize is not None else _identity
        )
        self._cassette_path = os.path.join(dir, f"{name}.json")

        requested_mode = os.environ.get(_ENV_VAR) or mode or "auto"
        if requested_mode not in _VALID_MODES_SET:
            raise ValueError(
                'cassette-fn: invalid mode "{}". Expected one of: {}'.format(
                    requested_mode, ", ".join(_VALID_MODES)
                )
            )

        self._resolved_mode: str = (
            ("replay" if os.path.exists(self._cassette_path) else "record")
            if requested_mode == "auto"
            else requested_mode
        )

        self._entries: Dict[str, List[Dict[str, Any]]] = {}

        if self._resolved_mode == "replay":
            try:
                with open(self._cassette_path, "r", encoding="utf-8") as f:
                    raw = f.read()
            except OSError as err:
                raise CassetteError(
                    'cassette-fn: cassette file not found at "{}" ({})'.format(self._cassette_path, err)
                ) from err
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as err:
                raise CassetteError(
                    'cassette-fn: cassette at "{}" is not valid JSON ({})'.format(self._cassette_path, err)
                ) from err
            if not isinstance(parsed, dict) or not isinstance(parsed.get("entries"), dict):
                raise CassetteError(
                    'cassette-fn: cassette at "{}" is malformed: missing "entries" object'.format(
                        self._cassette_path
                    )
                )
            self._entries = parsed["entries"]

        # Per-Tape-instance replay cursor: how many times each key has been
        # consumed. Deliberately NOT persisted to the cassette file, which
        # stays a pure recording.
        self._replay_cursor: Dict[str, int] = {}

        self._hits = 0
        self._misses = 0
        self._recorded = 0

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a synchronous function so calls are recorded to (or replayed
        from) the cassette.

        Both positional and keyword arguments are supported and included in
        the lookup key (keyword argument names are sorted before hashing, so
        argument order never affects the key) and in the stored entry, since
        essentially every real-world LLM SDK call (e.g.
        ``client.messages.create(model=..., max_tokens=..., messages=...)``)
        is keyword-based.

        The wrapped ``fn`` is always called with the real, un-normalized
        arguments, since it may need a real client object, file handle, or
        other value ``normalize`` strips out. What gets written to the
        cassette (both the lookup key and the stored ``args``/``kwargs``) is
        always the *normalized* value, never the raw one. This is what makes
        ``normalize`` an effective way to keep a secret (an API key, an auth
        token) out of a cassette file, and also what makes it possible to
        wrap a call that takes a non-serializable argument at all: strip or
        replace that argument in ``normalize`` and the raw, non-serializable
        value never reaches ``copy.deepcopy``.
        """

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if self._resolved_mode == "off":
                return fn(*args, **kwargs)

            normalized_args, normalized_kwargs = self._normalize(list(args), dict(kwargs))
            key = _compute_key(normalized_args, normalized_kwargs)

            if self._resolved_mode == "replay":
                entry_list = self._entries.get(key)
                if not entry_list:
                    self._misses += 1
                    raise CassetteError(
                        'cassette-fn: no recording found for key "{}" in cassette "{}"'.format(
                            key, self._cassette_path
                        )
                    )
                consumed = self._replay_cursor.get(key, 0)
                index = min(consumed, len(entry_list) - 1)
                self._replay_cursor[key] = consumed + 1
                self._hits += 1

                entry = entry_list[index]
                if "error" in entry:
                    error_info = entry["error"] or {}
                    err = RuntimeError(error_info.get("message"))
                    err.name = error_info.get("name")  # type: ignore[attr-defined]
                    raise err
                return copy.deepcopy(entry.get("result"))

            # self._resolved_mode == "record"
            if key not in self._entries:
                self._entries[key] = []
            try:
                result = fn(*args, **kwargs)
                new_entry: Dict[str, Any] = {"args": copy.deepcopy(normalized_args)}
                if normalized_kwargs:
                    new_entry["kwargs"] = copy.deepcopy(normalized_kwargs)
                new_entry["result"] = copy.deepcopy(result)
                self._entries[key].append(new_entry)
                self._recorded += 1
                self._misses += 1
                return result
            except Exception as err:
                new_entry = {"args": copy.deepcopy(normalized_args)}
                if normalized_kwargs:
                    new_entry["kwargs"] = copy.deepcopy(normalized_kwargs)
                new_entry["error"] = {"message": str(err), "name": type(err).__name__}
                self._entries[key].append(new_entry)
                self._recorded += 1
                self._misses += 1
                raise

        return wrapped

    def save(self) -> None:
        """Write the cassette to disk. No-op in 'replay' and 'off' mode."""
        if self._resolved_mode in ("replay", "off"):
            return
        parent = os.path.dirname(self._cassette_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = {"version": 1, "name": self._name, "entries": self._entries}
        with open(self._cassette_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def stats(self) -> Stats:
        return Stats(hits=self._hits, misses=self._misses, recorded=self._recorded, mode=self._resolved_mode)

    def keys(self) -> List[str]:
        return list(self._entries.keys())


def tape(
    *,
    dir: str = ".tapes",
    name: str = "default",
    mode: str = "auto",
    normalize: Optional[Callable[[List[Any], Dict[str, Any]], Tuple[Any, Any]]] = None,
) -> Tape:
    """Create a Tape. See ``Tape`` for the full behavior contract."""
    return Tape(dir=dir, name=name, mode=mode, normalize=normalize)
