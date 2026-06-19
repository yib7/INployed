import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import score_jobs as sj  # noqa: E402


def _resp(text):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
    )


class FakePool:
    def __init__(self, stage1_by_substr):
        self.stage1 = stage1_by_substr
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents))
        if model == sj.STAGE1_MODEL:
            score = 1
            for sub, sc in self.stage1.items():
                if sub in contents:
                    score = sc
                    break
            return _resp(json.dumps({"score": score, "reason": "r"}))
        return _resp(json.dumps(
            {"deep_score": 8, "strengths": ["s"], "gaps": ["g"], "recommendation": "apply"}))

    def stats(self):
        return {"free_calls": len(self.calls), "vertex_calls": 0}


def test_score_stage1_success():
    pool = FakePool({"JD-TEXT": 5})
    out = asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "resume", "J1", "JD-TEXT here"))
    assert out == {"job_posting_id": "J1", "score": 5, "reason": "r"}


def test_score_stage1_error_returns_error_dict():
    class Boom:
        async def generate(self, **k):
            raise RuntimeError("kaboom")
    out = asyncio.run(sj.score_stage1(Boom(), asyncio.Semaphore(1), "resume", "J1", "x"))
    assert out["score"] is None
    assert out["reason"].startswith("ERROR:")


def test_stage2_dispatched_highest_score_first(monkeypatch):
    monkeypatch.setattr(sj, "STAGE2_CONCURRENCY", 1)
    df = pd.DataFrame({
        "job_posting_id": ["j1", "j2", "j3"],
        "job_description_md": ["AAA", "BBB", "CCC"],
        "filtered_out": [False, False, False],
    })
    pool = FakePool({"AAA": 5, "BBB": 4, "CCC": 5})
    asyncio.run(sj.run_scoring(pool, "resume", df))
    order = []
    for model, contents in pool.calls:
        if model == sj.STAGE2_MODEL:
            for sub in ("AAA", "BBB", "CCC"):
                if sub in contents:
                    order.append(sub)
    assert order == ["AAA", "CCC", "BBB"]


def test_make_pool_delegates(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(sj.KeyPool, "from_env",
                        classmethod(lambda cls, *, state_path: sentinel))
    assert sj.make_pool() is sentinel
