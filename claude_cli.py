"""Shared headless-CLI transport for the optional Claude provider.

Root-level so score_jobs.py can lazy-import it. NOT copied to the VM -- every
import must stay behind a `provider == "claude"` check (the VM scores with
Gemini unconditionally; see keypool.py / score_jobs.py). Consumers:

- `local/resume_tailor/llm.py` (`_call_claude`, a one-shot per call, its own
  retry loop mirroring `_call_gemini`).
- `score_jobs.py` (`ClaudePool`, an async adapter duck-typed to match
  `keypool.KeyPool.generate(model=, contents=, config=)` so call sites don't
  change).

Caching note: the `claude` CLI marks a prompt-cache breakpoint on
`--system-prompt`. Callers MUST put stable, byte-identical-across-calls
content in `system` and volatile per-item content in `user` (stdin) --
putting per-item data in the system prompt defeats caching and can even
increase cost (a changed system prompt is a fresh, uncached breakpoint).

Stdlib only: no `google.genai`, no Qt, no third-party imports. This module
must import cleanly even where those packages are absent.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from types import SimpleNamespace

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no console flash
DEFAULT_TIMEOUT_S = 180


class ClaudeCLIError(RuntimeError):
    """Raised for any failed `claude` CLI invocation.

    `.kind` is one of: 'not_found' (CLI missing from PATH -- never retriable),
    'timeout' (subprocess exceeded timeout_s), 'rate_limit' (usage/rate limit
    text detected in stderr or the envelope), 'bad_json' (stdout wasn't a
    parseable JSON envelope, or -- for extract_json_text callers -- the
    envelope's `result` text wasn't extractable JSON), 'error' (anything else).
    """

    def __init__(self, msg: str, *, kind: str = "error"):
        super().__init__(msg)
        self.kind = kind


def find_claude() -> str | None:
    """Path to the `claude` executable, or None if not on PATH.

    `shutil.which` resolves `claude.cmd` / `claude.exe` on Windows and the
    plain `claude` binary elsewhere.
    """
    return shutil.which("claude")


def is_rate_limit_message(text: str | None) -> bool:
    """True if `text` looks like a Claude Code usage/rate-limit message."""
    t = (text or "").lower()
    return any(
        s in t
        for s in ("rate limit", "usage limit", "limit reached", "429",
                  "overloaded", "resets at")
    )


@dataclass
class CLIResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def run_claude(
    system: str,
    user: str,
    model: str,
    *,
    json_mode: bool = False,
    allow_websearch: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CLIResult:
    """One `claude -p` invocation, no retry (retry policy belongs to each
    caller -- `_call_claude` in llm.py, `ClaudePool.generate` here).

    Prompt rides stdin (`user`), the JSON envelope comes back on stdout.
    `--system-prompt` fully overrides the CLI's default system prompt (no
    repo CLAUDE.md / skills leak in) and is ALSO where the CLI marks its
    prompt-cache breakpoint -- see the module docstring's caching note.
    Runs in a temp cwd so no project files are visible to the child process.
    """
    exe = find_claude()
    if exe is None:
        raise ClaudeCLIError(
            "`claude` CLI not found on PATH. Install Claude Code and log in "
            "(run `claude` once).",
            kind="not_found",
        )
    sys_prompt = system
    if json_mode:
        sys_prompt += (
            "\n\nRespond with ONLY valid JSON -- no prose, no markdown, "
            "no code fences."
        )
    argv = [
        exe, "-p", "--output-format", "json", "--model", model,
        "--system-prompt", sys_prompt, "--exclude-dynamic-system-prompt-sections",
    ]
    if allow_websearch:
        argv += ["--allowedTools", "WebSearch"]
    try:
        proc = subprocess.run(
            argv, input=user, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            cwd=tempfile.gettempdir(), creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCLIError(
            f"claude timed out after {timeout_s:.0f}s ({model})", kind="timeout"
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:400]
        raise ClaudeCLIError(
            f"claude exited {proc.returncode}: {err}",
            kind="rate_limit" if is_rate_limit_message(err) else "error",
        )
    try:
        envelope = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise ClaudeCLIError(
            f"claude emitted a non-JSON envelope: {(proc.stdout or '')[:300]}",
            kind="bad_json",
        ) from exc
    result = str(envelope.get("result") or "")
    if envelope.get("is_error"):
        raise ClaudeCLIError(
            f"claude reported an error: {result[:300]}",
            kind="rate_limit" if is_rate_limit_message(result) else "error",
        )
    if not result.strip():
        raise ClaudeCLIError("empty response", kind="error")
    usage = envelope.get("usage") or {}
    return CLIResult(
        result.strip(),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
    )


def extract_json_text(text: str) -> str:
    """Parse JSON out of `text`, tolerating ```json fences or surrounding
    prose (same tolerance as `resume_tailor.llm._extract_json`), then
    re-serialize to canonical JSON via `json.dumps` so callers doing
    `json.loads(resp.text)` (score_jobs.py) always get clean input.

    Raises ValueError when no JSON can be salvaged.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE
        ).strip()
    try:
        return json.dumps(json.loads(stripped))
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = stripped.find(opener), stripped.rfind(closer)
        if 0 <= i < j:
            try:
                return json.dumps(json.loads(stripped[i : j + 1]))
            except json.JSONDecodeError:
                continue
    raise ValueError(f"claude did not return valid JSON. Got:\n{stripped[:500]}")


class _Resp:
    """Duck-types the google-genai response shape score_jobs._track_usage
    reads (`.text`, `.usage_metadata.prompt_token_count/candidates_token_count`).
    """

    def __init__(self, text: str, in_tok: int, out_tok: int):
        self.text = text
        self.usage_metadata = SimpleNamespace(
            prompt_token_count=in_tok, candidates_token_count=out_tok
        )


class ClaudePool:
    """Async adapter matching `keypool.KeyPool.generate(model=, contents=,
    config=)` so `score_jobs.py` call sites don't change when switching
    provider. `config` is a genai `GenerateContentConfig`-shaped object; read
    via `getattr` so hand-built fakes (SimpleNamespace) work in tests.

    Warm-up serialization: the FIRST `generate` call for a given
    `(model, hash(system))` key runs alone -- concurrent calls sharing that
    key block behind an `asyncio.Event` until the first call completes
    (success OR failure), then proceed at full concurrency. This lets the
    CLI's cold-start/cache-creation cost happen once per (model, system)
    combination instead of once per concurrent worker. Distinct keys never
    block each other.
    """

    RATE_LIMIT_RETRIES = 4
    TRANSIENT_RETRIES = 3

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S, max_procs: int = 4):
        self._sem = asyncio.Semaphore(max(1, max_procs))
        self._timeout_s = timeout_s
        self._claude_calls = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._warmup_events: dict[tuple, asyncio.Event] = {}
        self._warmed: set[tuple] = set()

    def stats(self) -> dict:
        return {
            "free_calls": 0,
            "vertex_calls": 0,
            "claude_calls": self._claude_calls,
            "cache_read_tokens": self._cache_read_tokens,
            "cache_write_tokens": self._cache_write_tokens,
        }

    async def _enter_warmup_gate(self, key: tuple) -> bool:
        """Returns True if this call is the FIRST for `key` (it must call
        `_release_warmup_gate(key)` when done, success or failure). Otherwise
        waits for the first call to finish, then returns False.

        Synchronous up to the first `await`, so two calls racing on the same
        key can't both see "no event yet" -- whichever runs first in the
        event loop claims the slot before yielding control.
        """
        if key in self._warmed:
            return False
        event = self._warmup_events.get(key)
        if event is None:
            self._warmup_events[key] = asyncio.Event()
            return True
        await event.wait()
        return False

    def _release_warmup_gate(self, key: tuple) -> None:
        self._warmed.add(key)
        event = self._warmup_events.pop(key, None)
        if event is not None:
            event.set()

    async def generate(self, *, model, contents, config):
        system = getattr(config, "system_instruction", "") or ""
        json_mode = getattr(config, "response_mime_type", None) == "application/json"
        schema = getattr(config, "response_schema", None)
        if json_mode and schema:
            system += "\nThe JSON MUST match this JSON Schema exactly:\n" + json.dumps(schema)

        key = (model, hash(system))
        is_first = await self._enter_warmup_gate(key)
        try:
            return await self._generate_with_retry(
                model=model, contents=contents, system=system, json_mode=json_mode
            )
        finally:
            if is_first:
                self._release_warmup_gate(key)

    async def _generate_with_retry(self, *, model, contents, system, json_mode):
        rl = transient = 0
        while True:
            try:
                async with self._sem:
                    res = await asyncio.to_thread(
                        run_claude, system, contents, model,
                        json_mode=json_mode, timeout_s=self._timeout_s,
                    )
                # The CLI call happened and burned real subscription tokens --
                # count them NOW, before JSON extraction can fail, so a
                # bad_json attempt's tokens aren't dropped from stats().
                self._cache_read_tokens += res.cache_read_tokens
                self._cache_write_tokens += res.cache_write_tokens
                text = extract_json_text(res.text) if json_mode else res.text
                self._claude_calls += 1  # successful generates only
                return _Resp(text, res.input_tokens, res.output_tokens)
            except ClaudeCLIError as exc:
                if exc.kind == "not_found":
                    raise  # never retriable
                if exc.kind == "rate_limit":
                    if rl >= self.RATE_LIMIT_RETRIES:
                        raise  # backoff budget spent -- no transient attempts
                    rl += 1
                    await asyncio.sleep(
                        min(30.0 * 2 ** (rl - 1), 300.0) + random.uniform(0, 5)
                    )
                    continue
                transient += 1
                if transient >= self.TRANSIENT_RETRIES:
                    raise  # caller (score_stage1/2) catches -> score=None row
                await asyncio.sleep(1.5 * transient)
            except ValueError as exc:  # extract_json_text failure
                transient += 1
                if transient >= self.TRANSIENT_RETRIES:
                    raise ClaudeCLIError(f"non-JSON output: {exc}", kind="bad_json") from exc
                await asyncio.sleep(1.5 * transient)
