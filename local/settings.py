"""Central settings layer: a single source of truth for user-editable options.

The dashboard and the watcher both read local/config.json. This module
describes WHICH keys are user-tunable (SETTINGS_SCHEMA) and provides safe
load/validate/save that:

  * fall back to a Field's default when a key is absent — or, for a key that
    replaced older ones, to a pure migration function (DERIVED_WHEN_ABSENT),
  * validate types and min/max ranges before writing,
  * MERGE into the existing backing file so keys not in the schema
    (resume_layout, backend, gemini_auth, ...) survive a save,
  * write atomically with a .bak backup so a crash mid-write can't corrupt
    config.json.

The schema is a flat list of Field rows grouped by `section`, backing onto
four target files (config / search / scoring / env — see TARGET_FILES), so the
Qt Settings tab can render one labelled input per row. A Field may also declare
`show_if` — a gate that keeps it off screen until it can actually do something
(is_visible / visible_keys) — `advanced`, which folds a power-user knob behind
the tab's disclosure checkbox, and `restart`, which says a running dashboard
cannot see a new value until it is relaunched. All three are RENDERING decisions
only: load/validate/save never consult any of them, so a hidden field keeps its
value on disk. `pattern` is the exception that proves it: a format rule
validate() DOES enforce, so the tab cannot write free text the consumer would
silently discard.
Every public function accepts an optional `targets` mapping so tests can point
the backing files at a tmp directory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import envfile  # local module: comment-preserving .env reader/writer
from jsonutil import replace_with_retry  # shared Windows-lock-retrying replace
from locks import file_lock              # shared sidecar read-modify-write lock

HERE = Path(__file__).resolve().parent
# settings.py lives in local/, so the repo root (where scraper.py / score_jobs.py
# read their standalone JSON configs) is one level up.
ROOT = HERE.parent


@dataclass(frozen=True)
class Field:
    key: str            # config key (for env-target fields this is the ENV var name)
    label: str          # UI label
    type: str           # "int"|"str"|"bool"|"choice"|"multichoice"|"path"|"list"
    default: Any
    section: str        # "Dashboard"|"Scraper"|"Scoring"|"Resume"|"Credentials"|...
    target: str         # backing-file id (TARGET_FILES): config|search|scoring|env
    help: str = ""
    choices: tuple = ()
    min: float | None = None
    max: float | None = None
    secret: bool = False        # a credential: shown from the local .env, writes the box as-is
    path_kind: str = "dir"      # for type=="path": "dir" picks a folder, "file" picks a file
    optional: bool = False      # UI hint: blank is fine (no value needed to run)
    slider: bool = False        # UI hint: render a bounded int as a drag slider (needs min+max)
    # UI hint: show this field only while another field holds one of these values,
    # e.g. ("provider", ("gemini",)). Declarative DATA, not a callable — Field is
    # frozen, and a tuple is comparable and printable, so the whole gate graph is
    # testable without building a form (see is_visible / visible_keys).
    # PURELY a rendering decision: load(), save() and validate() never consult it,
    # so a hidden field keeps round-tripping its stored value to disk.
    show_if: tuple[str, tuple[str, ...]] | None = None
    # UI hint: a power-user knob, folded away behind the Settings tab's "Show
    # advanced settings" checkbox. Same contract as `show_if` — purely a rendering
    # decision, never consulted by load/save/validate — and the two COMPOSE rather
    # than override: a field is on screen iff
    #   (not advanced or the toggle is on) AND is_visible(field, gate_values)
    # which settings_tab._field_visible is the single place that decides.
    # Defaults False so a new Field is plain unless it opts in: a forgotten flag
    # leaves a setting visible, which is the harmless direction.
    advanced: bool = False
    # A format rule for a free-text field, as schema DATA (like `choices`) rather
    # than a branch inside validate(). Matched against the WHOLE value with
    # `re.fullmatch`, so a rule can't be satisfied by a prefix. Unlike `show_if`
    # and `advanced` this is NOT a rendering hint — validate() enforces it, which
    # is what stops the Settings tab writing a value its consumer would silently
    # discard. `pattern_help` is the sentence the user reads, so it must name the
    # shape ("Use comma-separated whole numbers, e.g. 30,50,70") rather than echo
    # the regex.
    #
    # THE RULE A PATTERN MUST OBEY: reject only what the consumer would silently
    # DISCARD — never a value it honours. This repo is public and others run it,
    # so a rule stricter than the runtime is a lock-out: validate()
    # runs over EVERY collected field, so one already-saved value the editor has
    # newly decided it dislikes blocks every future Save of every OTHER setting,
    # from a row a configuration gate may keep off screen entirely. Write the
    # differential test (editor rejects it ⟺ the consumer loses part of it) rather
    # than eyeballing the regex.
    pattern: str | None = None
    pattern_help: str = ""
    # UI hint: a running dashboard will not see a new value for this key until it
    # is restarted, so the Settings tab chips the row and says so again after the
    # Save. Same contract as `show_if` / `advanced` — a rendering decision that
    # load/save/validate never consult.
    #
    # WHY IT IS TRUE OF (NEARLY) EVERY .env FIELD, which is not obvious and is
    # what made the first cut of this badge cover 3 rows instead of 16:
    # `local/app.py` calls `load_dotenv()` at startup, so this process's
    # `os.environ` is a snapshot of `.env` taken once per launch. Nothing writes
    # it back — `save()` and `envfile` touch the FILE only — and `python-dotenv`
    # defaults to `override=False`, so a later `load_dotenv()` cannot beat a value
    # that is already set. That closes both routes at once: a consumer freezing
    # the value in a module constant at import (`resume_tailor/config.py`,
    # `chrome_launch.CHROME_ACCOUNT`, `scraper.API_TOKEN`) and one reading `os.environ`
    # live on every call (`keypool.KeyPool.from_env`, `llm.py`) get the same stale
    # snapshot — and so does every SUBPROCESS, which inherits a copy of it and
    # then cannot override it from `.env` either. The six VM keys are the one
    # exception: `vm_sync.VMTarget.from_env` reads the file through
    # `settings.load`. Pinned by
    # test_every_env_field_needs_a_restart_except_the_six_the_vm_tab_re_reads.
    #
    # It over-warns in exactly one case, deliberately: a key ABSENT from `.env` at
    # launch is absent from `os.environ` too, so there is nothing for
    # `override=False` to protect and a subprocess started afterwards really does
    # pick the new value up. That is first-run setup, where the user is about to
    # restart anyway — and the flag is per-Field, not per-value, so the honest
    # choice is the one that cannot leave someone staring at a stale model id.
    restart: bool = False
    # For a `choice` field whose readers give a blank a meaning of its own: the
    # dropdown entry that stands for the blank. The Settings tab lists it after
    # the choices, opens a stored blank on it and writes it back blank, and
    # `field_problem` accepts the blank. Left empty, a stored blank opens on the
    # default (settings_tab._match_choice). Pinned by
    # test_a_blank_entry_is_declared_only_on_a_choice_field_and_names_no_choice.
    blank_label: str = ""


# Targets whose backing file is a .env (key=value), not JSON. Their Field.key is
# the literal environment-variable name, so values round-trip straight to .env.
ENV_TARGETS = {"env"}

# Same character class envfile._CONTROL_RE refuses on write; see validate().
# It is everything `str.splitlines()` breaks on -- the C0 controls plus U+0085,
# U+2028 and U+2029, which sit outside \x00-\x1f and still split a line.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")

# The field types whose value is a plain string, i.e. the only ones a
# `Field.pattern` can be matched against. Pinned by
# test_a_pattern_is_only_declared_on_a_text_field so a pattern on, say, a list
# field is caught by the schema lint rather than by a TypeError in validate().
TEXT_TYPES = ("str", "path", "editable_choice")

# Gemini model ids offered in the model dropdowns (the recent 3.x family). These
# are EDITABLE dropdowns ("editable_choice"): pick one or type a custom id, so a
# new model id is never blocked — and a wrong pick can't silently break scoring.
# The pro tier is the "-preview" id on purpose: Google has not shipped a stable
# GA gemini-3.1-pro (and gemini-3.5-pro doesn't resolve on Vertex projects), so
# the preview id is the only pro-tier option. It is never a default — only an
# opt-in "max quality" choice. Re-verified against Google's deprecation table on
# 2026-09-04: no GA 3.1-pro/3.5-pro exists, and the preview id is still active.
# Every other id here is on Google's Stable list.
#
# gemini-3.1-flash-lite is the one id in this tuple carrying a shutdown date:
# Google's lifecycle table reads `gemini-3.1-flash-lite | May 7, 2026 | May 7, 2027 |
# gemini-3.5-flash-lite` (re-read 2026-09-15; the date is the earliest possible
# retirement and the model is still on the Stable list). It stays in the tuple as
# an opt-in so stored configs that name it keep resolving, but since v1.12.0
# (2026-09-18, the author's call) the stage-1 scoring default and the tailor's
# flash-lite tier both sit on gemini-3.5-flash-lite, its named replacement.
# keypool.LIMITS carries a 15/500 row for it (read from the account's AI Studio
# dashboard), the same gate the old default had, so the swap costs no throughput.
#
# The newer flash ids (3.8-flash, 3.7-flash, 3.6-flash) are offered as opt-in
# choices, and the defaults deliberately stay on the flash-lite/flash pair the VM
# scores with. Price alone would argue for moving the
# stage-2 default off 3.5-flash, since 3.6/3.7/3.8-flash list at $0.75/$3.75 per
# 1M through 2026-12-31 against 3.5-flash's $1.50/$9.00. Two reasons it stays
# anyway: scoring normally runs on the free-tier key pool rather than paid credit,
# so list price is not what this pipeline pays; and a 2026-06-11 downgrade of
# exactly these two defaults was reversed eight days later on quality grounds.
# Changing the pair is a re-tune with a scoring run behind it, not a version bump.
# keypool.LIMITS has a free-tier rpm/rpd row for every Flash id in this tuple
# (test_every_gemini_models_choice_has_a_real_limits_row pins that); only the pro
# preview falls to keypool.DEFAULT_LIMITS, on purpose.
GEMINI_MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash",
                 "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
                 "gemini-3.1-pro-preview")

# Claude model ids offered in the Claude model dropdowns (also editable_choice).
# These are passed to the Claude Code CLI, not to the Anthropic API directly, so
# the API-side deprecation of temperature/top_p/top_k on Opus 4.7 and later does
# not reach this project: llm._call_claude accepts those arguments for signature
# parity with _call_gemini and drops them, because print-mode exposes neither.
# The three ids the tuple held then (haiku 4.5, sonnet 5, opus 5) were re-checked
# against Anthropic's model-deprecation table on 2026-09-04: state Active, none
# deprecated, none retired. The tier map (haiku fast, sonnet standard, opus deep)
# still matches what each tier is for. The nearest horizon in the whole model
# set, Gemini included, is claude-haiku-4-5-20251001's tentative retirement "not
# sooner than October 15, 2026"; Anthropic gives at least 60 days' notice and
# there is no newer haiku to move to, so the id stays and this comment is the
# reminder to re-check it.
#
# claude-opus-5-5 joined in cycle 19 (2026-09-27) as the deep tier's default
# (RESUME_TAILOR_CLAUDE_MODEL_PRO here and resume_tailor/config.py's tier map).
# claude-opus-5 stays in the tuple, so a stored config naming it keeps resolving
# and it stays one pick away in all six Claude dropdowns.
#
# claude-sonnet-5-5 joined in cycle 21 (2026-09-29) as an option in all six Claude
# dropdowns; no default changed.
#
# Re-checked 2026-10-03 against the same table: all five ids are Active, none
# deprecated. claude-haiku-4-5-20251001 still reads "Not sooner than October 15,
# 2026" with no newer haiku listed, so it stays the fast tier's default.
CLAUDE_MODELS = ("claude-haiku-4-5", "claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5",
                 "claude-opus-5-5")

# How the résumé tailor picks a model for each stage, per provider. The strings
# are the ones `local/resume_tailor/config.py` compares against
# (config.MODEL_MODE_TIERS / MODEL_MODE_SIMPLE) — the coupling that makes the
# dropdown mean anything — and this is a plain `choice`, not `editable_choice`,
# because a value outside the pair is a typo the runtime would silently read
# as 'tiers'.
#
# Index 0 is 'tiers', the default, because it is the mode that reproduces what
# every install did before this setting existed. A stored value in another case
# ('Simple') reads as its match and a blank one as 'tiers'; any other stored
# value stays on screen as typed and Save flags it (settings_tab._set_combo).
MODEL_MODE_TIERS = "tiers"
MODEL_MODE_SIMPLE = "simple"
MODEL_MODES = (MODEL_MODE_TIERS, MODEL_MODE_SIMPLE)

# Settings-snapshot retention: the choices of the archive_mode setting, which
# replaced a four-key retention DSL (archive_enabled + archive_prune_mode +
# archive_prune_keep + archive_prune_days). ARCHIVE_KEEP_ALL is deliberately the
# SAME string as settings_archive.PRUNE_OFF, so the pruner's existing "unknown
# mode deletes nothing" arm makes the default reproduce today's behaviour exactly.
# The counted modes carry their count IN the string, which is what let
# settings_archive.prune() keep its signature (see _keep_from_mode there).
ARCHIVE_KEEP_ALL = "Keep everything"
ARCHIVE_KEEP_20 = "Keep newest 20"
ARCHIVE_KEEP_100 = "Keep newest 100"
ARCHIVE_OFF = "Off"
# Index 0 is the default, the mode that deletes nothing. A blank reads as that
# default; a hand-edited value that matches none of them stays on screen as typed
# and Save flags it (settings_tab._set_combo), so a typo changes no snapshots
# until the user picks one of these four.
ARCHIVE_MODES = (ARCHIVE_KEEP_ALL, ARCHIVE_KEEP_20, ARCHIVE_KEEP_100, ARCHIVE_OFF)


# Backing files, keyed by Field.target. The Scraper/Scoring sections write the
# root-level configs that scraper.py / score_jobs.py read standalone on the VM.
TARGET_FILES: dict[str, Path] = {
    "config": HERE / "config.json",
    "search": ROOT / "search_config.json",
    "scoring": ROOT / "scoring_config.json",
    # No "apply" target: zero fields ever pointed at apply_config.json, and the
    # apply pipeline reads that legacy file itself (resume_tailor.apply_config
    # .load_apply_config opens the repo-root path directly and merges over its own
    # DEFAULTS), so nothing here needs to know about it. Consequence: a legacy
    # root apply_config.json is not copied into settings snapshots, the same
    # treatment apply_answers.json, the LIVE answer store, gets.
    # Secrets, identity, and paths live in the git-ignored .env at the repo root,
    # the same file scraper.py / score_jobs.py / the tailor load at runtime.
    "env": ROOT / ".env",
}

# The graduation month's format rule (the "About you" section), a copy of the
# month grammar in score_jobs.GRADUATION_MONTH_RE with a blank allowed: a month
# name or its three-letter form (Sept and Sep too), an optional period, then a
# four-digit year, with spaces around it all. score_jobs is copied alone to the
# VM and this module does not import it, so the text is duplicated and
# test_candidate_settings.py holds the two together with a differential test.
# The trailing \s* sits inside the optional group: two adjacent \s* around an
# optional group backtrack quadratically on a long run of spaces ending in a
# stray character, and test_candidate_settings.py pins the run-time on 50,000.
_GRADUATION_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
                      r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?"
                      r"|dec(?:ember)?")
GRADUATION_MONTH_PATTERN = (r"(?i)\s*(?:(?:" + _GRADUATION_MONTHS
                            + r")\.?\s+(?:19|20)\d{2}\s*)?")

# The rollover rule the scorer applies (score_jobs.candidate_profile): an in-school
# status whose graduation month is before this month counts as Finished school. The
# Settings tab names it at Save, because the shipped default month is already in the
# past and the switch is silent. This is a copy of parse_graduation_month and of the
# rollover comparison (score_jobs is copied alone to the VM and this module never
# imports it); test_candidate_settings.py holds both to the scorer's own answer.
_GRADUATION_MONTH_RE = re.compile(
    r"\s*(" + _GRADUATION_MONTHS + r")\.?\s+((?:19|20)\d{2})\s*", re.I)
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")
# The two in-school labels and their codes, whitespace-folded and casefolded.
_IN_SCHOOL_STATUSES = frozenset(("in school: undergraduate", "in school: graduate",
                                 "undergrad", "grad"))


def parse_graduation_month(text: Any) -> tuple[int, int] | None:
    """(year, month) for "May 2026", "Sept. 2027" and the like; None otherwise.

    The same answer as score_jobs.parse_graduation_month, including for a
    non-string (None) and for the casefold spellings the (?i) match admits.
    """
    if not isinstance(text, str):
        return None
    m = _GRADUATION_MONTH_RE.fullmatch(text)
    if not m:
        return None
    prefix = m.group(1)[:3].casefold()
    month = next((i for i, name in enumerate(_MONTH_NAMES, 1)
                  if name[:3].casefold() == prefix), None)
    return None if month is None else (int(m.group(2)), month)


def graduation_month_passed(status: Any, month: Any, today: date | None = None) -> str | None:
    """The month as "May 2026" when the scorer would count an in-school `status` as
    finished for it (the month is before today's), else None.

    A finished status, an unknown one, a blank month and a month that does not parse
    all return None, as does the current month and any later one.
    """
    if not isinstance(status, str) or " ".join(status.split()).casefold() not in _IN_SCHOOL_STATUSES:
        return None
    grad = parse_graduation_month(month)
    if grad is None:
        return None
    now = today if today is not None else datetime.now().date()
    if grad < (now.year, now.month):
        return f"{_MONTH_NAMES[grad[1] - 1]} {grad[0]}"
    return None


SETTINGS_SCHEMA: list[Field] = [
    # --- Jev (cycle 19): one switch for every Jev use, read by local/jev_switch.py
    # at call time from config.json, so a flip needs no restart. The master
    # switch stays in plain sight; the three area switches fold under advanced
    # and gate on it, so the chain hides them (and the Resume section's three
    # Jev options, which gate on jev_tailor) while Jev is off. A bool gate
    # spells its choices ("True", "False"): settings_tab._connect_gate_signals
    # watches a checkbox gate through them. Auto-apply runs on Jev alone, so it
    # has no area switch here and only the master turns it off.
    Field("jev_enabled", "Use Jev", "bool", True, "Jev", "config",
          choices=("True", "False"),
          help="Jev is the TypeSafe judge. On: it scores collected jobs on the scorer's "
               "own 1-5 and 1-10 scales, picks and checks résumé bullets (your tailor "
               "provider still writes each one), rates how hard each queued application "
               "is, and reads every auto-apply form. Off: scoring and tailoring run on "
               "Gemini or Claude alone, as they did before Jev, and auto-apply cannot "
               "start. Takes effect on the next run."),
    # The key row sits in the section whose feature spends it, with the same
    # secret/env/restart shape as the Credentials rows: Field.key is the exact
    # environment-variable name jev.TypeSafeJev reads. The apply_run child
    # loads `.env` without overriding what it inherits, so a first paste reaches
    # it and a rotated key sits behind the dashboard's startup snapshot until a
    # restart (hence `restart`). Never gated: a key pasted while Jev is off is
    # there when it comes back on.
    Field("TYPESAFE_API_KEY", "TypeSafe API key (Jev judge)", "str", "",
          "Jev", "env", secret=True, optional=True, restart=True,
          help="Needed while 'Use Jev' is on: every Jev judgment in scoring, tailoring, "
               "the difficulty check and auto-apply goes to the TypeSafe Jev model with "
               "this key. Create a key at console.typesafe.ai/keys; it bills $0.042 per "
               "million input tokens (output is free)."),
    Field("jev_scoring", "Jev for scoring", "bool", True, "Jev", "config",
          show_if=("jev_enabled", ("True",)), advanced=True,
          help="Jev scores each collected job against your résumé in both scoring "
               "stages, on the same scales the Scoring provider uses. Off: the "
               "Scoring provider scores every job on its own. Takes effect on the "
               "next scoring run."),
    Field("jev_tailor", "Jev for the résumé tailor", "bool", True, "Jev", "config",
          choices=("True", "False"), show_if=("jev_enabled", ("True",)), advanced=True,
          help="Jev picks your skills lines, shortlists the experience a job asks for, "
               "and checks each rewritten bullet against what you wrote. Off: the "
               "Résumé tailor provider does every step itself. The three Jev options "
               "in the Resume section need this on."),
    Field("jev_difficulty", "Jev difficulty check", "bool", True, "Jev", "config",
          choices=("True", "False"), show_if=("jev_enabled", ("True",)), advanced=True,
          help="The Auto-apply tab's difficulty check: it opens each queued posting, "
               "reads the first application page without typing anything, and rates "
               "it 1 to 10 so you know which jobs to leave to the run. About 2 to 4 "
               "Jev requests per job. Off hides it."),

    Field("min_score", "Min score to highlight", "int", 4, "Dashboard", "config",
          help="Jobs at/above this score are surfaced as high-priority.", min=1, max=5),
    Field("repost_window_days", "Hide reposts for (days)", "int", 30, "Dashboard", "config",
          help="A job with the same title, company and location as one you marked seen or "
               "applied within this many days stays out of High Score, and unseen duplicates "
               "collapse to the newest posting. Nothing is marked; after the window the repost "
               "shows again. 0 turns it off.",
          min=0, max=180),
    Field("followup_days", "Follow-up after (days)", "int", 5, "Dashboard", "config",
          help="Days after applying before the tracker nudges a follow-up.", min=1, max=60,
          slider=True),
    Field("gdrive_root", "Job data folder", "path", "", "Dashboard", "config",
          help="Folder the dashboard reads scored CSVs from."),
    # No mtime_stable_seconds row: the sync debounce is watcher-only (see
    # watcher.DEFAULT_CONFIG), never read by the dashboard, so it is not a
    # user-tunable dashboard setting. watcher.load_config() reads config.json
    # directly, so a saved value still applies and an absent key falls back to 30.
    Field("stale_after_hours", "Flag data as stale after (hours)", "int", 36,
          "Dashboard", "config", advanced=True,
          help="The Stats tab warns that the pipeline may have failed when the newest run is "
               "older than this. Discovery runs a few times a day, so 36h means a missed "
               "day stands out.", min=6, max=336),
    Field("apply_open_browser", "Open the posting in your browser on Apply", "bool", True,
          "Dashboard", "config",
          help="When on, the Apply button opens the job's application page in Chrome as well "
               "as showing the apply sheet. Off keeps you in the dashboard; the posting URL "
               "is still on the apply sheet and the Open-folder button still works."),

    # --- About you (cycle 21): the candidate's school status and clearance, written
    # to root-level scoring_config.json. score_jobs.candidate_profile() resolves
    # these four keys into the profile the scorer's prompts and filters read. The
    # two dropdown lists and every default mirror score_jobs (EDUCATION_STATUSES,
    # CLEARANCE_LEVELS, _SCORING_DEFAULTS); tests/test_candidate_settings.py pins
    # each of them against the scorer, and the month rule below against
    # parse_graduation_month, so this module never imports score_jobs.
    Field("education_status", "School status", "choice", "Finished school", "About you",
          "scoring", choices=("Finished school", "In school: undergraduate",
                              "In school: graduate"),
          help="Where you are in school, stated to the scorer the way it already knows "
               "today's date. Finished school drops internship and co-op titles before "
               "scoring, and postings for current students score 1. In school keeps "
               "internships and co-ops, and postings for a different degree level score 1. "
               "Graduate means a master's or PhD program."),
    # The pattern is drawn at what score_jobs.parse_graduation_month keeps, and a
    # blank is kept too: the scorer reads it as "no date", so the row must let it
    # save. validate() runs re.fullmatch(pattern, value) with NO flags, so the
    # case-insensitivity the scorer gets from re.I travels inline as (?i).
    Field("graduation_month", "Graduation month", "str", "May 2026", "About you",
          "scoring", optional=True, pattern=GRADUATION_MONTH_PATTERN,
          pattern_help="A month and year, such as May 2026.",
          help="The month you graduated or expect to, such as May 2026. Postings limited "
               "to a graduation window this date falls outside score 1. An in-school "
               "status whose month has passed counts as finished school. Leave it blank "
               "to leave the date out."),
    Field("clearance_level", "Security clearance held", "choice", "None", "About you",
          "scoring", choices=("None", "Public Trust", "Secret", "Top Secret", "TS/SCI"),
          help="The active US security clearance you hold. Postings that need this level "
               "or a lower one are kept and scored. Postings that need a higher level are "
               "dropped before scoring, unless the open box is on and the employer "
               "sponsors it."),
    Field("clearance_sponsorship", "Open to getting a clearance through the employer",
          "bool", False, "About you", "scoring",
          help="On: postings that sponsor a clearance, or ask for the ability to obtain "
               "one, are kept and scored. Postings that need an active clearance you do "
               "not hold are still dropped."),

    # --- Scraper: written to root-level search_config.json (read by scraper.py) ---
    Field("keywords", "Search keywords", "list",
          ['"Data Scientist"', '"AI Engineer"', '"AI Developer"', '"AI Scientist"',
           '"Software Engineer"', '"Software Developer"', '"Data Analyst"',
           '"Data Engineer"', '"LLM"', '"Analytics Engineer"', '"Decision Scientist"',
           '"Generative AI"', '"Gen AI"', '"GenAI"', '"Quant"',
           '"Implementation Engineer"', '"Agentic"', '"Applied AI"',
           '"Artificial Intelligence"', '"Business Analyst"'],
          "Scraper", "search",
          help="One search phrase per line. Wrap any phrase of two or more words in "
               '"double quotes" (e.g. "Data Scientist") so it is searched as a phrase; '
               "single words need no quotes. Each line runs once per ticked remote type."),
    Field("remote_types", "Remote types", "multichoice", ["Hybrid", "On-site"],
          "Scraper", "search",
          help="Which workplace types to search. Each ticked type runs once per keyword.",
          choices=("On-site", "Remote", "Hybrid")),
    Field("limit_per_input", "Postings per search", "int", 100, "Scraper", "search",
          help="Max postings collected per (keyword x remote type). Higher = more spend.",
          min=1, max=500),
    Field("location", "Location", "str", "United States", "Scraper", "search",
          help="Geographic location filter for searches."),
    # NOT advanced, deliberately: it sits beside `location` and is what a non-US
    # user must change. location="United Kingdom" with country="US" silently
    # mis-searches — plausible results, no error — so nothing else tells them.
    Field("country", "Country code", "str", "US", "Scraper", "search",
          help="Two-letter country code (e.g. US, GB, CA)."),
    Field("time_range", "Time range", "choice", "Past 24 hours", "Scraper", "search",
          help="Only collect postings newer than this. Widening beyond 'Past 24 hours' "
               "raises the chance of re-collecting (and re-paying for) postings older "
               "than the 90-day exclude window.",
          choices=("Past 24 hours", "Past week", "Past month", "Any time")),
    Field("job_type", "Job type", "choice", "Full-time", "Scraper", "search",
          help="Employment type to search for.",
          choices=("Full-time", "Part-time", "Contract", "Temporary",
                   "Internship", "Volunteer", "Other")),
    Field("experience_level", "Experience level", "choice", "Entry level", "Scraper", "search",
          help="Seniority filter.",
          choices=("Internship", "Entry level", "Associate",
                   "Mid-Senior level", "Director", "Executive")),
    Field("drop_easy_apply", "Drop Easy Apply jobs before scoring", "bool", False,
          "Scraper", "scoring",
          help="When on, Easy Apply jobs are filtered out before AI scoring (saves "
               "scoring tokens). They stay in the master list as filtered so they are "
               "never re-collected or re-billed. If you use the cloud VM, push config "
               "to the VM after changing this, and re-upload score_jobs.py once."),

    # --- Scoring: written to root-level scoring_config.json (read by score_jobs.py) ---
    # `provider` leads the section (cycle 19's ST-2) and gates the two model PAIRS
    # after it: only the pair the chosen provider uses is on screen, and the other
    # pair keeps its stored value.
    Field("provider", "Scoring provider", "choice", "gemini", "Scoring", "scoring",
          help="Which AI service scores jobs when Jev is off, and the fallback when Jev "
               "is down. 'claude' uses your local Claude Code CLI (subscription). This "
               "setting IS pushed to the cloud VM, but the VM has no claude CLI "
               "installed, so it falls back to Gemini there. Applies from the next "
               "scoring run.",
          choices=("gemini", "claude")),
    Field("stage1_model", "Stage-1 model", "editable_choice", "gemini-3.5-flash-lite",
          "Scoring", "scoring", choices=GEMINI_MODELS, show_if=("provider", ("gemini",)),
          advanced=True,
          help="Cheap model that scores every surviving job 1-5. Pick from the "
               "list or type a model ID your account can use (a wrong name silently "
               "breaks scoring)."),
    Field("stage2_model", "Stage-2 model", "editable_choice", "gemini-3.5-flash",
          "Scoring", "scoring", choices=GEMINI_MODELS, show_if=("provider", ("gemini",)),
          advanced=True,
          help="Deeper model for jobs that pass the Stage-2 threshold. Pick from "
               "the list or type a model ID your account can use (a wrong name silently "
               "breaks scoring)."),
    # Free-tier quota is metered per (API key, MODEL), not per key -- one key
    # exhausted on gemini-3.8-flash still has its whole gemini-3.7-flash
    # allowance. So naming extra models multiplies the day's free calls
    # (models x keys) instead of splitting one allowance, and the pool only
    # reaches the PAID Vertex backstop once every pair is spent. Empty = the
    # single-model behaviour these lists replaced.
    Field("stage1_models", "Stage-1 fallback models", "list", [],
          "Scoring", "scoring", show_if=("provider", ("gemini",)), advanced=True,
          help="Extra models Stage 1 may use once 'Stage-1 model' runs out of free "
               "requests, one per line, best first. Each model has its OWN free daily "
               "quota per API key, so two models double the free calls. Give any model "
               "the dropdown doesn't list a 'Per-model rate limits' row too."),
    Field("stage2_models", "Stage-2 fallback models", "list", [],
          "Scoring", "scoring", show_if=("provider", ("gemini",)), advanced=True,
          help="Extra models Stage 2 may use once 'Stage-2 model' runs out of free "
               "requests, one per line, best first. Stage 2 is the expensive stage: the "
               "full Flash models allow only 20 requests/day each, so listing three of "
               "them is 4x the deep scores before anything is billed."),
    Field("stage1_model_claude", "Stage-1 model (Claude)", "editable_choice",
          "claude-haiku-4-5", "Scoring", "scoring", choices=CLAUDE_MODELS,
          show_if=("provider", ("claude",)), advanced=True,
          help="Used only when Scoring provider is 'claude'."),
    Field("stage2_model_claude", "Stage-2 model (Claude)", "editable_choice",
          "claude-sonnet-5", "Scoring", "scoring", choices=CLAUDE_MODELS,
          show_if=("provider", ("claude",)), advanced=True,
          help="Used only when Scoring provider is 'claude'."),
    # The ONLY rate-limit control, keyed by model. It replaced a per-stage
    # rpm/rpd pair per stage, which could describe a stage's PRIMARY model and
    # nothing else -- not the ranked fallbacks, and not a model both stages name
    # (those collapsed last-write-wins, which fired for real: two stages on
    # gemini-3.5-flash-lite left it gated at stage 2's 5/20 instead of 15/500).
    # Normally left empty; keypool.LIMITS covers every Flash model in the
    # dropdown (the pro preview is the one id gated at DEFAULT_LIMITS).
    Field("model_limits", "Per-model rate limits", "list", [],
          "Scoring", "scoring", show_if=("provider", ("gemini",)), advanced=True,
          help="One 'model requests-per-minute requests-per-day' per line, e.g. "
               "'gemini-3.8-flash 5 20'. For that model it replaces the built-in "
               "free-tier table, for the scorer and the resume tailor alike. Needed "
               "only for a model the built-in table doesn't know, or when Google "
               "changes an allowance. A line without two whole numbers after the "
               "model id is skipped with a warning. See aistudio.google.com/rate-limit."),
    Field("stage1_concurrency", "Stage-1 concurrency", "int", 6, "Scoring", "scoring",
          advanced=True,
          help="Parallel Stage-1 LLM calls.", min=1, max=50, slider=True),
    Field("stage2_concurrency", "Stage-2 concurrency", "int", 4, "Scoring", "scoring",
          advanced=True,
          help="Parallel Stage-2 LLM calls.", min=1, max=50, slider=True),
    Field("stage2_threshold", "Stage-2 threshold", "int", 4, "Scoring", "scoring",
          help="Stage-1 score at/above which a job gets deep Stage-2 analysis.", min=1, max=5,
          slider=True),
    Field("jev_writer", "Jev writer for high scores", "bool", True, "Scoring", "scoring",
          advanced=True, help="When Jev scores a job at/above the stage 2 threshold (4 by "
          "default), the Scoring provider's stage 1 model (Claude Haiku or Gemini Flash-Lite by "
          "default) writes its reason, strengths and gaps. On the Claude provider: 30-100 seconds "
          "and about $0.04 at Haiku list prices per job. Off: those jobs keep Jev's "
          "code-written notes, and the provider runs only when Jev cannot score a job."),
    # NOT advanced, deliberately: this is the only ceiling on an LLM bill, so it
    # stays where a user worried about spend can find it without first learning
    # that a disclosure toggle exists. `rescore_cap` below reads like its twin but
    # is retry-of-failures plumbing — advanced.
    Field("max_scored_per_run", "Max scored per run", "int", 800, "Scoring", "scoring",
          help="Spend guard: cap on LLM-scored jobs per run.", min=1, max=5000),
    Field("rescore_cap", "Rescore cap", "int", 200, "Scoring", "scoring", advanced=True,
          help="Spend guard: cap on failed/missing master rows retried per run.", min=0, max=5000),
    Field("min_filter_years", "Min required years cutoff", "int", 1, "Scoring", "scoring",
          help="Roles requiring at least this many years of experience are filtered out.",
          min=0, max=20, slider=True),
    Field("repost_reuse_days", "Repost score reuse window (days)", "int", 30, "Scoring",
          "scoring", advanced=True,
          help="Reuse a matching prior score for a repost within this many days. 0 turns "
               "reuse off, so every repost is scored fresh.", min=0, max=180, slider=True),

    # --- Resume: artifact toggles + cover-letter tone (config.json) ---
    # Layout controls live in the Resume Data tab's "Resume Layout (bullet sizing)"
    # section: per-bullet line budgets AND the project count + at-most/exactly-N
    # mode (jobsdata.save_projects_count). Required sections come from the
    # master_experience yaml `tailor:` block. Here: artifact toggles, tone, and
    # post-tailor UX only.
    Field("tailor_open_folder", "Open output folder after tailoring", "bool", False,
          "Resume", "config",
          help="When on, the tailored résumé's folder opens in File Explorer after each run. "
               "Off (default) keeps the screen tidy when you tailor several jobs at once; reach "
               "the folder from the Apply panel's 'Open folder' button or the path in the status bar."),
    Field("tailor_ats_report", "Write ATS report", "bool", True, "Resume", "config",
          help="Write ats_report.txt (keyword coverage) for each tailored résumé."),
    Field("tailor_prep_sheet", "Generate interview-prep sheet", "bool", False, "Resume", "config",
          help="Also generate the interview-prep sheet during tailoring (otherwise "
               "it's on-demand via the Interview prep button)."),
    Field("resume_tone", "Cover-letter tone", "choice", "professional", "Resume", "config",
          help="Tone used when generating the cover letter.",
          choices=("professional", "concise", "enthusiastic", "impactful")),
    Field("cover_letter_avoid_ai_writing", "Strip AI writing patterns from the cover letter",
          "bool", True, "Resume", "config",
          help="Checks the COVER LETTER for the overused AI vocabulary, hedging and "
               "chatbot tics that make writing read as machine-made, and buys one repair "
               "call when one slips through (résumé bullets have their own pass). The "
               "letter prompts carry these rules either way; this decides whether the "
               "check enforces them. docs/USER_GUIDE.md lists exactly what it catches."),
    Field("resume_aiwriting_sweep", "Strip AI writing patterns from the résumé bullets",
          "bool", True, "Resume", "config",
          help="Sends each résumé entry to the model as a whole to clear the tells that "
               "only show up across its bullets: one sentence shape reused down the "
               "list, every bullet the same length, a three-part series in each line. "
               "COSTS ONE MODEL CALL PER RÉSUMÉ ENTRY ON EVERY TAILOR RUN. Turn it off "
               "to save that; the always-on per-bullet style gate is unaffected."),
    # The tailor's three Jev options (cycle 19, TL-7 to TL-9), each off by default
    # and on screen only while Jev runs for the tailor: they gate on jev_tailor,
    # which gates on jev_enabled. Read through resume_tailor/config.py's
    # best_of_n(), cover_letter_jev_check() and ats_meaning().
    Field("tailor_best_of_n", "Best of 3 bullet drafts (Jev picks)", "bool", False,
          "Resume", "config", show_if=("jev_tailor", ("True",)),
          help="The rewrite asks for 3 drafts of each bullet, and Jev keeps the one "
               "that shows the most of what the job asks for, among the drafts that "
               "pass the faithfulness check. That call's model output costs about "
               "three times as much."),
    Field("cover_letter_jev_check", "Jev checks the cover letter's claims", "bool", False,
          "Resume", "config", show_if=("jev_tailor", ("True",)),
          help="Jev reads each sentence of the cover letter against your selected "
               "experience and background notes. A sentence that claims something "
               "about you they do not state goes to the letter's repair step."),
    Field("tailor_ats_meaning", "ATS report: coverage by meaning (Jev)", "bool", False,
          "Resume", "config", show_if=("jev_tailor", ("True",)),
          help="Adds a line to ats_report.txt counting the job's keywords your résumé "
               "covers by meaning (a direct equivalent counts), beside the literal "
               "count. Jev judges each keyword. Needs 'Write ATS report' on."),

    # --- Auto-apply: the batch queue knobs (config.json). Read by the dashboard's
    # _queue_for_auto_apply and by apply_queue.build_context() for the agent run. ---
    Field("auto_apply_batch_cap", "Max jobs queued per batch", "int", 10,
          "Auto-apply", "config", min=1, max=25,
          help="At most this many jobs go into the auto-apply queue per 'Queue for "
               "auto-apply' action, and the most one drain works through. ~10 keeps a "
               "batch reviewable in one sitting: the run submits when 'Submit when "
               "verified' passes and parks the rest at their review page for you."),
    # The parallel difficulty check (cycle 22, local/assess_pool.py): on screen
    # only while the check itself is, the same gate that hides its button.
    # apply_run.DEFAULT_SETTINGS carries the same default, and apply_assess.py's
    # --parallel overrides it for one run.
    Field("auto_apply_check_parallel", "Difficulty checks at once", "int", 10,
          "Auto-apply", "config", min=1, max=10,
          show_if=("jev_difficulty", ("True",)),
          help="How many jobs 'Check difficulty' reads at the same time. Each check "
               "opens its own browser window on a temporary copy of the auto-apply "
               "profile, and the copies are deleted when the check ends. A lower "
               "number uses less memory. 1 checks one job at a time in the "
               "auto-apply profile itself."),
    # No auto_apply_inbox_url row: the single fallback URL duplicated the map
    # below, firing only when the signup domain missed it — and the shipped
    # DEFAULT_INBOX_MAP already covers the common providers, so an unmapped domain
    # is one line in the map, exactly as easy as a fallback URL. apply_queue
    # .build_context() still HONOURS a saved auto_apply_inbox_url (it reads
    # config.json directly and keeps its own DEFAULT_INBOX_URL), so nobody's
    # existing configuration changed behaviour. The provider defaults below must
    # stay in sync with apply_queue.DEFAULT_INBOX_MAP —
    # test_inbox_map_default_matches_apply_queue pins that.
    Field("auto_apply_inbox_map", "Inbox by email domain", "list",
          ["gmail.com https://mail.google.com",
           "googlemail.com https://mail.google.com",
           "outlook.com https://outlook.live.com/mail/",
           "hotmail.com https://outlook.live.com/mail/",
           "live.com https://outlook.live.com/mail/",
           "msn.com https://outlook.live.com/mail/"],
          "Auto-apply", "config",
          help="One 'emaildomain webmail-url' per line, so the agent opens the RIGHT "
               "inbox for the email it signs up with. Only consumer providers are "
               "seeded; add your work or school domain, e.g. 'yourschool.edu "
               "https://outlook.office.com/mail/' for Microsoft 365. Your signup "
               "email's domain (basics.email) is looked up here; a domain not listed "
               "falls back to Gmail. That inbox must already be signed in in Chrome."),
    # The Jev-judged run (cycle 16, local/jev.py + local/apply_run.py). Its key row
    # moved to the Jev section in cycle 19, since every Jev use spends it. The
    # judge mode stays in this section under advanced (ST-5): its one other
    # choice is the test judge the drain refuses.
    Field("auto_apply_jev_mode", "Auto-apply judge", "choice", "typesafe",
          "Auto-apply", "config", choices=("typesafe", "fake"), advanced=True,
          help="'typesafe' judges each page with the Jev model (needs the API key). "
               "'fake' is a test-only judge that never calls the API: it answers from word "
               "overlap and the drain refuses it. For a dry run keep 'typesafe' and turn "
               "off 'Submit when verified' (or pass --no-submit)."),
    Field("auto_apply_submit", "Submit when verified", "bool", True,
          "Auto-apply", "config",
          help="Submit an application when every required field is filled from your "
               "answers, verified, and no CAPTCHA, payment, or blocked question appeared. "
               "Off: park at the review page for you."),
    # The pause (cycle 19, PR): how long a run waits for the user's answer before it
    # parks the job. 0 is the behaviour every run had before the pause existed.
    # apply_run.DEFAULT_SETTINGS carries the same default for a run with no config.
    Field("auto_apply_pause_minutes", "Wait for your answer (minutes)", "int", 10,
          "Auto-apply", "config", min=0, max=60,
          help="When a run meets a question it needs you for (a required field your "
               "answers cannot fill, a tie between options, a field only you should "
               "type), it waits this many minutes for your answer in the Auto-apply tab "
               "or the browser, then parks the job. 0 parks at once."),
    Field("auto_apply_generate", "Draft free-text answers", "bool", True,
          "Auto-apply", "config",
          help="Draft an answer for a required open-ended question (your motivation, a "
               "project you are proud of) from your apply sheet with one flash-lite model "
               "call, then have the judge check every sentence against the sheet; a draft "
               "with an unsupported sentence is dropped and the field is left for you. An "
               "optional one stays blank. At most 3 drafts per job. Off: every such field "
               "is left for you."),
    Field("auto_apply_headless", "Hide the browser window", "bool", False,
          "Auto-apply", "config",
          help="Run the auto-apply Chromium with no visible window. Off (the default) "
               "shows the window so you can watch each application fill in and step in "
               "when it parks."),

    # --- Settings history: snapshot every Save so settings can be rolled back ---
    # Lives in local/config.json. Snapshots copy every settings file (including the
    # secret-bearing .env) into a git-ignored settings_archive/.
    #
    # ONE choice replaced four keys (archive_enabled + archive_prune_mode +
    # archive_prune_keep + archive_prune_days) — a retention DSL for folders
    # holding a few KB of text, two of whose knobs were inert under the default
    # mode. Age-based retention is dropped. The default reproduces the old
    # effective behaviour, `_legacy_archive_mode` below migrates an older config,
    # and the four old keys are NOT reaped from config.json (save() merges), so
    # checking out an older commit restores the old behaviour intact.
    Field("archive_mode", "Settings snapshots", "choice", ARCHIVE_KEEP_ALL,
          "Settings history", "config", choices=ARCHIVE_MODES,
          help="Each Save copies all your settings into a dated folder you can roll back "
               "to. 'Keep everything' (the default) never deletes one; a 'Keep newest' "
               "option deletes older ones on each Save; 'Off' stops taking new snapshots "
               "and leaves existing ones alone. Each snapshot holds a copy of your .env, "
               "so more snapshots means more copies of your keys on disk."),

    # Apply-form answers (work auth, sponsorship, EEO, "how did you hear") are NOT
    # configured here. They live in the richer Apply Answers tab (per-question,
    # fixed/open-ended, needs-review), which writes apply_answers.json — the single
    # source of truth the apply pipeline reads. apply_config.DEFAULTS only seeds
    # that store on first run.

    # --- Credentials: API keys / tokens, written to the git-ignored .env -------
    # secret=True fields show their saved value in the GUI (read straight from the
    # local .env) and write whatever the box holds — clearing it removes the key.
    # Field.key is the exact environment-variable name the pipeline reads.
    Field("BRIGHT_DATA_API_TOKEN", "Job-data API token", "str", "",
          "Credentials", "env", secret=True, optional=True, restart=True,
          help="Needed for job discovery. Create one in your job-data API dashboard, under API tokens."),
    Field("GEMINI_API_KEYS", "Gemini API keys (job scorer)", "str", "",
          "Credentials", "env", secret=True, optional=True, restart=True,
          help="ONE shared set of keys for both the JOB SCORER and the RESUME TAILOR: the "
               "tailor uses them whenever 'Resume tailor engine' is 'pool'. Comma-separated, no "
               "spaces; they are rotated, and one key's daily free quota is counted once across "
               "both. Get keys at aistudio.google.com; blank = use your Google Cloud project."),
    # Gated TWO deep: it is only readable when the Gemini side bills by key
    # (gemini_auth == api_key), and gemini_auth is itself only live when the
    # tailor runs on Gemini. The transitive rule in is_visible() is what stops a
    # stored "api_key" from leaving this box on screen under the Claude tailor.
    # It also renders in Credentials, a section SECTION_ORDER puts before Engine
    # where its gate lives: one reason gate signals are connected in a second
    # pass (settings_tab._build).
    Field("RESUME_TAILOR_GEMINI_API_KEY", "Gemini API key (resume tailor)", "str", "",
          "Credentials", "env", secret=True, optional=True, restart=True,
          show_if=("gemini_auth", ("api_key",)),
          help="Rarely needed. ONE key for the RESUME TAILOR alone, used only while 'Resume "
               "tailor engine' is 'api_key', for a tailor on a different Google account from the "
               "scorer. To share the scorer's keys instead, set that engine to 'pool' and leave "
               "this blank; 'vertex' bills your Google Cloud project."),

    # --- Connection & paths: non-secret identity / locations, also in .env -----
    Field("BRIGHT_DATA_DATASET_ID", "Job-data dataset ID", "str", "",
          "Connection & paths", "env", optional=True, restart=True,
          help="The job-postings dataset to query - an identifier, not a secret."),
    Field("GOOGLE_CLOUD_PROJECT", "Google Cloud project ID", "str", "",
          "Connection & paths", "env", optional=True, restart=True,
          help="Project with Vertex AI enabled (for Gemini scoring + tailoring). Leave blank "
               "if you use 'Gemini API keys (job scorer)' instead."),
    Field("GOOGLE_CLOUD_LOCATION", "Google Cloud location", "choice", "global",
          "Connection & paths", "env", advanced=True, restart=True,
          help="Vertex AI region. 'global' works for most users. Left blank, the résumé "
               "tailor falls back to 'global' but the job scorer falls back to "
               "'us-central1'; set this explicitly to keep the two in sync.",
          choices=("global", "us-central1", "us-east1", "us-west1", "europe-west1"),
          blank_label="(blank: tailor uses global, scorer us-central1)"),
    Field("RESUME_TAILOR_CANDIDATE", "Your name (resume filenames)", "str", "Your_Name",
          "Connection & paths", "env", restart=True,
          help="Used in generated resume filenames; write it with underscores, no spaces."),
    Field("RESUME_TAILOR_OUTPUT", "Resume output folder", "path", "",
          "Connection & paths", "env", path_kind="dir", optional=True, restart=True,
          help="Where tailored resumes are saved. Blank = your Downloads/Generated_Resumes."),
    # NOT advanced, deliberately: this is the fix for "no PDF came out", i.e. what
    # someone hunts for when the tool is ALREADY broken. Hiding a repair knob
    # behind a disclosure toggle is backwards — a user in that state has no reason
    # to suspect the setting exists.
    Field("PDFLATEX_PATH", "pdflatex path", "path", "pdflatex",
          "Connection & paths", "env", path_kind="file", optional=True, restart=True,
          help="Path to pdflatex (MiKTeX/TeX Live). Leave as 'pdflatex' if it's on your PATH."),
    Field("LINKEDIN_CHROME_ACCOUNT", "Chrome profile (Google email)", "str", "",
          "Connection & paths", "env", optional=True, restart=True,
          help="Open job links in the Chrome profile signed in to this Google account. "
               "Blank = your default browser."),

    # --- Engine (shown as "Résumé tailor"): which AI service tailors résumés
    # (gemini/claude provider switch, local/config.json, leading the section since
    # cycle 19's ST-3), which Google billing method the Gemini side uses, and the
    # per-stage Gemini + Claude model pickers (.env). -------------------------
    Field("tailor_provider", "Resume tailor provider", "choice", "gemini",
          "Engine", "config",
          help="The AI service that writes every résumé bullet and the cover letter. "
               "Jev (when on) picks, checks and gates around it. 'gemini' uses Google, "
               "billed the way 'Resume tailor engine' says (that setting is on screen "
               "only while this is 'gemini'). 'claude' runs your locally installed "
               "Claude Code CLI on your claude.ai subscription (run `claude` once to log "
               "in). Takes effect on the next tailor run.",
          choices=("gemini", "claude")),
    Field("gemini_auth", "Resume tailor engine", "choice", "vertex",
          "Engine", "config", show_if=("tailor_provider", ("gemini",)),
          help="How the Gemini side bills. 'pool' shares the job scorer's 'Gemini API keys': "
               "all of them, rotated, held to the free-tier limits set under Scoring, and "
               "billing your cloud project only once they run dry. 'vertex' bills every call "
               "to it. 'api_key' uses the single 'Gemini API key (resume tailor)'.",
          choices=("vertex", "api_key", "pool")),

    # The fallback chains, gated on the MODE rather than on gemini_auth. A chain
    # belonging to the wrong mode does damage: the three
    # tiers are quality classes whose free quota runs the opposite way (a lite
    # allows 500 requests/day per key, a full Flash 20), so one shared chain
    # would send the cheap high-volume selection pass into the scarce allowance
    # the cover letter needs. The mode gate is what keeps the matching set on
    # screen; that they only DO anything under 'pool' billing is said in help.
    Field("tailor_fallback_models", "Tailor fallback models", "list", [],
          "Engine", "config", advanced=True,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("simple",)),
          help="Extra models the tailor may use once its own model runs out of free "
               "requests, one per line, best first. Each model has a separate free "
               "daily quota per API key, so this is what keeps tailoring off your "
               "billed cloud project. Used only while 'Resume tailor engine' is 'pool'."),
    Field("tailor_fallback_flash_lite", "Fallbacks: fast (briefs)", "list", [],
          "Engine", "config", advanced=True,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)),
          help="Extra models for the FAST tier only (the per-entry briefs and verb "
               "swaps), one per line, best first. List other '-lite' "
               "models here: they allow ~500 free requests/day per key. Listing a full "
               "Flash model instead spends the 20/day budget the standard and deep tiers "
               "need. Used only in 'pool'."),
    Field("tailor_fallback_flash", "Fallbacks: standard (selection)", "list", [],
          "Engine", "config", advanced=True,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)),
          help="Extra models for the STANDARD tier only, one per line, best first. This "
               "tier makes the most calls: selecting your atoms, then every bullet "
               "cleanup pass. Keep these in the same quality class as the standard model: "
               "the full Flash models are interchangeable here, and each carries its own "
               "20 free requests/day per key. Used only while the engine is 'pool'."),
    Field("tailor_fallback_pro", "Fallbacks: deep (writing)", "list", [],
          "Engine", "config", advanced=True,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)),
          help="Extra models for the DEEP tier only, one per line, best first. This "
               "tier writes the first draft of every bullet and the cover letter, so list "
               "only models you would accept that from; a '-lite' model here quietly "
               "lowers their quality. Empty is a fine answer. Used only while the engine "
               "is 'pool'."),

    # --- Resume tailor models: which Gemini model each tailoring stage uses, ----
    # written to .env (read by local/resume_tailor/config.py as RESUME_TAILOR_MODEL_*).
    # Editable dropdowns: pick a 3.x model or type a custom id.
    #
    # The MODE field is the gate for the rest, per provider, and the gate CHAIN is
    # what makes that work: a Field has exactly one `show_if`, so the tier rows
    # cannot carry both "provider is gemini" and "mode is tiers". They gate on the
    # mode field, which gates on the provider — `is_visible` walks the chain, so a
    # tier row is hidden by EITHER the wrong provider or `simple` mode.
    #
    # Two mode fields rather than one shared: the user only ever sees the one their
    # provider uses, and the two sides can differ (one model everywhere on Claude,
    # the tuned tier split on Gemini) without a third "which provider does this
    # apply to?" question. `config.py` reads them as two independent env vars.
    Field("RESUME_TAILOR_MODEL_MODE", "Tailor models: simple or per stage",
          "choice", "tiers", "Engine", "env", choices=MODEL_MODES,
          show_if=("tailor_provider", ("gemini",)), restart=True,
          help="'simple' uses ONE model for every step of tailoring, the one named in "
               "'Tailor model: one for every step'. 'tiers' uses a different model per "
               "stage (the three per-stage pickers, under 'Show advanced settings'): a cheap "
               "one for the small calls, a standard one to select and clean up, a deep "
               "one to write. 'tiers' is the default."),
    Field("RESUME_TAILOR_MODEL_ALL", "Tailor model: one for every step",
          "editable_choice", "gemini-3.5-flash", "Engine", "env", choices=GEMINI_MODELS,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("simple",)), restart=True,
          help="The single model every tailoring stage uses while 'Tailor models: simple or "
               "per stage' is 'simple'. Pick a listed id or type your own. Left blank, "
               "tailoring quietly falls back to the three per-stage models."),
    Field("RESUME_TAILOR_MODEL_FLASH_LITE", "Tailor model: fast (briefs)",
          "editable_choice", "gemini-3.5-flash-lite", "Engine", "env", choices=GEMINI_MODELS,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Cheapest model, for the small calls: the per-entry briefs and verb "
               "swaps."),
    Field("RESUME_TAILOR_MODEL_FLASH", "Tailor model: standard (selection)",
          "editable_choice", "gemini-3.5-flash", "Engine", "env", choices=GEMINI_MODELS,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Default model, and the busiest: selects and groups your atoms, then runs "
               "every bullet cleanup pass (re-ask, underfull fill, style gate, AI-writing "
               "sweep), the skills lines, the prep sheet and the chat."),
    Field("RESUME_TAILOR_MODEL_PRO", "Tailor model: deep (writing)",
          "editable_choice", "gemini-3.5-flash", "Engine", "env", choices=GEMINI_MODELS,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Writes the first draft of every bullet, and the cover letter unless "
               "'Tailor model: cover letter' names a model. Deliberately "
               "defaults to the same model as 'Tailor model: standard (selection)' to "
               "keep costs down; set it to gemini-3.1-pro-preview yourself "
               "for the strongest writing (slower / pricier)."),
    # Blank is a real value here: config._cover_or reads it as "no model of its
    # own", so the letter keeps the deep model for its draft and the standard one
    # for its edits, exactly as before the row existed.
    Field("RESUME_TAILOR_MODEL_COVER", "Tailor model: cover letter",
          "editable_choice", "", "Engine", "env", choices=GEMINI_MODELS,
          show_if=("RESUME_TAILOR_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Writes the cover letter: its draft and every pass that edits it. Leave "
               "blank to keep the usual split (the deep model drafts, the standard one "
               "edits)."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_MODE", "Claude models: simple or per stage",
          "choice", "tiers", "Engine", "env", choices=MODEL_MODES,
          show_if=("tailor_provider", ("claude",)), restart=True,
          help="Claude provider only. 'simple' uses ONE model for every step of tailoring, "
               "the one named in 'Claude model: one for every step'. 'tiers' uses a different "
               "model per stage (the three per-stage pickers, under 'Show advanced settings'): "
               "haiku for the small calls, sonnet to select and clean up, opus to write."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_ALL", "Claude model: one for every step",
          "editable_choice", "claude-sonnet-5", "Engine", "env", choices=CLAUDE_MODELS,
          show_if=("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("simple",)), restart=True,
          help="The single model every tailoring stage uses while 'Claude models: simple or "
               "per stage' is 'simple'. Pick a listed id or type your own. Left blank, "
               "tailoring quietly falls back to the three per-stage models."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "Claude model: fast (briefs)",
          "editable_choice", "claude-haiku-4-5", "Engine", "env", choices=CLAUDE_MODELS,
          show_if=("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Claude provider only: the small calls (per-entry briefs, verb "
               "swaps)."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_FLASH", "Claude model: standard (selection)",
          "editable_choice", "claude-sonnet-5", "Engine", "env", choices=CLAUDE_MODELS,
          show_if=("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Claude provider only, the busiest tier: selecting your atoms, then every "
               "bullet cleanup pass, the skills lines, the prep sheet and the chat."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_PRO", "Claude model: deep (writing)",
          "editable_choice", "claude-opus-5-5", "Engine", "env", choices=CLAUDE_MODELS,
          show_if=("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Claude provider only: writes the first draft of every bullet, and the "
               "cover letter unless 'Claude model: cover letter' names a model."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_COVER", "Claude model: cover letter",
          "editable_choice", "", "Engine", "env", choices=CLAUDE_MODELS,
          show_if=("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)), advanced=True, restart=True,
          help="Claude provider only: writes the cover letter, its draft and every pass "
               "that edits it. Leave blank to keep the usual split (the deep model "
               "drafts, the standard one edits)."),
    # resume_tailor/config.py's CLAUDE_EFFORTS plus 'default' (send no --effort);
    # claude_effort() reads this, and a value outside the set falls back to 'low'.
    Field("RESUME_TAILOR_CLAUDE_EFFORT", "Claude thinking effort",
          "choice", "low", "Engine", "env",
          choices=("low", "medium", "high", "xhigh", "max", "default"),
          show_if=("tailor_provider", ("claude",)), advanced=True, restart=True,
          help="Claude provider only: how long Claude thinks before each tailoring answer. "
               "'low' (the default) writes a full résumé's bullets in under half a minute. "
               "Higher levels think longer, and each answer's time limit grows with the "
               "level so they have room to finish; a whole tailoring run takes longer at "
               "each step up. 'default' sends no level and lets the Claude CLI decide."),
    # config.claude_effort(TIER_COVER*) reads this; 'same' (or anything outside
    # the set) falls through to RESUME_TAILOR_CLAUDE_EFFORT above.
    Field("RESUME_TAILOR_CLAUDE_EFFORT_COVER", "Claude thinking effort: cover letter",
          "choice", "same", "Engine", "env",
          choices=("same", "low", "medium", "high", "xhigh", "max", "default"),
          show_if=("tailor_provider", ("claude",)), advanced=True, restart=True,
          help="Claude provider only: how long Claude thinks while writing the cover "
               "letter. 'same' uses 'Claude thinking effort'. A higher level writes a "
               "more considered letter and takes longer; its time limit grows with the "
               "level. It applies in 'simple' mode too."),

    # --- VM (cloud scraper): NON-secret gcloud connection identifiers, in .env --
    # The VM tab pushes config/schedule/pause via `gcloud compute`. Auth is your
    # existing `gcloud auth login` — no SSH password or key is ever stored.
    # vm_enabled is the section master switch (local, non-secret bool in config.json):
    # off (the default) hides the whole VM area in the GUI and silences push prompts.
    Field("vm_enabled", "Enable VM features", "bool", False, "VM (cloud scraper)", "config",
          help="Turn on to manage a cloud job-discovery VM from here (schedule, pause, push config). "
               "Off hides all VM settings and never prompts to push; leave off if you don't use a VM."),
    Field("VM_INSTANCE", "VM instance name", "str", "", "VM (cloud scraper)", "env",
          optional=True,
          help="GCP instance that runs job discovery (e.g. scraper-vm). Blank = VM actions disabled."),
    Field("VM_ZONE", "VM zone", "str", "", "VM (cloud scraper)", "env", optional=True,
          help="Compute zone, e.g. us-east1-c."),
    Field("VM_PROJECT", "GCP project", "str", "", "VM (cloud scraper)", "env", optional=True,
          help="GCP project id the instance lives in (optional if gcloud has a default)."),
    Field("VM_USER", "VM Linux user", "str", "", "VM (cloud scraper)", "env", optional=True,
          help="Linux account on the VM that owns the discovery run (run_scraper.sh, crontab, data)."),
    Field("VM_REMOTE_DIR", "VM home dir", "str", "~", "VM (cloud scraper)", "env",
          advanced=True,
          help="Remote dir the discovery files live in. Usually ~ (the Linux user's home)."),
    Field("VM_GCLOUD_PATH", "gcloud path", "path", "gcloud", "VM (cloud scraper)", "env",
          path_kind="file", optional=True, advanced=True,
          help="Path to the gcloud CLI. Leave as 'gcloud' if it's on your PATH."),
    # --- Local watcher task: keep the LinkedInJobsWatcher scheduled task in step --
    # with the VM schedule (local, non-secret; the VM tab's buttons use these too).
    Field("local_task_autosync", "Auto-sync local watcher task", "bool", False,
          "VM (cloud scraper)", "config",
          help="When on, applying a schedule to the VM also re-registers the local "
               "LinkedInJobsWatcher task so it checks for fresh results after each run. "
               "Syncs off the VM's wall-clock run times, so it assumes the VM shares "
               "your timezone. Off = the local task's triggers never move."),
    # The one free-text field a consumer has to PARSE, so the one that carries a
    # `pattern`. local_task.parse_offsets is deliberately junk-safe (it skips
    # unreadable entries and falls back to its own default, so a mangled value can
    # never leave the watcher task trigger-less) — which is right for the consumer
    # and wrong for the editor: without this rule the Settings tab happily saves
    # "every half hour" and nothing ever says the pipeline threw it away.
    #
    # The rule is drawn exactly at what the consumer DISCARDS: every non-empty
    # comma-separated entry must be a non-negative whole number. Blank, a stray or
    # trailing comma, and surrounding spaces all pass, because parse_offsets
    # honours each of them in full — blank means "use the built-in 30,50,70", and
    # `30,,50` really is (30, 50). Rejecting those (the first cut did) would have
    # locked anyone whose config already held one out of saving ANY setting, from
    # a row the VM master switch can keep off screen.
    # test_the_offsets_pattern_is_never_stricter_than_its_consumer is the guard.
    Field("local_task_offsets", "Watcher check offsets (minutes)", "str", "30,50,70",
          "VM (cloud scraper)", "config", advanced=True,
          pattern=r"\s*(?:\d+\s*)?(?:,\s*(?:\d+\s*)?)*",
          pattern_help="Use comma-separated whole numbers, e.g. 30,50,70.",
          help="Minutes after each VM run time the local watcher checks for fresh "
               "results, comma-separated (e.g. 30,50,70 = three checks per run)."),
]


# Friendly filename shown next to each field in the config GUI so a user can find
# and inspect the file a value is saved to themselves (keyed by Field.target).
STORAGE_LABELS: dict[str, str] = {
    "config": "config.json",
    "search": "search_config.json",
    "scoring": "scoring_config.json",
    "env": ".env",
}


def storage_location(field: Field) -> str:
    """The friendly filename a Field's value is saved to (for the GUI 'stored in'
    tag). Falls back to the raw target id for any unmapped target."""
    return STORAGE_LABELS.get(field.target, field.target)


def is_visible(field: Field, values: Mapping[str, Any]) -> bool:
    """Should `field` be rendered, given the current `values`?

    A field is visible iff its OWN `show_if` predicate holds AND its gate field
    is itself visible. The transitive half is not decoration: `gemini_auth` is
    gated on the tailor running on Gemini, so with the tailor on Claude a STORED
    `gemini_auth == "api_key"` would otherwise leave the Gemini API-key box on
    screen with nothing on the form governing it.

    `values` may be PARTIAL — the Qt form only reads the gate widgets — so an
    absent gate key falls back to that Field's own default rather than reading as
    "hidden".

    Values are compared as strings, EXACTLY: no strip/lower, unlike the runtime
    resolvers (`resume_tailor.config`, `score_jobs._active_scoring`) which
    normalise. Through the form the two agree on every choice the resolvers
    know: `settings_tab._set_combo` reads a hand-edited `"provider": "Claude"` as
    the "claude" choice before `_gate_values` hands it here, and a blank one as
    the field's default. A value matching no choice in any case stays on screen
    as typed, so it hides every field gated on it here and in the form alike,
    and Save flags it.

    Raises on a broken graph rather than degrading quietly, matching the posture
    of `settings_tab._set_field_visible` (KeyError) and `_connect_gate_signals`
    (TypeError): KeyError for a gate key that is not a schema field — silently
    hiding a field forever is the exact failure this phase exists to prevent —
    and ValueError for a cycle, which would otherwise be an infinite loop in
    front of a user. The cycle raise is value-DEPENDENT: a failing predicate
    returns False before the walk reaches the repeat, so a cyclic graph only
    raises when the values satisfy every predicate around the loop. Either way
    it terminates; the value-independent guard is the schema test. The shipped
    graph is pinned by
    test_every_gate_names_a_real_field_and_real_choices and
    test_show_if_graph_is_acyclic.
    """
    by_key = {f.key: f for f in SETTINGS_SCHEMA}
    seen: set[str] = {field.key}
    current = field
    while current.show_if is not None:
        gate_key, allowed = current.show_if
        gate = by_key.get(gate_key)
        if gate is None:
            raise KeyError(f"{current.key} gates on unknown field {gate_key!r}")
        if str(values.get(gate_key, gate.default)) not in allowed:
            return False
        if gate_key in seen:
            raise ValueError(f"show_if cycle at {gate_key!r}")
        seen.add(gate_key)
        current = gate
    return True


def visible_keys(values: Mapping[str, Any]) -> list[str]:
    """The keys of every Field that `values` puts on screen, in schema order."""
    return [f.key for f in SETTINGS_SCHEMA if is_visible(f, values)]


def _resolve_targets(targets: dict[str, Path] | None) -> dict[str, Path]:
    return TARGET_FILES if targets is None else targets


def target_path(target_id: str) -> Path:
    """The backing file `load()` reads for `target_id` (a TARGET_FILES key), for
    a reader outside this module that needs the same file: `jev_switch` reads
    the Jev switches from the "config" file on every call."""
    return Path(_resolve_targets(None)[target_id])


def _read_file(path: Path) -> dict[str, Any]:
    """Parse a backing JSON file, or {} when missing/unreadable.

    utf-8-sig for the same reason as jsonutil.read_json_dict: a BOM makes
    json.loads raise, this catches it, and the whole file reads as empty with
    nothing said. Notepad and PowerShell 5.1 both write BOMs."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_target(target_id: str, path: Path | None) -> dict[str, Any]:
    """Read a backing store as a {key: value} dict, picking the right parser for
    its target (env files vs JSON). {} when the path is unset/missing."""
    if path is None:
        return {}
    if target_id in ENV_TARGETS:
        return envfile.read(path)
    return _read_file(path)


# The legacy archive_prune_mode value whose count lived in a separate key. Kept
# here (not imported from settings_archive, which imports THIS module) purely so
# the migration below can recognise a config written before archive_mode existed.
_LEGACY_PRUNE_COUNT = "Keep newest N"


def _legacy_archive_mode(stored: Mapping[str, Any]) -> str:
    """Derive `archive_mode` from the four archive_* keys it replaced.

    Runs only for a config saved before that key existed (see
    DERIVED_WHEN_ABSENT). Deliberately PURE — raw backing-store mapping in,
    string out, no I/O — so every legacy combination is unit-testable without a
    filesystem.

    The governing invariant is NEVER PRUNE MORE AGGRESSIVELY THAN THE OLD POLICY.
    That is why a count that is not one of the two offered rounds UP (a saved
    `keep: 10` reads "Keep newest 20"), why a count above every option and the
    dropped age-based mode both read "Keep everything", and why anything
    unreadable does too. Rounding down would delete snapshots the user still has.
    """
    if "archive_enabled" in stored and not stored["archive_enabled"]:
        return ARCHIVE_OFF          # the master switch beats any prune policy
    if stored.get("archive_prune_mode") != _LEGACY_PRUNE_COUNT:
        # absent, "Keep everything", the dropped days mode, or garbage
        return ARCHIVE_KEEP_ALL
    try:
        keep = int(stored.get("archive_prune_keep", 20))
    except (TypeError, ValueError):
        return ARCHIVE_KEEP_ALL     # an unreadable count prunes nothing
    if keep <= 20:
        return ARCHIVE_KEEP_20
    if keep <= 100:
        return ARCHIVE_KEEP_100
    return ARCHIVE_KEEP_ALL


# Keys whose value is DERIVED from older keys in the SAME backing file when the
# key itself is absent — the migration hook `load()` consults BETWEEN the stored
# value and the schema default. So a stored value always wins, a derivation only
# ever runs for a config written before its key existed, and a fresh install
# (empty store) still lands on the Field default. Every function here must be
# pure: mapping in, value out, no I/O.
DERIVED_WHEN_ABSENT: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "archive_mode": _legacy_archive_mode,
}


def load(targets: dict[str, Path] | None = None) -> dict[str, Any]:
    """Return {key: stored-value-or-derived-or-default} for every schema Field.

    Reads each backing file once and looks each Field up in its own target,
    so the result is the effective configuration the UI should display. A key
    absent from its backing file falls back to DERIVED_WHEN_ABSENT (a migration
    off older keys in the same file) and then to the Field's default.
    """
    targets = _resolve_targets(targets)
    cache: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    for f in SETTINGS_SCHEMA:
        if f.target not in cache:
            cache[f.target] = _read_target(f.target, targets.get(f.target))
        store = cache[f.target]
        if f.key in store:
            values[f.key] = store[f.key]
            continue
        derive = DERIVED_WHEN_ABSENT.get(f.key)
        values[f.key] = f.default if derive is None else derive(store)
    return values


def secret_status(targets: dict[str, Path] | None = None) -> dict[str, bool]:
    """{key: is-it-set} for every secret Field, WITHOUT returning the value.

    The config GUI uses this to show "configured / not set" next to each secret
    box. Note the boxes themselves DO hold the stored values (loaded masked by
    default, with a per-field Hide toggle, and revealed in plaintext when a
    settings snapshot is loaded for review) — this helper is just the cheap
    set/unset probe for status labels, not a no-secrets-in-widgets guarantee.
    """
    targets = _resolve_targets(targets)
    cache: dict[str, dict[str, Any]] = {}
    out: dict[str, bool] = {}
    for f in SETTINGS_SCHEMA:
        if not f.secret:
            continue
        if f.target not in cache:
            cache[f.target] = _read_target(f.target, targets.get(f.target))
        out[f.key] = bool(str(cache[f.target].get(f.key, "")).strip())
    return out


# The four default-on Jev switches (cycle 19) and the one rule both of their
# readers use: the Settings checkbox (`settings_tab`) and `jev_switch`, which
# decides whether a run spends TypeSafe credits (SP1 follow-up 3). A missing
# key is the Field's default (on) and a real bool is itself. Any other value
# is on only as one of SWITCH_ON_WORDS, any case, spaces stripped, the words
# `score_jobs._as_bool` reads for the scoring config's bools. A hand-edited
# null, 0, "", "false" or "off", and any value outside the words, reads off
# in both places, so a stray value spends nothing.
JEV_SWITCHES = ("jev_enabled", "jev_scoring", "jev_tailor", "jev_difficulty")
SWITCH_ON_WORDS = frozenset(("true", "yes", "on", "1"))


def switch_on(value: Any) -> bool:
    """Is a stored Jev switch `value` on? A bool is itself; any other value is
    on only as one of `SWITCH_ON_WORDS` once stripped and lower-cased. A
    missing key never reaches here: `load()` fills the default (True), and
    `jev_switch` reads a missing key as True."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in SWITCH_ON_WORDS


def _coerce_ok(f: Field, value: Any) -> bool:
    """True when `value` is the right Python type for Field `f`."""
    if f.type == "int":
        # bool is a subclass of int; reject it for int fields.
        return isinstance(value, int) and not isinstance(value, bool)
    if f.type == "bool":
        return isinstance(value, bool)
    if f.type in ("str", "path"):
        return isinstance(value, str)
    if f.type == "choice":
        # Membership is `field_problem`'s job, with its own "Not allowed: ..."
        # message; this only guards the PYTHON TYPE, exactly like every other
        # branch here, so a wrong-type value (an int, a list) reads as its own
        # "Expected choice, got ...", kept separate from the membership message.
        return isinstance(value, str)
    if f.type == "editable_choice":
        # editable: any string is allowed (pick from choices OR type a custom id).
        return isinstance(value, str)
    if f.type in ("list", "multichoice"):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return True


def field_problem(f: Field, value: Any) -> str | None:
    """The problem with `value` for Field `f`, or None when it is fine.

    Every per-field rule `validate()` enforces, in one place: the Python type,
    `choice` membership, `multichoice` membership, `int` min/max, the .env
    line-break rule, and `pattern`. `validate()` is a thin loop over this, so a
    caller with ONE field and ONE candidate value (the Settings tab, re-checking
    as the user edits) gets the exact same message Save would: one shared source
    for the rules, so the two can never drift apart.
    """
    if not _coerce_ok(f, value):
        return f"Expected {f.type}, got {type(value).__name__}."
    if f.type == "choice" and value not in f.choices and not (f.blank_label and value == ""):
        # The Settings tab keeps an unknown stored choice on screen as typed, and a
        # blank one would read "Not allowed: ." under its dropdown. A field with a
        # `blank_label` lists the blank itself, so only there is "" a choice.
        return f"Not allowed: {value}." if value.strip() else "Pick one of the listed options."
    # An .env value is one physical KEY=VALUE line and envfile.read parses line by
    # line, so a newline does not round-trip: it writes a second line the reader
    # takes for a whole new assignment. envfile.update refuses it too, but that
    # raise happens mid-save with some targets already written, producing one
    # message with no way to mark which field caused it. Catch it here so the
    # form says which box is wrong before anything is written.
    if f.target in ENV_TARGETS and isinstance(value, str) and _CONTROL_RE.search(value):
        return ("No line breaks: this is stored as a single line in "
                ".env. (Several keys go on one line, comma-separated.)")
    if f.type == "int":
        if f.min is not None and value < f.min:
            return f"Must be >= {f.min}."
        if f.max is not None and value > f.max:
            return f"Must be <= {f.max}."
    elif f.type == "multichoice":
        bad = [v for v in value if v not in f.choices]
        if bad:
            return f"Not allowed: {', '.join(bad)}."
    elif f.type in TEXT_TYPES and f.pattern is not None:
        # `fullmatch` checks the WHOLE string, so a rule satisfied by only a
        # PREFIX cannot pass "30,50,70 and some junk" through and write it
        # straight to config.json.
        if re.fullmatch(f.pattern, value) is None:
            return f.pattern_help or "Not in the expected format."
    return None


def validate(values: dict[str, Any]) -> dict[str, str]:
    """Return {key: error_message} for invalid values; empty dict means valid.

    Only keys present in `values` AND in the schema are checked. A loop over
    `field_problem`, so Save and the Settings tab's per-field re-check can never
    disagree about what counts as a problem.
    """
    errors: dict[str, str] = {}
    by_key = {f.key: f for f in SETTINGS_SCHEMA}
    for key, value in values.items():
        f = by_key.get(key)
        if f is None:
            continue
        problem = field_problem(f, value)
        if problem is not None:
            errors[key] = problem
    return errors


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write `data` as JSON to `path`, backing up any existing file to .bak.

    Copy existing -> path.bak, write to a same-dir PID-tagged temp file, then
    replace onto the real path (atomic on the same filesystem).

    The replace goes through jsonutil.replace_with_retry like every other
    atomic writer in the tree: CPython's open() on Windows does not grant
    FILE_SHARE_DELETE, so MoveFileEx fails with PermissionError for the
    microseconds a lock-free reader holds the destination — and config.json is
    read lock-free by jobsdata on the UI thread and by the watcher on a timer.
    The try/finally is what keeps a failed write from stranding a
    config.json.<pid>.tmp in a directory the user is told to look at.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save(values: dict[str, Any], targets: dict[str, Path] | None = None) -> None:
    """Validate then persist `values`, grouped by Field.target.

    Raises ValueError(errors) if validation fails. For each backing file, merge
    the schema-owned values into the file's existing contents so unrelated keys
    survive, then write atomically with a .bak backup.
    """
    errors = validate(values)
    if errors:
        raise ValueError(errors)

    targets = _resolve_targets(targets)
    by_key = {f.key: f for f in SETTINGS_SCHEMA}

    # key -> values to write, grouped by target id.
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        f = by_key.get(key)
        if f is None:
            continue
        grouped.setdefault(f.target, {})[key] = value

    for target_id, updates in grouped.items():
        path = targets.get(target_id)
        if path is None:
            continue
        if target_id in ENV_TARGETS:
            # envfile.update merges in place (keeps comments + unknown keys) and
            # backs up to .bak itself, so no read-merge-write dance here.
            envfile.update(Path(path), {k: str(v) for k, v in updates.items()})
        else:
            # Locked read-merge-write. config.json is written from the UI thread
            # here, from the dashboard's background delete queue via
            # jobsdata._save_cfg, and from the watcher process; without the lock
            # whichever writer read first has its keys silently reverted.
            with file_lock(Path(path)):
                merged = _read_file(path)
                merged.update(updates)
                _atomic_write(Path(path), merged)
