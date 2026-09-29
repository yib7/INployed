"""VL-5: an installed `claude` CLI older than a model needs.

Claude Code 2.1.207 answers `--model claude-opus-5-5` with exit 0 and an
`is_error` envelope saying the model needs 2.1.280 or newer. run_claude maps
that to kind "cli_too_old", re-runs once on the model's MODEL_FALLBACKS entry,
and remembers the swap for the rest of the process.

Every test fakes `claude_cli.subprocess.run` and `claude_cli.find_claude`: the
real `claude` binary is never invoked. The conftest resets the remembered
swaps between tests (`claude_cli.reset_model_fallbacks`).
"""
import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import claude_cli  # noqa: E402

TOO_OLD = ("API Error: 400 Claude Code 2.1.207 does not support this model; version "
           "2.1.280 or newer is required. Run 'claude update', or update the Claude "
           "desktop app, then try again.")
NEW = "claude-opus-5-5"
OLD = "claude-opus-5"


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(result, *, is_error=False):
    return json.dumps({"result": result, "is_error": is_error,
                       "usage": {"input_tokens": 1, "output_tokens": 2}})


def _model(argv):
    return argv[argv.index("--model") + 1]


@pytest.fixture
def fake_exe(monkeypatch):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")


def _old_cli(monkeypatch, *, via="envelope", fallback_result=None):
    """A fake CLI that refuses NEW the way 2.1.207 does and answers anything
    else. Returns the list of models it was asked for, in call order."""
    models: list[str] = []
    lock = threading.Lock()

    def fake_run(argv, **kwargs):
        m = _model(argv)
        with lock:
            models.append(m)
        if m == NEW:
            if via == "envelope":
                return _proc(stdout=_envelope(TOO_OLD, is_error=True))
            return _proc(returncode=1, stderr=TOO_OLD)
        if fallback_result is not None:
            return fallback_result
        return _proc(stdout=_envelope(f"answer from {m}"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    return models


# --- the matcher ----------------------------------------------------------------

def test_is_cli_too_old_message_matches_the_live_text():
    assert claude_cli.is_cli_too_old_message(TOO_OLD) is True
    assert claude_cli.is_cli_too_old_message(TOO_OLD.upper()) is True


@pytest.mark.parametrize("text", [
    None, "", "some unrelated failure",
    "this model does not support this model",   # one half only
    "version 9 or newer is required",           # the other half only
])
def test_is_cli_too_old_message_needs_both_halves(text):
    assert claude_cli.is_cli_too_old_message(text) is False


def test_a_required_version_holding_429_still_maps_to_cli_too_old():
    """A future required version such as 2.1.429 carries "429", which the rate
    limit matcher counts. The too-old check runs first, so it still maps to
    cli_too_old."""
    text = TOO_OLD.replace("2.1.280", "2.1.429")
    assert claude_cli.is_rate_limit_message(text)   # the trap is real
    assert claude_cli._error_kind(text) == "cli_too_old"


def test_model_fallbacks_names_the_opus_pair():
    assert claude_cli.MODEL_FALLBACKS == {NEW: OLD}


# --- the kind, on both failure paths --------------------------------------------

@pytest.mark.parametrize("via", ["envelope", "exit"])
def test_a_model_with_no_fallback_raises_cli_too_old(monkeypatch, fake_exe, via):
    if via == "envelope":
        proc = _proc(stdout=_envelope(TOO_OLD.replace("2.1.280", "2.1.429"), is_error=True))
    else:
        proc = _proc(returncode=1, stderr=TOO_OLD.replace("2.1.280", "2.1.429"))
    calls = []
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **k: (calls.append(_model(argv)), proc)[1])
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", "claude-future-9")
    assert ei.value.kind == "cli_too_old"
    assert calls == ["claude-future-9"]          # no fallback to run


@pytest.mark.parametrize("via", ["envelope", "exit"])
def test_the_fallback_runs_once_then_later_calls_go_straight_to_it(
        monkeypatch, fake_exe, capsys, via):
    models = _old_cli(monkeypatch, via=via)
    first = claude_cli.run_claude("sys", "user", NEW)
    assert first.text == f"answer from {OLD}"
    assert models == [NEW, OLD]
    second = claude_cli.run_claude("sys", "user", NEW)
    assert second.text == f"answer from {OLD}"
    assert models == [NEW, OLD, OLD]             # no wasted first call


def test_the_fallback_run_keeps_the_effort_level(monkeypatch, fake_exe):
    efforts: list = []

    def fake_run(argv, **kwargs):
        efforts.append(argv[argv.index("--effort") + 1] if "--effort" in argv else None)
        if _model(argv) == NEW:
            return _proc(stdout=_envelope(TOO_OLD, is_error=True))
        return _proc(stdout=_envelope("ok"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.run_claude("sys", "user", NEW, effort="low")   # refused, then the fallback
    claude_cli.run_claude("sys", "user", NEW, effort="low")   # straight to the remembered swap
    assert efforts == ["low", "low", "low"]


def test_the_warning_prints_once_per_process(monkeypatch, fake_exe, capsys):
    _old_cli(monkeypatch)
    for _ in range(3):
        claude_cli.run_claude("sys", "user", NEW)
    err = capsys.readouterr().err
    line = f"claude CLI does not support {NEW} yet"
    assert err.count(line) == 1
    assert f"using {OLD}" in err
    assert "Run `claude update` to use it." in err
    assert "2.1.280" in err                      # the required version, parsed


@pytest.mark.parametrize("stream", [None, "closed"])
def test_a_missing_or_closed_stderr_never_fails_the_fallback(monkeypatch, fake_exe, stream):
    """The dashboard runs under pythonw, where sys.stderr is None."""
    import io
    if stream == "closed":
        stream = io.StringIO()
        stream.close()
    models = _old_cli(monkeypatch)
    monkeypatch.setattr(claude_cli.sys, "stderr", stream)
    assert claude_cli.run_claude("sys", "user", NEW).text == f"answer from {OLD}"
    assert models == [NEW, OLD]


def test_other_models_are_untouched_by_a_remembered_swap(monkeypatch, fake_exe):
    models = _old_cli(monkeypatch)
    claude_cli.run_claude("sys", "user", NEW)
    claude_cli.run_claude("sys", "user", "claude-sonnet-5")
    assert models == [NEW, OLD, "claude-sonnet-5"]


def test_the_fallbacks_own_failure_propagates(monkeypatch, fake_exe):
    models = _old_cli(monkeypatch, fallback_result=_proc(returncode=2, stderr="boom"))
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", NEW)
    assert ei.value.kind == "error"
    assert "boom" in str(ei.value)
    assert models == [NEW, OLD]                  # one fallback, no chain


def test_a_fallback_that_is_itself_too_old_raises_cli_too_old(monkeypatch, fake_exe):
    proc = _proc(stdout=_envelope(TOO_OLD, is_error=True))
    models = []
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **k: (models.append(_model(argv)), proc)[1])
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", NEW)
    assert ei.value.kind == "cli_too_old"
    assert models == [NEW, OLD]


def test_other_errors_on_the_new_model_do_not_swap(monkeypatch, fake_exe):
    models = []
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **k: (models.append(_model(argv)), _proc(returncode=1, stderr="boom"))[1])
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", NEW)
    assert ei.value.kind == "error"
    assert models == [NEW]


def test_threads_share_the_remembered_swap(monkeypatch, fake_exe, capsys):
    """The scorer's ClaudePool runs run_claude on worker threads. Once one
    thread has learned the swap, every later call on any thread skips NEW."""
    models = _old_cli(monkeypatch)
    claude_cli.run_claude("sys", "user", NEW)
    assert models == [NEW, OLD]

    def worker():
        claude_cli.run_claude("sys", "user", NEW)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert models[2:] == [OLD] * 8
    assert capsys.readouterr().err.count("does not support") == 1


def test_racing_threads_warn_once(monkeypatch, fake_exe, capsys):
    """Threads that all hit the refusal before any swap is remembered each run
    their own fallback, and the warning still prints once."""
    barrier = threading.Barrier(4)
    models = []
    lock = threading.Lock()

    def fake_run(argv, **kwargs):
        m = _model(argv)
        with lock:
            models.append(m)
        if m == NEW:
            barrier.wait(timeout=5)
            return _proc(stdout=_envelope(TOO_OLD, is_error=True))
        return _proc(stdout=_envelope("ok"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    threads = [threading.Thread(target=claude_cli.run_claude, args=("s", "u", NEW))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(models) == sorted([NEW] * 4 + [OLD] * 4)
    assert capsys.readouterr().err.count("does not support") == 1
    claude_cli.run_claude("s", "u", NEW)
    assert models[-1] == OLD


def test_the_result_names_the_model_that_ran(monkeypatch, fake_exe):
    """llm.USAGE records CLIResult.model, so a call opus 5 answered is never
    booked as opus 5.5."""
    _old_cli(monkeypatch)
    assert claude_cli.run_claude("sys", "user", NEW).model == OLD      # the swap call
    assert claude_cli.run_claude("sys", "user", NEW).model == OLD      # the remembered swap
    assert claude_cli.run_claude("sys", "user", "claude-sonnet-5").model == "claude-sonnet-5"


def test_active_swaps_is_a_copy_of_the_remembered_swaps(monkeypatch, fake_exe):
    _old_cli(monkeypatch)
    assert claude_cli.active_swaps() == {}
    claude_cli.run_claude("sys", "user", NEW)
    swaps = claude_cli.active_swaps()
    assert swaps == {NEW: OLD}
    swaps.clear()                                   # a copy: the caller cannot erase it
    assert claude_cli.active_swaps() == {NEW: OLD}


def test_reset_forgets_the_swap(monkeypatch, fake_exe):
    models = _old_cli(monkeypatch)
    claude_cli.run_claude("sys", "user", NEW)
    claude_cli.reset_model_fallbacks()
    claude_cli.run_claude("sys", "user", NEW)
    assert models == [NEW, OLD, NEW, OLD]


# --- ClaudePool does not retry the kind -----------------------------------------

def test_claude_pool_raises_cli_too_old_without_retrying(monkeypatch):
    calls = []

    def fake_run_claude(*a, **k):
        calls.append(1)
        raise claude_cli.ClaudeCLIError("too old", kind="cli_too_old")

    async def no_sleep(_s):
        raise AssertionError("cli_too_old must not sleep or retry")

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    monkeypatch.setattr(claude_cli.asyncio, "sleep", no_sleep)
    pool = claude_cli.ClaudePool()
    cfg = SimpleNamespace(system_instruction="s", response_mime_type=None)
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        asyncio.run(pool.generate(model="claude-future-9", contents="u", config=cfg))
    assert ei.value.kind == "cli_too_old"
    assert calls == [1]


# --- cli_version ------------------------------------------------------------------

def test_cli_version_parses_the_version_line(monkeypatch, fake_exe):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _proc(stdout="2.1.207 (Claude Code)\n")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    assert claude_cli.cli_version() == (2, 1, 207)
    assert captured["argv"] == ["C:/bin/claude.exe", "--version"]
    kw = captured["kwargs"]
    assert kw["creationflags"] == claude_cli._NO_WINDOW
    assert kw["cwd"] == claude_cli.tempfile.gettempdir()
    assert kw["timeout"] > 0
    assert "GEMINI_API_KEYS" not in kw["env"]


def test_cli_version_is_none_when_the_cli_is_missing(monkeypatch):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: None)
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda *a, **k: pytest.fail("no CLI, no subprocess"))
    assert claude_cli.cli_version() is None


@pytest.mark.parametrize("proc", [
    _proc(stdout="Claude Code, some build\n"),
    _proc(stdout=""),
    _proc(returncode=1, stdout="2.1.207 (Claude Code)\n"),
])
def test_cli_version_is_none_when_unparsable(monkeypatch, fake_exe, proc):
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda *a, **k: proc)
    assert claude_cli.cli_version() is None


@pytest.mark.parametrize("exc", [
    claude_cli.subprocess.TimeoutExpired(cmd="claude", timeout=1),
    OSError("WinError 2"),
])
def test_cli_version_is_none_when_the_probe_fails(monkeypatch, fake_exe, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    assert claude_cli.cli_version() is None


def test_conftest_hides_the_real_cli_by_default(monkeypatch):
    """No test can start the real `claude`: find_claude reads None unless a test
    opts in, even when one is on PATH."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: f"/bin/{name}")
    assert claude_cli.find_claude() is None
    assert claude_cli.cli_version() is None


@pytest.mark.real_find_claude
def test_a_test_can_opt_in_to_the_real_lookup(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: f"/bin/{name}")
    assert claude_cli.find_claude() == "/bin/claude"


def test_min_cli_version_names_opus_5_5():
    assert claude_cli.MIN_CLI_VERSION == {NEW: (2, 1, 280)}
