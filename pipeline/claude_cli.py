"""Shared headless-CLI transport for the optional Claude provider.

In pipeline/ beside score_jobs.py, which lazy-imports it. NOT copied to the VM -- every
import must stay behind a `provider == "claude"` check (the VM scores with
Gemini unconditionally; see keypool.py / score_jobs.py). Consumers:

- `local/resume_tailor/llm.py` (`_call_claude`, a one-shot per call, its own
  retry loop mirroring `_call_gemini`).
- `score_jobs.py` (`ClaudePool`, an async adapter duck-typed to match
  `keypool.KeyPool.generate(model=, contents=, config=)` so call sites don't
  change).

Caching note: the `claude` CLI marks a prompt-cache breakpoint on the system
prompt (`--system-prompt-file`). Callers MUST put stable, byte-identical-across-calls
content in `system` and volatile per-item content in `user` (stdin) --
putting per-item data in the system prompt defeats caching and can even
increase cost (a changed system prompt is a fresh, uncached breakpoint).

Stdlib only: no `google.genai`, no Qt, no third-party imports. This module
must import cleanly even where those packages are absent.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from types import SimpleNamespace

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no console flash
DEFAULT_TIMEOUT_S = 180

# Other providers' secrets must not ride into the `claude` child process. The
# parent has already loaded .env, so a bare subprocess.run() would hand Bright
# Data's and Gemini's credentials to an unrelated vendor's CLI purely by env
# inheritance. Nothing here is needed by `claude` (it authenticates through its
# own stored login), so strip them; everything else — PATH, HOME/USERPROFILE,
# ANTHROPIC_* — is passed through untouched.
_SCRUBBED_ENV_VARS = (
    "GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS", "RESUME_TAILOR_GEMINI_API_KEY",
    "BRIGHT_DATA_API_TOKEN", "HEALTHCHECKS_URL", "HEALTHCHECK_URL",
)


def _child_env() -> dict:
    """A copy of `os.environ` with every non-Anthropic credential removed."""
    env = dict(os.environ)
    for name in _SCRUBBED_ENV_VARS:
        env.pop(name, None)
    return env


class ClaudeCLIError(RuntimeError):
    """Raised for any failed `claude` CLI invocation.

    `.kind` is one of: 'not_found' (CLI missing from PATH -- never retriable),
    'timeout' (subprocess exceeded timeout_s), 'rate_limit' (usage/rate limit
    text detected in stderr or the envelope), 'bad_json' (stdout wasn't a
    parseable JSON envelope, or -- for extract_json_text callers -- the
    envelope's `result` text wasn't extractable JSON), 'cli_too_old' (the
    installed CLI refuses the model until it is updated; run_claude has already
    tried the model's MODEL_FALLBACKS entry, so never retriable), 'error'
    (anything else).
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


def is_cli_too_old_message(text: str | None) -> bool:
    """True if `text` is the CLI refusing a model it is too old to run.

    Claude Code 2.1.207 answers `--model claude-opus-5-5` with "Claude Code
    2.1.207 does not support this model; version 2.1.280 or newer is required".
    Both halves must be present, so an ordinary 400 never matches. Checked
    before is_rate_limit_message, because a required version such as 2.1.429
    carries "429".
    """
    t = (text or "").lower()
    return "does not support this model" in t and "or newer is required" in t


# A model the installed CLI may be too old for, mapped to the model a run uses
# instead until the user runs `claude update`. One hop only: a fallback is
# never itself swapped.
MODEL_FALLBACKS: dict[str, str] = {
    "claude-opus-5-5": "claude-opus-5",
    "claude-sonnet-5-5": "claude-sonnet-5",
}

# The CLI version each model first ran on, for Check setup's warning. The CLI
# error text is the source: "version 2.1.280 or newer is required".
# claude-sonnet-5-5 has no entry on purpose: the first CLI version that runs it
# is not known without a live call. Check setup stays silent about it (no
# version to name), and a CLI too old for it still swaps to claude-sonnet-5 at
# run time through MODEL_FALLBACKS. Add the entry once a refusal names the version.
MIN_CLI_VERSION: dict[str, tuple[int, int, int]] = {"claude-opus-5-5": (2, 1, 280)}

# Models the installed CLI refused this process, mapped to the fallback in use.
# Guarded by a lock because ClaudePool runs run_claude on worker threads.
_SWAP_LOCK = threading.Lock()
_SWAPPED: dict[str, str] = {}
_WARNED: set[str] = set()

_REQUIRED_VERSION_RE = re.compile(r"version\s+(\d+(?:\.\d+)+)\s+or newer is required",
                                  re.IGNORECASE)


def reset_model_fallbacks() -> None:
    """Forget every remembered model swap and warning (for tests)."""
    with _SWAP_LOCK:
        _SWAPPED.clear()
        _WARNED.clear()


def active_swaps() -> dict[str, str]:
    """{refused model: fallback in use} for this process, as a copy.

    The dashboard runs under pythonw, where the stderr warning goes nowhere, so
    the tailor reads this to put the swap on its own warning channel."""
    with _SWAP_LOCK:
        return dict(_SWAPPED)


def _swapped_model(model: str) -> str | None:
    with _SWAP_LOCK:
        return _SWAPPED.get(model)


def _remember_swap(model: str, fallback: str, cli_text: str) -> None:
    """Record the swap and print the one warning this process gives for it."""
    with _SWAP_LOCK:
        _SWAPPED[model] = fallback
        if model in _WARNED:
            return
        _WARNED.add(model)
    m = _REQUIRED_VERSION_RE.search(cli_text or "")
    needs = f" (it needs version {m.group(1)} or newer)" if m else ""
    stream = sys.stderr
    if stream is None:  # pythonw: no console, so nowhere to warn
        return
    try:
        print(f"claude CLI does not support {model} yet{needs}; using {fallback}. "
              "Run `claude update` to use it.", file=stream, flush=True)
    except (OSError, ValueError):  # a closed or broken stream never fails the run
        pass


def _error_kind(text: str) -> str:
    if is_cli_too_old_message(text):
        return "cli_too_old"
    return "rate_limit" if is_rate_limit_message(text) else "error"


_VERSION_RE = re.compile(r"\s*v?(\d+(?:\.\d+)+)")


def cli_version(timeout_s: float = 20) -> tuple[int, ...] | None:
    """The installed CLI's version from `claude --version` ("2.1.207 (Claude
    Code)" gives (2, 1, 207)), or None when the CLI is missing, the probe fails
    or the output does not start with a version. Free: no model call."""
    exe = find_claude()
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            cwd=tempfile.gettempdir(), creationflags=_NO_WINDOW,
            env=_child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = _VERSION_RE.match(proc.stdout or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _write_system_prompt(text: str) -> str:
    """Write `text` to a new temp file for --system-prompt-file and return its
    path. newline='' keeps the text byte for byte (no \\r\\n on Windows), so the
    prompt cache sees the same system prompt on every call."""
    fd, path = tempfile.mkstemp(prefix="claude-system-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


@dataclass
class CLIResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # The model that answered: the requested one, or its MODEL_FALLBACKS entry
    # after a swap. '' only from callers that build a CLIResult by hand.
    model: str = ""


def run_claude(
    system: str,
    user: str,
    model: str,
    *,
    json_mode: bool = False,
    allow_websearch: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    effort: str | None = None,
) -> CLIResult:
    """One `claude -p` invocation, no retry, except the one model fallback
    (retry policy belongs to each caller -- `_call_claude` in llm.py,
    `ClaudePool.generate` here).

    `effort` rides `--effort` (low / medium / high / xhigh / max); None or ''
    sends no flag and leaves the CLI's own default. At that default Opus 4.8
    spent about 14,600 output tokens thinking over 8 résumé bullets (176 s),
    where `low` answered in 12 s, so the tailor pins it; the scorer passes none.

    Prompt rides stdin (`user`), the JSON envelope comes back on stdout.
    `--system-prompt-file` fully overrides the CLI's default system prompt (no
    repo CLAUDE.md / skills leak in) and is ALSO where the CLI marks its
    prompt-cache breakpoint -- see the module docstring's caching note.
    Runs in a temp cwd so no project files are visible to the child process.

    The model fallback: when the installed CLI is too old for `model`
    (kind 'cli_too_old') and MODEL_FALLBACKS names a fallback, the call runs
    once more on the fallback. The swap is remembered for the rest of the
    process, so later calls for `model` go straight to the fallback, and one
    warning goes to stderr. The fallback's own failure is raised as is; a
    'cli_too_old' for a model with no fallback is raised as is.
    """
    kwargs = dict(json_mode=json_mode, allow_websearch=allow_websearch,
                  timeout_s=timeout_s, effort=effort)
    swapped = _swapped_model(model)
    if swapped is not None:
        return _run_once(system, user, swapped, **kwargs)
    try:
        return _run_once(system, user, model, **kwargs)
    except ClaudeCLIError as exc:
        fallback = MODEL_FALLBACKS.get(model)
        if exc.kind != "cli_too_old" or fallback is None:
            raise
        _remember_swap(model, fallback, str(exc))
    return _run_once(system, user, fallback, **kwargs)


def _run_once(
    system: str,
    user: str,
    model: str,
    *,
    json_mode: bool,
    allow_websearch: bool,
    timeout_s: float,
    effort: str | None = None,
) -> CLIResult:
    """The single `claude -p` invocation behind run_claude."""
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
    # The system prompt (the full résumé + schema) rides a temp file, never
    # argv. On Windows `claude` is usually npm's claude.cmd shim, which runs
    # through cmd.exe, and cmd.exe refuses any command line over 8,191
    # characters ("The command line is too long."). The file also keeps the
    # résumé out of process listings.
    sys_path = _write_system_prompt(sys_prompt)
    # A bare completion: no built-in tools (WebSearch alone, and only on request),
    # no skills and no MCP servers. With the CLI's full toolset the cover letter's
    # repair prompt, which credits its rules to "avoid-ai-writing v3.18.0", made
    # the model run the user's installed skill of that name, and the skill's audit
    # text reached the letter. The tool schemas also cost ~30k
    # cache-write tokens on every call. An empty `--tools` argument survives the
    # claude.CMD shim.
    argv = [
        exe, "-p", "--output-format", "json", "--model", model,
        "--system-prompt-file", sys_path, "--exclude-dynamic-system-prompt-sections",
        "--tools", "WebSearch" if allow_websearch else "",
        "--disable-slash-commands", "--strict-mcp-config",
    ]
    if effort:
        argv += ["--effort", effort]
    if allow_websearch:
        argv += ["--allowedTools", "WebSearch"]
    try:
        proc = subprocess.run(
            argv, input=user, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            cwd=tempfile.gettempdir(), creationflags=_NO_WINDOW,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCLIError(
            f"claude timed out after {timeout_s:.0f}s ({model})", kind="timeout"
        ) from exc
    finally:
        try:
            os.remove(sys_path)
        except OSError:  # already gone, or still held open: leave it in the temp folder
            pass
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:400]
        kind = _error_kind(err)
        if kind != "cli_too_old" and is_cli_too_old_message(proc.stdout):
            kind = "cli_too_old"  # the refusal rode stdout beside unrelated stderr
        raise ClaudeCLIError(f"claude exited {proc.returncode}: {err}", kind=kind)
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
            f"claude reported an error: {result[:300]}", kind=_error_kind(result),
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
        model=model,
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
        # `model` mirrors KeyPool.generate, which takes a single id OR a ranked
        # list of interchangeable ones. There is nothing to rank here: the list
        # exists to spend Gemini's per-(key, model) free allowance, and Claude
        # has no such allowance to multiply -- every model is billed the same
        # subscription. So take the first and ignore the rest, rather than let a
        # list reach `--model` and the (model, system) warmup key as a repr.
        if isinstance(model, (list, tuple)):
            if not model:
                raise ClaudeCLIError("generate called with no model")
            model = model[0]
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
                if exc.kind in ("not_found", "cli_too_old"):
                    raise  # never retriable (run_claude already tried the fallback)
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
