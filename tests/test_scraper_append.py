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
