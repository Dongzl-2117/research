"""图片按内容哈希落在 DATA_DIR/images 下，数据库只存相对路径。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_image_file(data_dir: Path, source_id: str, src: Path) -> tuple[str, str, bool]:
    digest = sha256_file(src)
    ext = src.suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".img"
    relative = f"{source_id}/{digest[:2]}/{digest}{ext}"
    dest = data_dir / "images" / relative
    created = False
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        created = True
    return digest, relative, created


def sync_article_images(
    conn,
    *,
    article_id: int,
    source_id: str,
    data_dir: Path,
    refs: list[tuple[str, Path | None]],
) -> tuple[int, int, int]:
    """返回 (新保存文件数, 挂上的引用数, 缺失引用数)。"""
    linked_rows: list[tuple[str, str, str]] = []
    stored = 0
    missing = 0
    for original, path in refs:
        if path is None or not path.is_file():
            missing += 1
            continue
        if path.suffix.lower() not in IMAGE_EXTS:
            missing += 1
            continue
        digest, relative, created = store_image_file(data_dir, source_id, path)
        if created:
            stored += 1
        linked_rows.append((original, relative, digest))
    existing = {
        (row["original_ref"], row["content_hash"])
        for row in conn.execute(
            """
            SELECT original_ref, content_hash
            FROM article_images
            WHERE article_id = %s
            """,
            (article_id,),
        ).fetchall()
    }
    new_set = {(original, digest) for original, _relative, digest in linked_rows}
    if existing == new_set and len(existing) == len(linked_rows):
        return stored, len(linked_rows), missing
    conn.execute("DELETE FROM article_images WHERE article_id = %s", (article_id,))
    if linked_rows:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO article_images
                  (article_id, source_id, original_ref, storage_path, content_hash)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (article_id, source_id, original, relative, digest)
                    for original, relative, digest in linked_rows
                ],
            )
    return stored, len(linked_rows), missing
