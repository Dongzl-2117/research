"""research-grok-smoke：部署后核 Grok OAuth 这条路的冒烟和并发压测。

只做两件事，都不写库：

1. ``--check``：发一次很短的真实请求，把接口回的 provider / model / reasoning_effort / fast / 上下文
   和用量打出来。凭证不可用就退出码 2，不会改走 DeepSeek。
2. ``--pressure 8,16,24``：按档位同时发小请求（每档 ``--rounds`` 轮），统计请求数、429 次数
   （含后来重试成功的）、平均耗时，并在本机读 ``memory_pressure``、``docker stats`` 的峰值内存。
   限流明显就停下，不往下一档走。档位不另夹低硬顶。

样例（在 Mac mini 上）：

    docker compose exec app research-grok-smoke --check
    docker compose exec app research-grok-smoke --pressure 8,16,24 --rounds 2
    docker compose exec app research-grok-smoke --image /data/images/fangwei-mp/ab/abcd.png

``--image`` 走识图那条路（`ocr.vision_engine`），只打印识别结果，不写 image_texts，也不动文章。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from researchlib.config import Settings
from researchlib.grok import (
    GROK_CONTEXT_TOKENS,
    GROK_MODEL_ID,
    GROK_REASONING_EFFORT,
    grok_concurrency_plan,
)
from researchlib.llm import LLMError, LLMUnavailable, chat, credential_kind, http_stats, reset_http_stats
from researchlib.llm_provider import llm_provider, provider_label

PROMPT = "只回答两个字：收到"
PRESSURE_PROMPT = "只回答一个字：好"


def _provider_summary(settings: Settings) -> dict:
    return {
        "provider": llm_provider(),
        "label": provider_label(llm_provider()),
        "model": GROK_MODEL_ID,
        "reasoning_effort": GROK_REASONING_EFFORT,
        "fast": False,
        "context_tokens": GROK_CONTEXT_TOKENS,
        "concurrency": settings.llm_concurrency,
        "concurrency_plan": grok_concurrency_plan(),
        "credential": credential_kind(settings) or "missing",
    }


def _check(settings: Settings) -> int:
    print(json.dumps(_provider_summary(settings), ensure_ascii=False))
    usage: dict = {}
    try:
        text, model = chat(settings, [{"role": "user", "content": PROMPT}], max_tokens=512, usage=usage)
    except LLMUnavailable as exc:
        print(f"凭证不可用：{exc}", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"调用失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({"model_reported": model, "content": text.strip(), "usage": usage}, ensure_ascii=False))
    return 0


def _memory_pressure() -> str:
    """macOS 的 memory_pressure 摘要（不在 macOS 上就空着）。"""
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", out)
    return f"free {match.group(1)}%" if match else ""


def _docker_stats() -> dict:
    """app 容器当前的内存占用和上限，用作压测时的峰值参考。"""
    for command in (
        ["docker", "compose", "stats", "--no-stream", "--format", "json", "app"],
        ["docker", "compose", "stats", "--no-stream", "app"],
    ):
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return {}
        if not out:
            continue
        try:
            data = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            continue
        return {
            "mem_usage": data.get("MemUsage") or data.get("MemUsageRaw"),
            "cpu": data.get("CPUPerc"),
        }
    return {}


def _one_request(settings: Settings, slot) -> dict:
    usage: dict = {}
    started = time.monotonic()
    try:
        chat(settings, [{"role": "user", "content": PRESSURE_PROMPT}], max_tokens=256, usage=usage, slot=slot)
        ok, error = True, ""
    except (LLMError, LLMUnavailable) as exc:
        ok, error = False, str(exc)[:200]
    return {"ok": ok, "seconds": time.monotonic() - started, "error": error}


def _pressure(settings: Settings, ladder: list[int], rounds: int) -> int:
    """逐档加并发。每档报请求数、429 次数（含后来重试成功的）、平均耗时、内存。限流明显就停。

    档位按命令行来，不另夹一层低硬顶。每一档自己的信号量传给 ``chat``，用来测这一档，
    不改生产进程的 ``RESEARCH_LLM_CONCURRENCY``。
    """
    rows = []
    for width in ladder:
        width = max(1, width)
        reset_http_stats()
        peak_mem: list[str] = []
        stop = threading.Event()

        def sample(bucket: list[str] = peak_mem, flag: threading.Event = stop) -> None:
            # 默认参数把这一档的桶和开关绑进来，下一档换了新对象不会写进上一档。
            while not flag.is_set():
                found = _docker_stats().get("mem_usage")
                if found:
                    bucket.append(str(found))
                flag.wait(2.0)

        watcher = threading.Thread(target=sample, daemon=True)
        watcher.start()
        slot = threading.BoundedSemaphore(width)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=width) as pool:
            results = list(pool.map(lambda _n, s=slot: _one_request(settings, s), range(width * rounds)))
        stop.set()
        watcher.join(timeout=5)
        elapsed = time.monotonic() - started
        failures = [row for row in results if not row["ok"]]
        # 重试后成功的 429 也算。只数最后失败的请求会把限流看成 0。
        rate_limited = int(http_stats().get("http_429", 0))
        row = {
            "concurrency": width,
            "requests": len(results),
            "failures": len(failures),
            "rate_limited": rate_limited,
            "rate_limited_ratio": round(rate_limited / max(1, len(results)), 3),
            "avg_seconds": round(statistics.fmean(row["seconds"] for row in results), 2),
            "wall_seconds": round(elapsed, 2),
            "memory_pressure": _memory_pressure(),
            "docker_stats_peak": max(peak_mem, key=len) if peak_mem else "",
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
        if rate_limited / max(1, len(results)) > 0.05:
            print(f"并发 {width} 时 429 超过 5%，停在了一档", file=sys.stderr)
            break
        if failures and rate_limited == 0:
            print(f"并发 {width} 时有非 429 的失败，先停下看清原因：{failures[0]['error']}", file=sys.stderr)
            break
    if rows:
        best = max(rows, key=lambda item: item["concurrency"])
        print(json.dumps({"highest_tested": best["concurrency"], "rows": len(rows)}, ensure_ascii=False))
    return 0


def _image(settings: Settings, paths: list[str]) -> int:
    """只认图、只打印：不走 run_ocr，不写 image_texts，也不动文章。"""
    from researchlib.ocr import Recognition, resolve_engine

    try:
        engine, recognize = resolve_engine(settings)
    except Exception as exc:  # noqa: BLE001 - 冒烟要原样报出来
        print(f"识图起不来：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"engine": engine, "images": len(paths)}, ensure_ascii=False))
    status = 0
    for raw in paths:
        path = Path(raw)
        started = time.monotonic()
        try:
            result = recognize(path)
            text = result.text if isinstance(result, Recognition) else str(result)
        except Exception as exc:  # noqa: BLE001
            print(f"{path}: 失败 {exc}", file=sys.stderr)
            status = 1
            continue
        print(
            json.dumps(
                {
                    "image": str(path),
                    "seconds": round(time.monotonic() - started, 1),
                    "chars": len(text or ""),
                    "text": (text or "")[:400],
                },
                ensure_ascii=False,
            )
        )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grok OAuth 冒烟和并发压测（不写库）")
    parser.add_argument("--check", action="store_true", help="打提供方口径并真发一次短请求")
    parser.add_argument("--pressure", default="", help="并发档位，逗号分隔，例如 8,16,24。不另夹低硬顶")
    parser.add_argument("--rounds", type=int, default=2, help="每档发几轮（每轮 concurrency 个请求）")
    parser.add_argument("--image", action="append", default=[], help="识一张本地图，只打印不写库")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if llm_provider() != "grok":
        print(
            "research-grok-smoke 只核 Grok。当前 RESEARCH_LLM_PROVIDER 不是 grok。",
            file=sys.stderr,
        )
        return 2
    if not args.check and args.pressure.strip() == "" and not args.image:
        print(json.dumps(_provider_summary(settings), ensure_ascii=False))
        return 0
    status = 0
    if args.check:
        status |= _check(settings)
    if args.pressure.strip():
        ladder = [int(piece) for piece in args.pressure.split(",") if piece.strip().isdigit()]
        if not ladder:
            print("--pressure 要写成 8,16,24", file=sys.stderr)
            return 2
        status |= _pressure(settings, ladder, max(1, args.rounds))
    if args.image:
        status |= _image(settings, args.image)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
