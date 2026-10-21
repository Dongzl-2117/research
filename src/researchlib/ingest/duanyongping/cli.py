"""段永平博客 PDF → Markdown → 现有白名单导入。

预期的 PDF（约 40MB，约 4440 页）在主人的 Mac 上：

    /Users/dongzhilin/Downloads/投资资料/段永平博客文章合集（+2006+-+2018+）.pdf

协调人把它拷到本机 data/inbox 之后：

    docker compose exec app research-ingest-duanyongping \\
      --pdf "/data/inbox/段永平博客文章合集（+2006+-+2018+）.pdf"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from researchlib.ingest.duanyongping import SOURCE_ID
from researchlib.ingest.duanyongping.emit import render_report, report_payload, write_markdown, write_report
from researchlib.ingest.duanyongping.extract import lines_to_blocks, parse_text, parse_xml
from researchlib.ingest.duanyongping.segment import entries_from_blocks

DEFAULT_PDF = "/Users/dongzhilin/Downloads/投资资料/段永平博客文章合集（+2006+-+2018+）.pdf"
_HELP = f"""
把段永平博客 PDF 拆成条目，只保留谈到具体公司的，再按现有 Markdown 导入写进来源「段永平」。

问答整条保留（读者的问题 + 段永平的回答 + 回答后面别人的回复）。
有日期的进首页按天；没有日期的不进按天，只出现在作者页、公司页和检索。
观点和摘要只统计段永平本人写的段落。读者提问和博友回复不进模型。

已经入库的旧数据不读 PDF、不调用模型，回扫一遍：

  research-ingest-duanyongping --reclassify --dry-run
  research-ingest-duanyongping --reclassify

然后只对这个来源抽观点：

  research-views --source duanyongping-blog --limit 100

PDF 预期路径（不会随仓库分发，约 40MB）：
  {DEFAULT_PDF}

拷到服务器的 data/inbox 后，一条命令抽完并入库：

  docker compose exec app research-ingest-duanyongping \\
    --pdf "/data/inbox/段永平博客文章合集（+2006+-+2018+）.pdf"

只生成 Markdown 和分类报告、不写数据库：

  docker compose exec app research-ingest-duanyongping \\
    --pdf "/data/inbox/段永平博客文章合集（+2006+-+2018+）.pdf" \\
    --emit-only

本机已经装了 poppler，并且 DATABASE_URL 指向本库时，可以直接：

  research-ingest-duanyongping \\
    --pdf "{DEFAULT_PDF}"

依赖 poppler 的 pdftohtml（保留加粗，用来认出段永平的回答）和 pdfinfo。
pdftohtml 不可用时退回 pdftotext。测试夹具用 --xml 或 --text，不需要那份 40MB 的 PDF。
"""


def default_out() -> Path:
    data = os.environ.get("DATA_DIR")
    if data:
        return Path(data) / "inbox" / "duanyongping-blog"
    return Path("data/inbox/duanyongping-blog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-ingest-duanyongping",
        description="导入段永平博客 PDF（来源「段永平」）",
        epilog=_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--pdf", type=Path, help=f"本地 PDF。预期 {DEFAULT_PDF}")
    source.add_argument("--xml", type=Path, help="pdftohtml -xml 的输出，测试夹具用")
    source.add_argument("--text", type=Path, help="pdftotext -layout 的输出，测试夹具用")
    parser.add_argument("--source", default=SOURCE_ID, help="白名单来源 id，默认 duanyongping-blog")
    parser.add_argument("--out", type=Path, help="Markdown 目录，默认 $DATA_DIR/inbox/duanyongping-blog")
    parser.add_argument("--report", type=Path, help="分类报告 JSON，默认同输出目录的 report.json")
    parser.add_argument("--pages", help="只抽这些页，例如 1-20。默认全文")
    parser.add_argument("--emit-only", action="store_true", help="只写 Markdown 和报告，不入库")
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="回扫库里的 duanyongping-blog：洗页码垃圾，博友回复标非本人，无具体公司不进观点。不读 PDF，不调用模型",
    )
    parser.add_argument("--dry-run", action="store_true", help="配合 --reclassify：只计数，不写库")
    return parser


def _page_args(pages: str | None) -> list[str]:
    if not pages:
        return []
    if "-" in pages:
        first, last = pages.split("-", 1)
    else:
        first = last = pages
    if not first.isdigit() or not last.isdigit():
        raise SystemExit(f"页码范围不对：{pages}。写成 1-20 或 3。")
    return ["-f", first, "-l", last]


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(command, capture_output=True, check=False)
    except OSError:
        return None


def pdf_page_count(path: Path) -> int | None:
    proc = _run(["pdfinfo", str(path)])
    if proc is None or proc.returncode != 0:
        return None
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if line.lower().startswith("pages:"):
            raw = line.split(":", 1)[1].strip()
            return int(raw) if raw.isdigit() else None
    return None


def read_pdf(path: Path, pages: str | None) -> tuple[list, str]:
    """返回 (段落, 说明)。优先 pdftohtml XML，好认出加粗的回答。"""
    span = _page_args(pages)
    xml_proc = _run(["pdftohtml", "-xml", "-i", "-nodrm", "-stdout", "-enc", "UTF-8", *span, str(path)])
    if xml_proc is not None and xml_proc.returncode == 0:
        xml = xml_proc.stdout.decode("utf-8", errors="replace")
        if "<page" in xml.lower():
            blocks = lines_to_blocks(parse_xml(xml))
            if blocks:
                return blocks, "pdftohtml"
    text_proc = _run(["pdftotext", "-layout", "-enc", "UTF-8", *span, str(path), "-"])
    if text_proc is None:
        raise SystemExit("没有 pdftohtml 或 pdftotext。先安装 poppler。")
    if text_proc.returncode != 0:
        detail = text_proc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"抽不了文字：{path}。{detail}")
    text = text_proc.stdout.decode("utf-8", errors="replace")
    blocks = lines_to_blocks(parse_text(text))
    if not blocks:
        raise SystemExit(f"{path} 没有抽出文字。确认这是文字版 PDF，不是扫描件。")
    return blocks, "pdftotext"


def load_blocks(args: argparse.Namespace) -> tuple[list, str]:
    if args.xml is not None:
        if not args.xml.is_file():
            raise SystemExit(f"找不到 XML：{args.xml}")
        return lines_to_blocks(parse_xml(args.xml.read_text(encoding="utf-8"))), str(args.xml)
    if args.text is not None:
        if not args.text.is_file():
            raise SystemExit(f"找不到文本：{args.text}")
        return lines_to_blocks(parse_text(args.text.read_text(encoding="utf-8"))), str(args.text)
    if not args.pdf.is_file():
        raise SystemExit(
            f"找不到 PDF：{args.pdf}\n预期路径：{DEFAULT_PDF}\n"
            "拷到 data/inbox 后把 --pdf 指到 /data/inbox 里的那份。"
        )
    count = pdf_page_count(args.pdf)
    blocks, how = read_pdf(args.pdf, args.pages)
    origin = f"{args.pdf}（{how}"
    if count is not None:
        origin += f"，pdfinfo {count} 页"
    origin += "）"
    return blocks, origin


def require_enabled(source_id: str) -> None:
    from researchlib.config import Settings
    from researchlib.sources import SourceRejected, load_sources, require_source

    settings = Settings.from_env()
    try:
        require_source(load_sources(settings.sources_path), source_id, kind="markdown")
    except SourceRejected as exc:
        print(f"拒绝导入：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _reclassify(dry_run: bool) -> int:
    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.ingest.duanyongping.reclassify import format_report, reclassify_source

    conn = prepare(Settings.from_env())
    try:
        report = reclassify_source(conn, dry_run=dry_run)
    finally:
        conn.close()
    print(format_report(report, dry_run=dry_run))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and not args.reclassify:
        raise SystemExit("--dry-run 只配合 --reclassify")
    if args.reclassify and (args.pdf or args.xml or args.text):
        raise SystemExit("--reclassify 不和 --pdf、--xml、--text 一起用")
    if args.reclassify:
        return _reclassify(args.dry_run)
    if args.pdf is None and args.xml is None and args.text is None:
        raise SystemExit("需要 --pdf、--xml、--text 之一，或 --reclassify")
    if not args.emit_only:
        require_enabled(args.source)
    blocks, origin = load_blocks(args)
    entries, mode, posts = entries_from_blocks(blocks)
    if not entries:
        print(f"没有拆出条目：{origin}", file=sys.stderr)
        return 1
    out = args.out or default_out()
    written = write_markdown(entries, out)
    payload = report_payload(entries, source=args.source, origin=origin, mode=mode, posts=posts)
    report_path = args.report or (out / "report.json")
    write_report(payload, report_path)
    print(render_report(payload))
    print(f"\nMarkdown: {out}（{len(written)} 篇）")
    print(f"报告: {report_path}")
    if args.emit_only:
        return 0
    if not written:
        print("没有可导入的条目。")
        return 0
    from researchlib.ingest.markdown_dir import main as markdown_main

    return markdown_main(["--source", args.source, "--input", str(out)])


if __name__ == "__main__":
    sys.exit(main())
