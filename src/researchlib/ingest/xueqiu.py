"""把已经抓下来的雪球帖转成通用 Markdown 归档。

不访问雪球，也不再判断转发或评论。目录里的帖子 JSON 会写成带 frontmatter 的
Markdown，外部 id 是雪球帖子 id，`research-ingest-markdown` 再导一遍不会复制。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from researchlib.textutil import split_frontmatter

AUTHOR = "黑黑无常"
SOURCE_ID = "heiheiwuchang"
_STATUS_ID = re.compile(r"^\d{5,}$")
# 抓取目录里的元数据，不是帖子。文件名以 _ 开头的一律跳过。
SKIP_NAMES = frozenset({"_summary.json", "_STOP.json", "_STOP.png", "_run_meta.json"})


@dataclass
class ConvertReport:
    written: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [path.stem for path in self.written]


def _yaml_str(value: str) -> str:
    """双引号标量。JSON 字符串是合法的 YAML 双引号，标题里的冒号和问号不会被拆开。"""
    return json.dumps(value, ensure_ascii=False)


def _post_id(post: dict) -> str | None:
    raw = post.get("id")
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if _STATUS_ID.fullmatch(text):
        return text
    return None


def _body_text(post: dict) -> str | None:
    raw = post.get("body_text")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _plain_markdown(text: str, title: str) -> str:
    """同名 .md 没有 frontmatter，第一行常常是重复的 # 标题。正文用剩下的段落。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("---\n"):
        _front, rest = split_frontmatter(text)
        if rest != text:
            text = rest
    first, sep, remainder = text.partition("\n")
    if first.startswith("# "):
        heading = first[2:].strip()
        if not title or heading == title:
            return remainder.lstrip("\n") if sep else ""
    return text


def _title(post: dict, body: str) -> str:
    raw = post.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    for line in body.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned
    return "未命名"


def _url(post: dict, post_id: str) -> str:
    raw = post.get("url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    author_id = str(post.get("author_id") or "").strip()
    if author_id and re.fullmatch(r"\d+", author_id):
        return f"https://xueqiu.com/{author_id}/{post_id}"
    return f"https://xueqiu.com/{post_id}"


def render_markdown(post: dict, body: str) -> str:
    """frontmatter 的 id 就是雪球帖子 id。正文用完整 body_text，不截断。"""
    post_id = _post_id(post)
    if not post_id:
        raise ValueError("缺少雪球帖子 id")
    title = _title(post, body)
    published = str(post.get("published_at") or "").strip()
    lines = [
        "---",
        f"id: {_yaml_str(post_id)}",
        f"title: {_yaml_str(title)}",
        f"url: {_yaml_str(_url(post, post_id))}",
    ]
    if published:
        # date 在导入器的日期键里排在 published 前面，两个写同一个值。
        lines.append(f"date: {_yaml_str(published)}")
        lines.append(f"published: {_yaml_str(published)}")
    lines.append(f"author: {_yaml_str(AUTHOR)}")
    lines.append("---")
    text = "\n".join(lines) + "\n"
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    if normalized:
        text += normalized
        if not text.endswith("\n"):
            text += "\n"
    return text


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("不是 JSON 对象")
    return data


def _sibling_markdown(directory: Path, post_id: str, json_path: Path) -> Path | None:
    for candidate in (directory / f"{post_id}.md", json_path.with_suffix(".md")):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _post_body(directory: Path, post: dict, post_id: str, json_path: Path) -> str:
    body = _body_text(post)
    if body is not None:
        return body
    sibling = _sibling_markdown(directory, post_id, json_path)
    if sibling is None:
        return ""
    return _plain_markdown(sibling.read_text(encoding="utf-8-sig"), _title(post, ""))


def convert_directory(source: Path, output: Path) -> ConvertReport:
    """读 source 里的帖子 JSON，把 Markdown 写到另一个目录。

    全部转换成功时，删掉输出目录里这次没写到的 .md，避免上一份归档里多出来的帖子被一起导入。
    有读失败时不动那些多余文件。
    """
    report = ConvertReport()
    if not source.is_dir():
        report.errors.append(f"找不到抓取目录：{source}")
        return report
    if source.resolve() == output.resolve():
        report.errors.append("输出目录要和抓取目录分开，避免盖掉原始的 md")
        return report
    output.mkdir(parents=True, exist_ok=True)
    written_names: set[str] = set()
    seen: dict[str, str] = {}
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith("_") or path.name in SKIP_NAMES:
            report.skipped.append(path.name)
            continue
        if path.suffix.lower() != ".json":
            report.skipped.append(path.name)
            continue
        try:
            post = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            report.errors.append(f"{path.name}: {exc}")
            continue
        post_id = _post_id(post)
        if post_id is None:
            report.skipped.append(path.name)
            continue
        if post_id in seen:
            report.errors.append(f"帖子 id 重复：{post_id}（{path.name} 与 {seen[post_id]}）")
            continue
        seen[post_id] = path.name
        try:
            body = _post_body(source, post, post_id, path)
            rendered = render_markdown(post, body)
        except (OSError, UnicodeError, ValueError) as exc:
            report.errors.append(f"{path.name}: {exc}")
            continue
        target = output / f"{post_id}.md"
        target.write_text(rendered, encoding="utf-8")
        written_names.add(target.name)
        report.written.append(target)
    if not report.written and not report.errors:
        report.errors.append(f"没有可转换的帖子：{source}")
    # 只在这一次完整写完时清掉输出目录里多余的 md，避免读失败把上一份归档删掉。
    if report.written and not report.errors:
        for stale in output.glob("*.md"):
            if stale.name not in written_names and stale.is_file() and not stale.is_symlink():
                stale.unlink()
    report.written.sort(key=lambda item: item.name)
    return report


def render_report(report: ConvertReport, output: Path) -> str:
    lines = [f"写成 {len(report.written)} 篇，目录 {output}"]
    if report.skipped:
        lines.append("跳过：" + "、".join(report.skipped))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把雪球「黑黑无常」已抓取的 JSON 转成 Markdown 归档")
    parser.add_argument("--input", required=True, type=Path, help="抓取目录（{id}.json，可有同名 .md）")
    parser.add_argument("--output", required=True, type=Path, help="给 research-ingest-markdown 的目录")
    args = parser.parse_args(argv)
    report = convert_directory(args.input, args.output)
    print(render_report(report, args.output))
    for error in report.errors:
        print(error, file=sys.stderr)
    if report.errors or not report.written:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
