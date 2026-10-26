"""出问题才提示：备份、试恢复、定时导入、夜间补齐、自动上线、后台重算和 LLM 调用的最近结果。

每个任务把结果写进 DATA_DIR/status/<任务>.json：宿主机上的脚本（备份、试恢复、定时抓取、夜间补齐、自动上线）
在成功和失败时都写，进程里的后台重算和 LLM 调用由这里的函数写。首页只在出问题时提示一行，
正常时什么都不显示。LLM 的提示按当时的提供方说（DeepSeek 或 Grok）。

文件格式：{"task": "backup", "ok": true, "at": "2026-10-20T03:17:42Z", "message": "…"}，
失败时另记上一次成功的时间 last_ok_at。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("researchlib.health")

STATUS_DIR = "status"

# 任务名 → (页面上的叫法, 多久没成功就算有问题；None 表示只看最近一次成没成功)
TASKS: dict[str, tuple[str, timedelta | None]] = {
    "backup": ("备份", timedelta(hours=36)),
    "restore-check": ("试恢复", timedelta(days=40)),
    "crawl-fangwei-mp": ("方伟公众号抓取", timedelta(hours=30)),
    "crawl-fangwei-zsxq": ("方伟星球抓取", timedelta(hours=30)),
    "ingest-aihot": ("AIHOT 定时导入", timedelta(hours=6)),
    "nightly": ("夜间补齐", timedelta(hours=50)),
    "upkeep": ("后台重算", None),
    "deploy": ("自动上线", None),
}
# LLM 连续失败这么多次才提示；凭证不可用（拿不到 key 或登录失效）马上提示。
LLM_FAILURES_TO_WARN = 3


def status_path(data_dir: Path, task: str) -> Path:
    return Path(data_dir) / STATUS_DIR / f"{task}.json"


def read_status(data_dir: Path, task: str) -> dict | None:
    try:
        # 脚本在宿主机上写，万一截断了半个汉字也照样读出来，不能因此丢掉一条失败提示。
        data = json.loads(status_path(data_dir, task).read_bytes().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(data_dir: Path, task: str, ok: bool, message: str = "", **extra) -> None:
    """原子地写一个任务的状态。失败时保留上一次成功的时间。写不进去只记日志，不往外抛。"""
    path = status_path(data_dir, task)
    previous = read_status(data_dir, task) or {}
    payload = {"task": task, "ok": bool(ok), "at": _now_iso(), "message": (message or "")[:500]}
    if ok:
        payload["last_ok_at"] = payload["at"]
    elif previous.get("last_ok_at"):
        payload["last_ok_at"] = previous["last_ok_at"]
    payload.update(extra)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("写状态文件失败 %s：%s", path, exc)


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _when(moment: datetime | None, zone_name: str) -> str:
    if moment is None:
        return ""
    return moment.astimezone(ZoneInfo(zone_name)).strftime("%m-%d %H:%M")


def _span(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 48:
        return f"{max(1, int(hours))} 小时"
    return f"{int(hours // 24)} 天"


def notices(data_dir: Path, zone_name: str, now: datetime | None = None) -> list[str]:
    """出了问题的任务，每个一句话。没有问题返回空列表。"""
    moment = now or datetime.now(UTC)
    found: list[str] = []
    for task, (label, max_age) in TASKS.items():
        data = read_status(data_dir, task)
        if data is None:
            continue
        at = parse_time(data.get("at"))
        message = str(data.get("message") or "").strip()
        if not data.get("ok"):
            text = f"{label}失败（{_when(at, zone_name)}）"
            found.append(text + (f"：{message}" if message else ""))
        elif max_age is not None and at is not None and moment - at > max_age:
            found.append(f"{label}已经 {_span(moment - at)}没成功了（最近一次 {_when(at, zone_name)}）")
    llm = read_status(data_dir, "llm")
    if llm and not llm.get("ok"):
        since = parse_time(llm.get("since")) or parse_time(llm.get("at"))
        message = str(llm.get("message") or "").strip()
        # 旧状态文件没有 provider，按当时的 Grok 说；新写的都带。
        label = str(llm.get("provider") or "").strip() or "Grok"
        if llm.get("unavailable"):
            if label == "DeepSeek":
                found.append(
                    f"DeepSeek 凭证不可用（{_when(since, zone_name)} 起），摘要、观点和问库都停了："
                    "在 .env 或 compose 里填 DEEPSEEK_API_KEY。"
                )
            elif label == "Grok":
                found.append(
                    f"Grok 登录失效（{_when(since, zone_name)} 起），摘要、观点和问库都停了：到 AIHOT 那边重新登录。"
                )
            else:
                found.append(
                    f"{label} 凭证不可用（{_when(since, zone_name)} 起），摘要、观点和问库都停了。"
                )
        elif int(llm.get("failures") or 0) >= LLM_FAILURES_TO_WARN:
            found.append(
                f"{label} 调用连续失败 {int(llm['failures'])} 次（{_when(since, zone_name)} 起）"
                + (f"：{message}" if message else "")
            )
    return found


# —— LLM 调用的健康状况：连续失败计数 ——

_llm_lock = threading.Lock()
_llm_cache: dict[str, dict] = {}


def record_llm(
    data_dir: Path | None,
    *,
    ok: bool,
    error: str = "",
    unavailable: bool = False,
    provider: str = "",
) -> None:
    """每次 LLM 调用的最终结果。成功且之前也正常时什么都不写；失败才累计次数。"""
    if data_dir is None:
        return
    key = str(data_dir)
    with _llm_lock:
        previous = _llm_cache.get(key)
        if previous is None:
            previous = read_status(data_dir, "llm") or {"ok": True}
        if ok:
            if previous.get("ok", True):
                _llm_cache[key] = previous
                return
            state = {"ok": True, "failures": 0}
            write_status(data_dir, "llm", True, failures=0, provider=provider)
        else:
            was_ok = previous.get("ok", True)
            failures = (0 if was_ok else int(previous.get("failures") or 0)) + 1
            since = _now_iso() if was_ok else (previous.get("since") or _now_iso())
            state = {"ok": False, "failures": failures, "since": since, "unavailable": unavailable}
            write_status(
                data_dir, "llm", False, error,
                failures=failures, since=since, unavailable=unavailable, provider=provider,
            )
        _llm_cache[key] = state


def reset_llm_cache() -> None:
    with _llm_lock:
        _llm_cache.clear()
