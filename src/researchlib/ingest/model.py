"""导入器之间传递的文章和计数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class ParsedDoc:
    external_id: str
    title: str
    published_at: datetime | None
    body_md: str
    raw_meta: dict
    image_refs: list[tuple[str, Path | None]]
    attachment_refs: list[tuple[str, Path | None]] = field(default_factory=list)


@dataclass
class IngestReport:
    source_id: str
    source_name: str
    archive_files: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0
    db_rows: int = 0
    images_in_archive: int = 0
    images_stored: int = 0
    images_linked: int = 0
    images_missing: int = 0
    # 写入后认出是库里已有的同一篇（另一条路径导入过，或同一作者在别的来源发过）。
    duplicates: int = 0
    # 新正文像删除提示、或比库里短一半以上：没有替换，只把这一版记进 article_revisions。
    held: int = 0
    # 知识星球 INDEX.md 不是话题，导入时从库里去掉。
    removed_index: int = 0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"来源: {self.source_id} ({self.source_name})",
            f"归档文章: {self.archive_files}",
            f"数据库文章: {self.db_rows}",
            f"写入: 新增 {self.inserted}，更新 {self.updated}，未变 {self.unchanged}",
            f"拦下: {self.held}（删稿、截断或受保护投影冲突；保留原文并暂存来稿待核）",
            f"拒绝: {self.rejected}",
            f"重复: {self.duplicates}（库里已有同一篇，不进列表和检索）",
            f"归档图片: {self.images_in_archive}",
            f"新保存图片: {self.images_stored}",
            f"挂上文章的图片: {self.images_linked}",
            f"缺失图片引用: {self.images_missing}",
        ]
        if self.removed_index:
            lines.append(f"去掉目录: {self.removed_index}（INDEX.md 不是话题，不进按天）")
        if self.errors:
            lines.append(f"错误: {len(self.errors)}")
            lines.extend(f"  - {item}" for item in self.errors[:20])
            if len(self.errors) > 20:
                lines.append(f"  - …还有 {len(self.errors) - 20} 条")
        return "\n".join(lines)
