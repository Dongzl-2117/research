"""把段落切成博客条目。问答保持一条：读者的问题、段永平的回答、以及回答之后别人的回复。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from researchlib.ingest.duanyongping.companies import companies_in
from researchlib.ingest.duanyongping.extract import (
    Block,
    is_author_text,
    is_comment_break,
    is_date_only,
    is_other_text,
    normalize_date,
)

_PUNCT = re.compile(r"[。！？!?，,；;]")
_AUTHOR_LABEL = re.compile(
    r"^(?:段永平|段总)\s*(?:回复\s*\S{1,24})?\s*(?:(?:→|->)\s*\S{1,24})?\s*"
    r"(?:[（(][^)）]{0,80}[)）])?\s*[:：]\s*"
)


@dataclass
class Post:
    title: str
    date: str
    repost: bool
    blocks: list[Block] = field(default_factory=list)
    page: int = 0


@dataclass
class Entry:
    title: str
    question: str
    author: str
    others: str
    published: str
    kind: str
    companies: list[str]
    keep: bool
    reason: str
    post_title: str
    page: int
    repost: bool = False

    @property
    def external_id(self) -> str:
        blob = "\n".join([self.post_title, self.question, self.author, self.published])
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        return f"dyp-{digest}"

    @property
    def feed(self) -> bool:
        return self.keep and bool(self.published)


def is_repost(title: str) -> bool:
    text = (title or "").strip()
    return text.startswith(("【转载】", "转载", "【引用】", "引用", "转发"))


def _body_size(blocks: list[Block]) -> float:
    sizes = sorted(block.size for block in blocks if block.size)
    if not sizes:
        return 0
    return sizes[len(sizes) // 2]


def _is_title(block: Block, nxt: Block | None, prev: Block | None, body_size: float) -> bool:
    text = block.text.strip()
    if not (2 <= len(text) <= 40):
        return False
    if _PUNCT.search(text):
        return False
    if is_date_only(text) or is_author_text(text) or is_other_text(text) or is_comment_break(text):
        return False
    if body_size and block.size >= body_size * 1.18:
        return True
    if nxt is not None and is_date_only(nxt.text) and len(text) <= 30:
        return True
    page_break = prev is None or prev.page != block.page
    # 新的一页开头、后面紧跟提问或署名的短行是标题。没写日期的博文靠这个切开。
    if (
        page_break
        and nxt is not None
        and len(text) <= 20
        and (is_author_text(nxt.text) or is_other_text(nxt.text) or is_date_only(nxt.text))
    ):
        return True
    if nxt is not None and is_other_text(nxt.text) and len(text) <= 16:
        return True
    return False


def split_posts(blocks: list[Block]) -> list[Post]:
    body_size = _body_size(blocks)
    posts: list[Post] = []
    current: Post | None = None
    index = 0
    while index < len(blocks):
        block = blocks[index]
        nxt = blocks[index + 1] if index + 1 < len(blocks) else None
        prev = blocks[index - 1] if index else None
        if _is_title(block, nxt, prev, body_size):
            if current is not None and (current.blocks or current.title):
                posts.append(current)
            date = ""
            step = 1
            if nxt is not None and is_date_only(nxt.text):
                date = normalize_date(nxt.text)
                step = 2
            current = Post(title=block.text.strip(), date=date, repost=is_repost(block.text), page=block.page)
            index += step
            continue
        if current is None:
            current = Post(title="", date="", repost=False, page=block.page)
        if not is_date_only(block.text):
            current.blocks.append(block)
        elif not current.date:
            current.date = normalize_date(block.text)
        index += 1
    if current is not None and (current.blocks or current.title):
        posts.append(current)
    return posts


def _author_block(block: Block, post: Post, in_thread: bool) -> bool:
    if is_author_text(block.text) or block.bold:
        return True
    # 博文正文（评论区之前、没有别人署名）算他写的。转载的正文不是。
    if post.repost or in_thread:
        return False
    if is_other_text(block.text) or is_comment_break(block.text):
        return False
    return True


def _clean_author(text: str) -> str:
    cleaned = _AUTHOR_LABEL.sub("", text.strip(), count=1)
    return cleaned.strip() or text.strip()


_SPEAKER_PREFIX = re.compile(
    r"^(?:问|网友|博友|读者|Q|q)\S{0,16}?\s*"
    r"(?:\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
    r"(?:\s+\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?)?)?"
    r"\s*[:：]\s*"
)


def _clip(text: str, limit: int = 42) -> str:
    first = (text or "").strip().split("\n", 1)[0]
    flat = re.sub(r"\s+", " ", first).strip()
    flat = _SPEAKER_PREFIX.sub("", flat, count=1)
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _entry_title(post: Post, question: str, author: str) -> str:
    for text in (question, author, post.title):
        title = _clip(text)
        if title and title not in {"评论", "留言", "未命名"}:
            return title
    return post.title or "未命名"


def _make(
    post: Post,
    question: str,
    author: str,
    others: str,
    published: str,
    kind: str,
) -> Entry:
    return Entry(
        title=_entry_title(post, question, author),
        question=question.strip(),
        author=author.strip(),
        others=others.strip(),
        published=published,
        kind=kind,
        companies=[],
        keep=False,
        reason="",
        post_title=post.title,
        page=post.page,
        repost=post.repost,
    )


def _turn_date(blocks: list[Block], post_date: str) -> str:
    for block in blocks:
        found = normalize_date(block.text)
        if found:
            return found
    return post_date


def segment_post(post: Post) -> list[Entry]:
    """一篇博客里：正文是一条；评论区里每一问一答是一条。"""
    entries: list[Entry] = []
    essay: list[str] = []
    question: list[str] = []
    author: list[str] = []
    trailing: list[str] = []
    in_thread = False

    def close_essay() -> None:
        if essay:
            entries.append(_make(post, "", "\n\n".join(essay), "", post.date, "essay"))
            essay.clear()

    def close_qa(others: list[str]) -> None:
        if not author:
            return
        blocks = [Block(text, False, 0, post.page) for text in question + author + others]
        entries.append(
            _make(
                post,
                "\n\n".join(question),
                "\n\n".join(_clean_author(part) for part in author),
                "\n\n".join(others),
                _turn_date(blocks, post.date),
                "qa" if question or others else "essay",
            )
        )
        question.clear()
        author.clear()

    for block in post.blocks:
        if is_comment_break(block.text):
            if not in_thread:
                close_essay()
            in_thread = True
            continue
        if _author_block(block, post, in_thread):
            if trailing:
                close_qa([])
                question.extend(trailing)
                trailing = []
            if essay and not author and not in_thread:
                close_essay()
            if question or in_thread or is_author_text(block.text) or (author and block.bold):
                in_thread = True
                author.append(block.text)
            else:
                essay.append(_clean_author(block.text))
        elif author:
            trailing.append(block.text)
        else:
            if essay and (in_thread or is_other_text(block.text)):
                close_essay()
            if is_other_text(block.text) or in_thread:
                in_thread = True
                question.append(block.text)
            elif post.repost:
                question.append(block.text)
                in_thread = True
            else:
                essay.append(block.text)
    if author:
        close_qa(trailing)
    elif essay:
        close_essay()
    if not entries and post.title:
        entries.append(_make(post, "", "", "", post.date, "essay"))
    return entries


def classify(entry: Entry) -> Entry:
    author = re.sub(r"\s+", "", entry.author)
    if len(author) < 6:
        entry.keep = False
        entry.reason = "没有段永平本人的话"
        entry.companies = []
        return entry
    # 转载里的公司名是别人的文章。只有他本人的话点了名才留。
    scope = entry.author if entry.repost else f"{entry.question}\n{entry.author}"
    real, side = companies_in(scope)
    if real:
        entry.keep = True
        entry.companies = real
        entry.reason = "谈到" + "、".join(real)
        return entry
    entry.keep = False
    entry.companies = []
    entry.reason = "公司只出现在举例里" if side else "没有具体公司"
    return entry


def entries_from_blocks(blocks: list[Block]) -> tuple[list[Entry], str, int]:
    posts = split_posts(blocks)
    mode = "posts" if len([post for post in posts if post.title]) >= 2 else "flat"
    if mode == "flat":
        # 认不出一篇篇博文时，只把粗体和「段永平：」当他的话，避免把整本未加粗的提问都算成他写的。
        flat = Post(title="", date="", repost=True, blocks=blocks, page=blocks[0].page if blocks else 0)
        found = segment_post(flat)
    else:
        found = [entry for post in posts for entry in segment_post(post)]
    return [classify(entry) for entry in found], mode, len(posts)
