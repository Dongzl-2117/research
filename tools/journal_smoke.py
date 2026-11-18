"""用 Chromium 核验本机手记。安装项目依赖、Playwright 并设置 JOURNAL_CHROMIUM 后运行。
文章和公司提供合成记录，其余页面和资产走真实应用路由。不会连接正式数据库或模型。
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from fastapi.testclient import TestClient
from markupsafe import Markup
from playwright.sync_api import expect, sync_playwright

from researchlib.web.app import create_app
from researchlib.web.routes import templates

ORIGIN = "http://127.0.0.1:8088"
DEFAULT = {"title": "样本", "canonical": ORIGIN, "nav": "archive"}
BASE = "2026-05-02T09:00:00+00:00"


def serve(route, client, requests):
    req = route.request
    path = urlsplit(req.url).path
    requests.append((req.method, req.url))
    assert req.url.startswith(ORIGIN + "/") and req.method == "GET", req.url
    if path == "/articles/9":
        data = dict(DEFAULT, article={"id": 9, "title": "样本公告"}, date="2026-05-01", body_language="original",
                    body_html=Markup('<p class="is-quoted">软件订阅收入的数据。</p>'),
                    source=SimpleNamespace(id="sample", author="李四", name="样本", timeline=True))
        return route.fulfill(body=templates().env.get_template("article.html").render(data),
                             content_type="text/html; charset=utf-8")
    if path == "/companies/13":
        data = dict(DEFAULT, company={"id": 13, "name_zh": "样本科技"}, tab="articles",
                    total=0, company_authors=[], page=1, pages=1)
        return route.fulfill(body=templates().env.get_template("company.html").render(data),
                             content_type="text/html; charset=utf-8")
    result = client.get(path)
    route.fulfill(body=result.content, status=result.status_code,
                  content_type=result.headers.get("content-type", "application/octet-stream"))


def run(browser, width):
    ctx = browser.new_context(viewport={"width": width, "height": 830}, is_mobile=width < 500,
                              has_touch=width < 500, timezone_id="UTC", accept_downloads=True)
    client = TestClient(create_app())
    requests = []
    ctx.route("**/*", lambda route: serve(route, client, requests))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.clock.install(time=BASE)
    page.set_default_timeout(4500)
    page.goto(ORIGIN + "/companies/13")
    nav = ".sidebar" if width >= 1024 else ".tabbar"
    page.locator(nav).get_by_role("link", name="手记").click()
    expect(page.locator("#notes")).to_contain_text("还没有判断")
    expect(page.locator("#watches")).to_contain_text("暂无自选")
    expect(page.locator("#due-list")).to_contain_text("没有到期")
    assert page.locator("body").evaluate("e=>e.scrollWidth<=innerWidth")
    page.goto(ORIGIN + "/companies/13")
    page.get_by_role("link", name="管理自选").click()
    watch = page.locator("#watch-form")
    expect(watch.locator("input")).to_have_value("样本科技")
    watch.get_by_role("button").click()
    expect(page.locator("#watches a")).to_have_attribute("href", "/companies/13")
    page.goto(ORIGIN + "/companies/13")
    page.get_by_role("link", name="写公司手记").click()
    expect(page.locator('#note-form [name="company"]')).to_have_value("样本科技")
    page.goto(ORIGIN + "/articles/9?chunk=3")
    excerpt_link = page.get_by_role("link", name="记入手记")
    expect(excerpt_link).to_have_attribute("href", re.compile("excerpt="))
    page.locator(".reader .is-quoted").evaluate("""e => {
        const range = document.createRange();
        range.setStart(e.firstChild, 0);
        range.setEnd(e.firstChild, 6);
        document.getSelection().removeAllRanges();
        document.getSelection().addRange(range);
    }""")
    expect(excerpt_link).to_have_attribute("href", re.compile("excerpt=" + quote("软件订阅收入") + "$"))
    excerpt_link.click()
    form = page.locator("#note-form")

    def field(key):
        return form.locator(f'[name="{key}"]')

    expect(field("ref")).to_have_value("/articles/9?chunk=3")
    expect(field("excerpt")).to_have_value("软件订阅收入")
    field("company").fill("样本科技")
    field("hypothesis").fill("续费改善会提高净留存")
    field("falsifier").fill("续费率连续两季下降")
    field("review").fill("2026-05-02")
    field("metric").fill("净留存")
    field("unit").fill("%")
    field("period").fill("2026 Q1")
    field("expected").fill("112")
    form.get_by_role("button", name="保存此版本").click()
    expect(page.locator("#notes")).to_contain_text("续费改善")
    expect(page.locator("#due-list")).to_contain_text("2026-05-02")
    page.reload()
    expect(page.locator("#notes")).to_contain_text("112")
    expect(page.locator("#notes a", has_text="核对原文")).to_have_attribute("href", "/articles/9?chunk=3")
    page.clock.set_system_time("2026-05-04T10:00:00+00:00")
    page.get_by_role("button", name="记录新判断").click()
    field("actual").fill("108")
    field("published").fill("2026-05-05T08:00")
    field("filing").fill("https://example.org/filing")
    form.get_by_role("button", name="保存此版本").click()
    expect(page.locator("#journal-status")).to_contain_text("晚于本次")
    field("published").fill("2026-05-03T08:00")
    field("change").fill("低于预期，下调增长判断")
    field("review").fill("2026-06-01")
    form.get_by_role("button", name="保存此版本").click()
    expect(page.locator("#notes")).to_contain_text("下调增长判断")
    expect(page.locator("#due-list")).to_contain_text("没有到期")
    page.locator("#as-of").fill("2026-05-02T11:00")
    expect(page.locator("#notes")).not_to_contain_text("108")
    expect(page.locator("#notes")).not_to_contain_text("下调增长判断")
    expect(page.locator("#due-list")).to_contain_text("2026-05-02")
    expect(page.locator("#new-note")).to_be_hidden()
    page.get_by_role("button", name="返回当前").click()
    page.get_by_text("备份与恢复", exact=True).click()
    with page.expect_download() as dl:
        page.get_by_role("button", name="导出 JSON").click()
    backup = json.loads(dl.value.path().read_text())
    assert {r["kind"] for r in backup["records"]} == {"note", "watch"}

    def upload(doc):
        page.locator("#import").set_input_files({"name": "journal.json", "mimeType": "application/json",
                                                 "buffer": json.dumps(doc, ensure_ascii=False).encode()})

    upload(backup)
    expect(page.locator("#journal-status")).to_contain_text("已合并 0 条")
    # 同一批先插入新编号，随后发生分支冲突，整笔写入应回滚。
    conflict = json.loads(json.dumps(backup))
    note = next(r for r in conflict["records"] if r["kind"] == "note")
    other = dict(note, id="note:abababab-abab-abab-abab-abababababab")
    conflict["records"].insert(0, other)
    note["revisions"] = json.loads(json.dumps(note["revisions"]))
    note["revisions"][0]["value"]["hypothesis"] = "冲突分支"
    upload(conflict)
    expect(page.locator("#journal-status")).to_contain_text("冲突")
    page.reload()
    expect(page.locator(".journal-note")).to_have_count(1)
    # 两个标签编辑同一旧版，先保存的版本保留；后保存的提示重新打开。
    page.get_by_role("button", name="记录新判断").click()
    peer = ctx.new_page()
    page.clock.set_system_time("2026-05-04T11:00:00+00:00")
    peer.goto(ORIGIN + "/journal")
    peer.get_by_role("button", name="记录新判断").click()
    peer.locator('[name="change"]').fill("并发写入的最新版本")
    peer.get_by_role("button", name="保存此版本").click()
    expect(peer.locator("#notes")).to_contain_text("并发写入")
    field("change").fill("旧标签里的改动")
    form.get_by_role("button", name="保存此版本").click()
    expect(page.locator("#journal-status")).to_contain_text("已更新")
    assert not errors, errors
    assert not any("续费" in url or "hypothesis=" in url or method != "GET" for method, url in requests)
    ctx.close()
    # 全新浏览器上下文恢复 JSON，核验两版判断、到期日、自选和来源。
    clean = browser.new_context(timezone_id="UTC")
    clean.route("**/*", lambda route: serve(route, client, requests))
    page = clean.new_page()
    page.clock.install(time="2026-05-05T12:00:00+00:00")
    page.goto(ORIGIN + "/journal")
    expect(page.locator("#notes")).to_contain_text("还没有判断")
    page.get_by_text("备份与恢复", exact=True).click()
    upload(backup)
    expect(page.locator("#journal-status")).to_contain_text("已合并 2 条")
    expect(page.locator("#notes")).to_contain_text("下调增长判断")
    expect(page.locator("#watches")).to_contain_text("样本科技")
    page.get_by_text("此前的判断（1 版）", exact=True).click()
    expect(page.locator("#notes details")).to_contain_text("当时计划复核：2026-05-02")
    clean.close()
    print(f"{width}px：入口、持久化、复核、时点、备份恢复、冲突和私密请求检查通过")


def check_financial_revisions(browser):
    ctx = browser.new_context(timezone_id="America/New_York")
    client = TestClient(create_app())
    ctx.route("**/*", lambda route: serve(route, client, []))
    page = ctx.new_page()
    page.clock.install(time=BASE)
    page.goto(ORIGIN + "/journal")
    page.get_by_text("备份与恢复", exact=True).click()
    identity = "note:11111111-1111-4111-8111-111111111111"
    accession = "0000987654-24-000001"
    filing = "https://www.sec.gov/Archives/edgar/data/1234567/000098765424000001/" + accession + "-index.htm"
    v = dict(company="示例", companyId="", hypothesis="订阅增长", falsifier="", review="", ref="", excerpt="",
             metric="营收", unit="USD", period="2024 Q1", expected="125", actual="", published="", filing="", change="")
    initial = {"at": "2024-04-01T00:00:00.000Z", "value": v}
    announced = "2024-11-03T06:30:00.375Z"
    result = {**v, "actual": "110", "published": announced, "filing": filing}
    sec = dict(cik="0001234567", tag="us-gaap:Revenues", accession=accession, form="10-Q/A", filed="2024-11-03")
    first = {"at": "2024-11-03T06:31:00.100Z", "value": result, "secFact": sec}

    def import_tail(tail):
        doc = {"format": "research-journal", "version": 1, "records": [
            {"id": identity, "kind": "note", "revisions": [initial, tail]}]}
        page.locator("#import").set_input_files({"name": "filing.json", "mimeType": "application/json",
                                                 "buffer": json.dumps(doc).encode()})

    def export_versions():
        with page.expect_download() as download:
            page.locator("#export").click()
        return json.loads(download.value.path().read_bytes())["records"][0]["revisions"]

    import_tail({key: val for key, val in first.items() if key != "secFact"})
    expect(page.locator("#journal-status")).to_contain_text("已合并 1 条")
    assert "secFact" not in export_versions()[-1]
    import_tail(first)
    expect(page.locator("#journal-status")).to_contain_text("已合并 1 条")
    assert export_versions()[-1] == first
    replay = {**first, "at": "2024-11-03T07:00:00.000Z"}
    import_tail(replay)
    expect(page.locator("#journal-status")).to_contain_text("已合并 0 条")
    assert export_versions()[-1] == first
    import_tail({key: val for key, val in first.items() if key != "secFact"})
    expect(page.locator("#journal-status")).to_contain_text("已合并 0 条")
    assert export_versions()[-1] == first
    import_tail({**replay, "value": {**result, "actual": "108"}})
    expect(page.locator("#journal-status")).to_contain_text("冲突")
    assert export_versions()[-1] == first
    page.get_by_role("button", name="记录新判断").click()
    page.locator('[name="change"]').fill("复核后维持假设")
    page.get_by_role("button", name="保存此版本").click()
    expect(page.locator("#journal-status")).to_contain_text("已保存")
    assert export_versions()[-1]["value"]["published"] == announced
    page.locator("#as-of").fill("2024-11-03T01:45")
    expect(page.locator("#journal-status")).to_contain_text("出现两次")
    page.locator("#journal-zone").select_option("utc")
    page.locator("#as-of").fill("2024-11-03T06:31:00.099")
    expect(page.locator("#notes")).not_to_contain_text("110")
    page.locator("#as-of").fill("2024-11-03T06:31:00.1")
    expect(page.locator("#notes")).to_contain_text("110")
    page.get_by_role("button", name="返回当前").click()
    page.get_by_role("button", name="记录新判断").click()
    period = page.locator('[name="period"]')
    period.fill("2024 Q2")
    period.press("Tab")
    for key in ("expected", "actual", "filing", "published"):
        expect(page.locator(f'[name="{key}"]')).to_have_value("")
    page.get_by_role("button", name="保存此版本").click()
    expect(page.locator("#journal-status")).to_contain_text("已保存")
    versions = export_versions()
    assert versions[-1]["value"]["period"] == "2024 Q2" and versions[-1]["value"]["actual"] == ""
    assert versions[-2]["value"]["actual"] == "110" and versions[-2]["value"]["period"] == "2024 Q1"
    ctx.close()
    print("多次生成的 SEC 补丁、恢复冲突、夏令时回拨、毫秒级回看和跨财期复核通过")


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("JOURNAL_CHROMIUM"), headless=True)
        for width in (1440, 390):
            run(browser, width)
        check_financial_revisions(browser)
        browser.close()
