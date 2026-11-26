"""GET /api/v1/status：云端看定时任务的现状。只给任务名、状态、时间，不带报错原文。"""

from fastapi.testclient import TestClient

from researchlib import health
from researchlib.web.app import create_app


def test_status_api_lists_tasks_and_keeps_error_text_private(conn, settings, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
    health.write_status(settings.data_dir, "backup", True, "备份完成：7.5M")
    health.write_status(settings.data_dir, "crawl-fangwei-zsxq", True, "新增 3 篇")
    health.write_status(
        settings.data_dir, "crawl-fangwei-zsxq", False, "抓取失败（退出码 1）：https://example.invalid/feed?key=abc"
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/status")
        listed = client.get("/llms.txt").text
    assert response.status_code == 200
    data = response.json()
    assert data["commit"] == "0123456789abcdef0123456789abcdef01234567"
    by_task = {item["task"]: item for item in data["tasks"]}
    assert set(health.TASKS) <= set(by_task)
    assert by_task["backup"]["state"] == "ok" and by_task["backup"]["at"]
    crawl = by_task["crawl-fangwei-zsxq"]
    # 失败时带着上一次成功的时间：从外面看得出断了多久。
    assert crawl["state"] == "failed" and crawl["last_ok_at"] and crawl["label"] == "方伟星球抓取"
    assert by_task["nightly"]["state"] == "unknown"
    assert data["problems"] == len(health.notices(settings.data_dir, settings.display_timezone)) >= 1
    assert "example.invalid" not in response.text and "message" not in response.text
    assert "/api/v1/status" in listed
