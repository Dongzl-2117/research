from datetime import UTC, datetime

from researchlib.textutil import SHANGHAI, content_hash


def test_content_hash_stable_across_utc_roundtrip():
    title = "The Chip Fly in the AI Ointment"
    body = "Hello\n\nWorld\n"
    shanghai = datetime(2026, 1, 30, 0, 0, 0, tzinfo=SHANGHAI)
    utc = shanghai.astimezone(UTC)
    assert utc.isoformat() != shanghai.isoformat()
    assert content_hash(title, body, shanghai) == content_hash(title, body, utc)


def test_content_hash_uses_shanghai_stamp():
    title = "x"
    body = "y"
    when = datetime(2026, 1, 29, 16, 0, 0, tzinfo=UTC)
    got = content_hash(title, body, when)
    # Same as hashing the Shanghai wall time explicitly.
    from hashlib import sha256

    from researchlib.textutil import normalize_newlines

    stamp = when.astimezone(SHANGHAI).isoformat()
    expect = sha256(f"{title}\n{stamp}\n{normalize_newlines(body)}".encode()).hexdigest()
    assert got == expect
    assert stamp == "2026-01-30T00:00:00+08:00"
