"""定时抓取只为自己导入的新稿和从预览转为全文的旧编号付费。"""

from researchlib import fresh
from researchlib.ingest import markdown_dir


def test_receipt_selects_promoted_preview_and_omits_concurrent_history(conn, settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SOURCES_FILE", str(settings.sources_path))
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    source = ["--source", "stratechery"]
    daily, history = tmp_path / "daily", tmp_path / "history"
    daily.mkdir()
    history.mkdir()
    receipt = tmp_path / "receipt"

    def article(folder, slug, body):
        (folder / f"{slug}.md").write_text(f"# {slug}\n\n{body}\n", encoding="utf-8")

    def ingest(folder):
        return markdown_dir.main([*source, "--input", str(folder), "--origin", "crawl"])

    article(daily, "Preview", "Short preview.")
    assert ingest(daily) == 0
    preview = conn.execute("SELECT id, raw_meta FROM articles").fetchone()
    assert preview["raw_meta"]["hidden"] == "short_post"

    article(daily, "Preview", "Updated cash flow and earnings. " * 300)
    article(daily, "New", "New shipment costs. " * 600)
    with monkeypatch.context() as patch:
        patch.setenv("RESEARCH_CRAWL_RECEIPT", str(receipt))
        assert ingest(daily) == 0
    article(history, "Older", "A historical capital allocation report. " * 300)
    assert ingest(history) == 0
    ids = {row["title"]: row["id"] for row in conn.execute("SELECT id, title FROM articles")}
    assert ids["Preview"] == preview["id"]

    paid_jobs = []

    def record(_settings, eligible, *args):
        paid_jobs.append(set(eligible))
        return 0

    monkeypatch.setattr("researchlib.llm.ensure_oauth", lambda _: None)
    for step in ("step_translate", "step_views", "step_digest", "step_mechanism"):
        monkeypatch.setattr(fresh, step, record)
    assert fresh.main([*source, "--receipt", str(receipt)]) == 0
    assert paid_jobs == [{ids["Preview"], ids["New"]}] * 4

    # 同份日常归档重跑，也不把已有文章再交给处理流程。
    with monkeypatch.context() as patch:
        patch.setenv("RESEARCH_CRAWL_RECEIPT", str(receipt))
        assert ingest(daily) == 0
    paid_jobs.clear()
    assert fresh.main([*source, "--receipt", str(receipt)]) == 0
    assert paid_jobs == []
