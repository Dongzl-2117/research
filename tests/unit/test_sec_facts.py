"""用合成披露逐项核对时间边界、修订、原始精度和手记桥接。"""

import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError

import pytest

from researchlib import sec_facts as sec

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/sec/companyfacts.json"
NOW = datetime(2025, 6, 1, tzinfo=UTC)
ACCEPTED = "2024-04-30T19:05:06-04:00"
NOTE = f"note:{uuid.UUID(int=1)}"
PERIOD = "2024-01-01/2024-03-31"
ARGS = [
    str(FIXTURE),
    "--tag",
    "us-gaap:Revenue",
    "--unit",
    "USD",
    "--cik",
    "1234567",
    "--start",
    "2024-01-01",
    "--end",
    "2024-03-31",
    "--before",
    "2024-05-01",
]


@pytest.fixture
def doc():
    return sec.parse_document(FIXTURE.read_bytes(), "0001234567")


def lookup(doc, before="2024-05-01", **changes):
    query = dict(tag="us-gaap:Revenue", unit="USD", start="2024-01-01", end="2024-03-31", before=before)
    return sec.observe(doc, **{**query, **changes})


def note():
    value = dict(
        company="示例",
        companyId="",
        hypothesis="增长可持续",
        falsifier="收入下降",
        review="2025-07-02",
        ref="",
        excerpt="",
        metric="营收",
        unit="USD",
        period=PERIOD,
        expected="125",
        actual="",
        published="",
        filing="",
        change="",
    )
    return {
        "format": "research-journal",
        "version": 1,
        "records": [{"id": NOTE, "kind": "note", "revisions": [{"at": "2024-04-01T00:00:00.000Z", "value": value}]}],
    }


def patch(result, document=None, **kwargs):
    args = dict(
        note_id=NOTE,
        company="示例",
        metric="营收",
        accession=result["latest"]["accession"] if result["latest"] else "",
        published=ACCEPTED,
        now=NOW,
    )
    return sec.journal_patch(result, document if document is not None else note(), **{**args, **kwargs})


def test_cutoff_precision_revision_comparatives_and_url(doc):
    assert lookup(doc, "2024-04-30")["status"] == "missing"
    first = lookup(doc)
    assert len(first["history"]) == 1
    row = first["latest"]
    assert row == {
        "actual": "120.0100000000000001",
        "accession": "0000999999-24-000001",
        "form": "10-Q",
        "filed": "2024-04-30",
        "filing": "https://www.sec.gov/Archives/edgar/data/1234567/000099999924000001/0000999999-24-000001-index.htm",
    }
    assert lookup(doc, "2024-05-20")["latest"] == row
    second = lookup(doc, "2024-05-21")
    assert [r["actual"] for r in second["history"]] == [row["actual"], "110"]
    assert second["latest"]["form"] == "10-Q/A"
    assert lookup(doc, "2025-05-01")["latest"]["actual"] == "111"


def test_separate_tag_unit_and_complete_period(doc):
    assert lookup(doc, tag="ifrs-full:Revenue")["status"] == "missing"
    assert lookup(doc, unit="CNY")["history"] == []
    assert lookup(doc, "2024-05-02", unit="EUR")["latest"]["actual"] == "95"
    assert lookup(doc, start="2023-04-01")["latest"]["actual"] == "490"
    assert lookup(doc, start=None)["status"] == "missing"
    assert lookup(doc, tag="us-gaap:Assets", start=None)["latest"]["actual"] == "9007199254740993"


def test_ambiguity_and_corrupt_same_filing(doc):
    rows = doc["facts"]["us-gaap"]["Revenue"]["units"]["USD"]
    rows.append({**rows[0], "accn": "0000999999-24-000009", "val": 90})
    result = lookup(doc)
    assert len(result["history"]) == 2 and result["latest"] is None
    assert result["status"] == "ambiguous_same_day"
    with pytest.raises(ValueError, match="唯一"):
        patch(result)
    rows[1]["val"] = 89
    with pytest.raises(ValueError, match="矛盾"):
        lookup(doc)


@pytest.mark.parametrize("invalid", [False, "17", {}, None, Decimal("NaN")])
def test_reject_false_numeric_values(doc, invalid):
    doc["facts"]["us-gaap"]["Revenue"]["units"]["USD"][0]["val"] = invalid
    with pytest.raises(ValueError, match="数值"):
        lookup(doc)


def test_journal_preserves_original_and_idempotent(doc):
    document = note()
    result = lookup(doc)
    merged = patch(result, document)
    versions = merged["records"][0]["revisions"]
    assert len(versions) == 2 and document["records"][0]["revisions"] == versions[:1]
    value = versions[1]["value"]
    assert value["expected"] == "125" and value["actual"] == "120.0100000000000001"
    assert value["published"] == "2024-04-30T23:05:06.000Z" and value["filing"] == result["latest"]["filing"]
    assert versions[1]["at"] == sec.stamp(NOW) and "0000999999-24-000001" in value["change"]
    assert patch(result, merged) == merged
    with pytest.raises(ValueError, match="人工核对"):
        patch(result, merged, published="2024-04-30T19:15:06-04:00")
    newest = patch(lookup(doc, "2024-05-21"), merged, published="2024-05-21T01:15:00Z", now=NOW + timedelta(days=1))
    assert len(newest["records"][0]["revisions"]) == 3
    with pytest.raises(ValueError, match="不能覆盖"):
        patch(result, newest)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(company="其他"),
        dict(metric="利润"),
        dict(published="2024-04-30"),
        dict(published="2024-04-30T12:00:00"),
        dict(published="2024-05-01T06:00:00Z"),
        dict(now=datetime(2024, 4, 30, 18, tzinfo=UTC)),
    ],
)
def test_journal_rejects_wrong_mapping_or_availability(doc, kwargs):
    with pytest.raises(ValueError):
        patch(lookup(doc), **kwargs)


def test_cli_observe_and_journal_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sec, "build_opener", lambda *args: pytest.fail("离线命令触网"))
    sec.main(["observe", *ARGS])
    assert json.loads(capsys.readouterr().out)["latest"]["actual"] == "120.0100000000000001"
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps(note()))
    output = tmp_path / "patch.json"
    cmd = [
        "journal",
        *ARGS,
        "--backup",
        str(backup),
        "--note-id",
        NOTE,
        "--company",
        "示例",
        "--metric",
        "营收",
        "--accession",
        "0000999999-24-000001",
        "--published-at",
        ACCEPTED,
        "--output",
        str(output),
    ]
    sec.main(cmd)
    assert len(json.loads(output.read_bytes())["records"][0]["revisions"]) == 2
    with pytest.raises(SystemExit) as err:
        sec.main(cmd)
    assert err.value.code == 2
    assert len(json.loads(backup.read_bytes())["records"][0]["revisions"]) == 1


def test_download_rate_contact_cik_raw_bytes_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    called = []
    waits = []
    monkeypatch.setattr(sec.time, "sleep", waits.append)
    replies = [
        FIXTURE.read_bytes(),
        b'{"cik":9,"facts":{}}',
        HTTPError("https://data.sec.gov", 429, "slow", {"Retry-After": "780"}, None),
    ]

    class Opener:
        def open(self, request, timeout):
            called.append((request.full_url, request.headers["User-agent"], timeout))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(reply)

    monkeypatch.setattr(sec, "build_opener", lambda handler: Opener())
    output = tmp_path / "original.json"
    with pytest.raises(ValueError, match="User-Agent"):
        sec.fetch(1234567, "匿名", output)
    agent = "Example Analyst fixture@example.invalid"
    sec.fetch(1234567, agent, output)
    assert output.read_bytes() == FIXTURE.read_bytes()
    assert called == [("https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json", agent, 30)]
    with pytest.raises(ValueError, match="新"):
        sec.fetch(1234567, agent, output)
    second = tmp_path / "another.json"
    with pytest.raises(ValueError, match="CIK"):
        sec.fetch(1234567, agent, second)
    with pytest.raises(HTTPError):
        sec.fetch(1234567, agent, second)
    with pytest.raises(ValueError, match="冷却"):
        sec.fetch(1234567, agent, second)
    assert len(called) == 3 and not second.exists() and all(0 < wait <= 1 for wait in waits)


def test_journal_checks_accession_period_and_no_prospective_actual(doc):
    result = lookup(doc)
    with pytest.raises(ValueError, match="accession"):
        patch(result, accession="0000999999-24-000009")
    for key in ("unit", "period"):
        backup = note()
        backup["records"][0]["revisions"][0]["value"][key] = "其他"
        with pytest.raises(ValueError, match="不匹配"):
            patch(result, backup)
    with pytest.raises(ValueError, match="已实现"):
        patch({**result, "end": "2025-01-01"})


@pytest.mark.parametrize("wrong", ["ABC", 0, "12345678901", True])
def test_invalid_issuer(wrong):
    with pytest.raises(ValueError, match="CIK"):
        sec.parse_document(FIXTURE.read_bytes(), wrong)


def test_output_exclusive_conflict_keeps_prior_bytes(tmp_path):
    output = tmp_path / "facts.json"
    sec.save_new(output, b"original")
    with pytest.raises(FileExistsError):
        sec.save_new(output, b"replacement")
    assert list(tmp_path.iterdir()) == [output] and output.read_bytes() == b"original"
