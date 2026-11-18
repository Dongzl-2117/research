"""公司判断依赖的正文与图片文字版本。SQL 中 a 为文章、v 为判断。"""

from researchlib.ocr import SHORT_BODY_CHARS

# 旧判断使用过的识图被更正或撤回时失效。首次补识图时，保留原来长正文已足以形成的判断。
OCR_STALE_SQL = (
    "(v.ocr_hash IS DISTINCT FROM a.ocr_hash AND (v.ocr_hash <> ''"
    f" OR (a.ocr_hash <> '' AND (btrim(v.judgment) = '' OR length(a.body_text) < {int(SHORT_BODY_CHARS)}))))"
)
CURRENT_INPUT_SQL = f"(v.content_hash = a.content_hash AND NOT {OCR_STALE_SQL})"
