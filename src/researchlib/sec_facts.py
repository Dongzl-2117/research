"""离线按 SEC 披露日查单项 Company Facts，显式下载单个 CIK 或生成手记复核版本。"""

import argparse
import fcntl
import json
import os
import re
import tempfile
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
MAX_BYTES = 128 * 1024 * 1024


def cik(value):
    s = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", s) or int(s) == 0:
        raise ValueError("CIK 必须是 1 至 10 位的正整数。")
    return s.zfill(10)


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        raise ValueError(f"日期必须使用 YYYY-MM-DD：{value!r}")
    return date.fromisoformat(value)


def instant(value):
    pattern = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(?:Z|[+-]\d\d:\d\d)"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("时间必须精确到秒，并带 Z 或明确的时区偏移。")
    return datetime.fromisoformat(value).astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_document(raw, expected_cik):
    doc = json.loads(raw, parse_float=Decimal)
    if not isinstance(doc, dict) or cik(doc.get("cik")) != cik(expected_cik) or not isinstance(doc.get("facts"), dict):
        raise ValueError("Company Facts 结构或 CIK 与请求不符。")
    return doc


def observe(doc, tag, unit, start, end, before):
    end_day, cutoff = day(end), day(before)
    if start and day(start) > end_day:
        raise ValueError("报告期起点晚于终点。")
    parts = tag.split(":")
    if len(parts) != 2 or not all(parts) or not unit:
        raise ValueError("请输入 taxonomy:tag 和准确的原始单位。")
    taxonomy, concept = parts
    issuer = cik(doc["cik"])
    rows = doc["facts"].get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit, [])
    if not isinstance(rows, list):
        raise ValueError("原始单位下缺少事实列表。")
    observed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("事实列表包含无效记录。")
        if row.get("end") != end or row.get("start") != start:
            continue
        filed = day(row.get("filed"))
        if filed >= cutoff:
            continue
        accn, form, value = row.get("accn"), row.get("form"), row.get("val")
        if not isinstance(accn, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accn):
            raise ValueError("命中记录缺少有效 accession。")
        if not isinstance(form, str) or not form or type(value) not in (int, Decimal) or not Decimal(value).is_finite():
            raise ValueError("命中记录的报表类型或数值无效。")
        filing = f"https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{accn.replace('-', '')}/{accn}-index.htm"
        if accn in observed:
            previous = observed[accn]
            if (previous["filed"], previous["form"], Decimal(previous["actual"])) != (filed.isoformat(), form, value):
                raise ValueError(f"同一 accession 包含互相矛盾的事实，请核对原文：{filing}")
            continue
        observed[accn] = {
            "accession": accn,
            "form": form,
            "filed": filed.isoformat(),
            "actual": str(value),
            "filing": filing,
        }
    history = sorted(observed.values(), key=lambda row: (row["filed"], row["accession"]))
    latest_day = history[-1]["filed"] if history else None
    latest = [row for row in history if row["filed"] == latest_day]
    if not latest:
        status = "missing"
    elif len(latest) == 1:
        status = "available"
    else:
        status = "ambiguous_same_day"
    return {
        "source": "SEC EDGAR Company Facts",
        "cik": issuer,
        "tag": tag,
        "unit": unit,
        "start": start,
        "end": end,
        "before": before,
        "status": status,
        "history": history,
        "latest": latest[0] if len(latest) == 1 else None,
    }


def journal_patch(result, document, note_id, company, metric, accession, published, now):
    if not result["latest"]:
        raise ValueError("当前条件没有唯一的最新可用披露。")
    observation = result["latest"]
    if accession != observation["accession"]:
        raise ValueError("指定的 accession 与截止日前的最新披露不符。")
    if result["end"] > observation["filed"]:
        raise ValueError("报告期晚于披露日，不能以已实现的指标导入手记。")
    accepted = instant(published)
    if accepted > now or accepted.astimezone(EASTERN).date() != day(observation["filed"]):
        raise ValueError("接受时间须在披露当日（纽约时区）且已经发生，请核对申报索引。")
    if (
        not isinstance(document, dict)
        or document.get("format") != "research-journal"
        or document.get("version") != 1
        or not isinstance(document.get("records"), list)
    ):
        raise ValueError("需要投研手记 v1 JSON 备份。")
    matches = [row for row in document["records"] if row["id"] == note_id and row["kind"] == "note"]
    if len(matches) != 1 or not matches[0]["revisions"]:
        raise ValueError("备份中找不到唯一的目标判断。")
    if not re.fullmatch(r"note:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", note_id):
        raise ValueError("判断编号无效。")
    record = matches[0]
    previous = record["revisions"][-1]
    value = previous["value"]
    period = result["end"] if not result["start"] else f"{result['start']}/{result['end']}"
    if (value["company"], value["metric"], value["unit"], value["period"]) != (company, metric, result["unit"], period):
        raise ValueError(f"公司、指标、单位或期间不匹配，请检查选中的判断；期间应为 {period}。")
    update = {"actual": observation["actual"], "published": stamp(accepted), "filing": observation["filing"]}
    if any(value[key] != val for key, val in update.items()):
        if value["filing"] == observation["filing"]:
            raise ValueError("已记录同一来源的不同实绩或发布时间，请先人工核对。")
        if value["published"] and instant(value["published"]) >= accepted:
            raise ValueError("已记录同时或更晚的不同披露，不能覆盖。")
        at = stamp(now)
        if at <= previous["at"]:
            raise ValueError("本机时间须晚于原手记版本。")
        change = (
            f"按 SEC（CIK {result['cik']}）{result['tag']} 核对实绩，"
            f"表单 {observation['form']}，accession {observation['accession']}。"
        )
        revision = {"at": at, "value": {**value, **update, "change": change}}
        record = {**record, "revisions": [*record["revisions"], revision]}
    return {"format": "research-journal", "version": 1, "records": [record]}


def save_new(path, data):
    # 写完再建立目标名，异常或同名并发下载不会留下残缺文件或覆盖历史快照。
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        tmp_path.write_bytes(data)
        os.link(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(issuer, agent, output):
    issuer = cik(issuer)
    if not re.fullmatch(r"[ -~]+", agent) or not re.fullmatch(r".+ \S+@[^\s@]+\.[^\s@]+", agent):
        raise ValueError("User-Agent 请用 ASCII 字符填写机构或姓名和真实的联系邮箱。")
    if output.exists():
        raise ValueError("请为每个快照指定一个新的文件名。")
    endpoint = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{issuer}.json"
    request = Request(endpoint, headers={"User-Agent": agent, "Accept": "application/json"})
    # 该用户所有本机进程共用一把锁。两次请求至少间隔一秒，SEC 限流后暂停。
    lock_dir = Path.home() / ".cache" / "research-sec-facts"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_dir / "next-request").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lock.seek(0)
        wait = float(lock.read() or 0) - time.time()
        if wait > 1:
            raise ValueError(f"SEC 冷却中，请在 {int(wait) + 1} 秒后再运行。")
        if wait > 0:
            time.sleep(wait)
        cooldown = 1
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as err:
            if err.code in (403, 429):
                retry = err.headers.get("Retry-After", "")
                cooldown = 600
                if retry.isdigit():
                    cooldown = max(cooldown, int(retry))
                elif retry:
                    try:
                        cooldown = max(cooldown, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, OverflowError, TypeError):
                        pass
            raise
        finally:
            lock.seek(0)
            lock.truncate()
            lock.write(str(time.time() + cooldown))
    if len(raw) > MAX_BYTES:
        raise ValueError("快照超过 128 MiB。")
    parse_document(raw, issuer)
    save_new(output, raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("fetch", help="显式请求 SEC，保存一个新快照")
    download.add_argument("--cik", required=True)
    download.add_argument("--user-agent", required=True)
    download.add_argument("--output", type=Path, required=True)
    for name, help_text in (("observe", "离线查询截止日以前的申报和修订"), ("journal", "离线生成手记复核补丁")):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("snapshot", type=Path)
        for field in ("cik", "tag", "unit", "end", "before"):
            cmd.add_argument("--" + field, required=True)
        cmd.add_argument("--start", help="期间指标须填起点，时点指标省略")
        cmd.add_argument("--output", type=Path, help="保存到新文件，默认输出到终端")
        if name == "journal":
            cmd.add_argument("--backup", type=Path, required=True)
            for field in ("note-id", "company", "metric", "accession", "published-at"):
                cmd.add_argument("--" + field, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            fetch(args.cik, args.user_agent, args.output)
            return
        doc = parse_document(args.snapshot.read_bytes(), args.cik)
        result = observe(doc, args.tag, args.unit, args.start, args.end, args.before)
        if args.command == "journal":
            result = journal_patch(
                result,
                json.loads(args.backup.read_bytes()),
                args.note_id,
                args.company,
                args.metric,
                args.accession,
                args.published_at,
                datetime.now(UTC),
            )
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            save_new(args.output, payload.encode())
        else:
            print(payload, end="")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        parser.exit(2, f"错误：{error}\n")


if __name__ == "__main__":
    main()
