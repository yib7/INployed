import asyncio
import json
import os
from pathlib import Path

import pandas as pd
import pytest
import scraper


def _full_load_reference(existing, new):  # the CURRENT semantics, for equivalence
    combined = pd.concat([existing, new], ignore_index=True)
    combined["job_posting_id"] = combined["job_posting_id"].astype(str)
    combined = combined.drop_duplicates(subset=["job_posting_id"], keep="first")
    return scraper.drop_blocklisted_companies(combined).reset_index(drop=True)


def test_chunked_matches_full_load(tmp_path, monkeypatch):
    existing = pd.DataFrame({"job_posting_id": ["1","2","3"], "job_title": ["a","b","c"],
                             "company_name": ["x","y","z"]})
    new = pd.DataFrame({"job_posting_id": ["3","4"], "job_title": ["C2","d"],
                        "company_name": ["z","w"]})
    m = tmp_path / "master.csv"
    existing.to_csv(m, index=False)
    monkeypatch.setattr(scraper, "MASTER_CSV", m)
    monkeypatch.setattr(scraper, "CHUNK", 2)  # force multi-chunk
    ret = scraper.append_to_master(new)
    got = pd.read_csv(m, dtype={"job_posting_id": str}).sort_values("job_posting_id").reset_index(drop=True)
    ref = _full_load_reference(existing.astype({"job_posting_id": str}), new).sort_values("job_posting_id").reset_index(drop=True)
    assert got["job_title"].tolist() == ref["job_title"].tolist()  # id 3 keeps existing "c"
    assert ret == len(pd.read_csv(m))  # returned count matches the actual resulting file


def test_new_only_column_unioned(tmp_path, monkeypatch):
    existing = pd.DataFrame({"job_posting_id": ["1"], "job_title": ["a"]})
    new = pd.DataFrame({"job_posting_id": ["2"], "job_title": ["b"], "brand_new": ["v"]})
    m = tmp_path / "master.csv"
    existing.to_csv(m, index=False)
    monkeypatch.setattr(scraper, "MASTER_CSV", m)
    scraper.append_to_master(new)
    df = pd.read_csv(m)
    assert "brand_new" in df.columns and df.set_index("job_posting_id").loc[2, "brand_new"] == "v"


def test_blocklist_refilters_whole_master(tmp_path, monkeypatch):
    existing = pd.DataFrame({"job_posting_id": ["1"], "company_name": ["BadCorp"]})
    new = pd.DataFrame({"job_posting_id": ["2"], "company_name": ["GoodCo"]})
    m = tmp_path / "master.csv"
    existing.to_csv(m, index=False)
    monkeypatch.setattr(scraper, "MASTER_CSV", m)
    monkeypatch.setattr(scraper, "load_blocklist", lambda: ["badcorp"])
    scraper.append_to_master(new)
    assert pd.read_csv(m, dtype=str)["job_posting_id"].tolist() == ["2"]


def test_unreadable_master_still_raises(tmp_path, monkeypatch):
    m = tmp_path / "master.csv"
    m.write_bytes(b"\x00\x01 not,csv\n\"unterminated")
    monkeypatch.setattr(scraper, "MASTER_CSV", m)
    # a genuinely unreadable master must not be silently treated as empty
    with pytest.raises(OSError):
        scraper.append_to_master(pd.DataFrame({"job_posting_id": ["9"]}))


# P2-1: the external-exclude JSON dump and the per-run CSV write are copied to
# the VM and must survive a crash/kill mid-write. Both must route through the
# module's atomic helpers (same-dir tempfile + os.replace), never a naked write
# that can leave a truncated file behind.

def test_write_external_exclude_ids_routes_through_atomic_json(tmp_path, monkeypatch):
    target = tmp_path / "external_exclude_ids.json"
    monkeypatch.setattr(scraper, "load_exclude_ids", lambda: ["1", "2", "3"])
    calls = []
    real = scraper._atomic_write_json

    def spy(path, data):
        calls.append((Path(path), list(data)))
        return real(path, data)

    monkeypatch.setattr(scraper, "_atomic_write_json", spy)
    out = scraper.write_external_exclude_ids(target)
    assert out == target
    assert calls == [(target, ["1", "2", "3"])]                 # atomic helper used
    assert json.loads(target.read_text(encoding="utf-8")) == ["1", "2", "3"]


def test_write_external_exclude_ids_leaves_old_file_on_crash(tmp_path, monkeypatch):
    target = tmp_path / "external_exclude_ids.json"
    target.write_text('["old"]', encoding="utf-8")
    before = target.read_bytes()
    monkeypatch.setattr(scraper, "load_exclude_ids", lambda: ["new1", "new2"])

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")

    monkeypatch.setattr(os, "replace", boom_replace)
    with pytest.raises(OSError):
        scraper.write_external_exclude_ids(target)
    assert target.read_bytes() == before                        # old file intact
    leftovers = [p for p in tmp_path.iterdir() if p.name != "external_exclude_ids.json"]
    assert leftovers == []                                      # tmp cleaned up


def test_run_dir_csv_written_atomically(tmp_path, monkeypatch):
    # main()'s run-dir CSV write must go through _atomic_to_csv (same-dir tmp +
    # os.replace), never a naked df.to_csv straight onto the final path (a crash
    # there would strand a truncated CSV that crashes the scoring step).
    label = scraper.RUN_LABELS[0]
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "require_credentials", lambda: None)
    monkeypatch.setattr(scraper, "load_search_config",
                        lambda: {"limit_per_input": 5, "keywords": ["k"], "remote_types": ["remote"]})
    monkeypatch.setattr(scraper, "load_blocklist", lambda: [])
    monkeypatch.setattr(scraper, "append_to_master", lambda df: len(df))

    async def fake_download(session, snapshot_id):
        return [{"job_posting_id": "1", "job_title": "A"},
                {"job_posting_id": "2", "job_title": "B"}]

    monkeypatch.setattr(scraper, "download", fake_download)

    spied = []
    real = scraper._atomic_to_csv

    def spy(df, path, **kwargs):
        spied.append(Path(path))
        return real(df, path, **kwargs)

    monkeypatch.setattr(scraper, "_atomic_to_csv", spy)

    # snapshot_id set -> recovery path (no trigger/billing, no network)
    asyncio.run(scraper.main(snapshot_id="snap-recover", run_label=label))

    run_dir = tmp_path / label
    written = list(run_dir.glob(f"linkedin_jobs_*_{label}.csv"))
    assert len(written) == 1
    assert written[0] in spied                                  # atomic helper used
    assert [p for p in run_dir.iterdir() if p.suffix == ".tmp"] == []
    got = pd.read_csv(written[0], dtype={"job_posting_id": str})
    assert sorted(got["job_posting_id"]) == ["1", "2"]
