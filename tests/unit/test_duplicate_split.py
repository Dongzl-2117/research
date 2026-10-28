"""认重复的切句：英文按句切、段内换行接回去；中文的切法和以前一样。不连库。"""

import re
import textwrap

from researchlib.duplicates import MIN_SENTENCE, _hash, fingerprint, normalize, same_article, sentences

CHINESE = """
## 广告

腾讯的广告加载率还在提升，视频号是新的广告库存，商家的投放在往这里挪。
腾讯把小程序和视频号接起来以后，交易闭环更短；广告主愿意出的价格也更高！

如果视频号的用户时长停止增长，这个判断就要重新看？广告库存的增量会明显放缓。
"""


def _english(seed: str, paragraphs: int = 12) -> str:
    return "\n\n".join(
        " ".join(
            f"In part {index} the {seed} argument turns on sentence {line}, which explains why the bundle "
            f"economics differ for platform number {index * 10 + line}."
            for line in range(5)
        )
        for index in range(paragraphs)
    )


def test_chinese_fingerprints_are_unchanged():
    old_split = re.compile(r"[。！？!?；;\n]+")
    old = {_hash(normalize(piece)) for piece in old_split.split(CHINESE) if len(normalize(piece)) >= MIN_SENTENCE}
    assert fingerprint(CHINESE).sentences == old


def test_english_splits_on_sentence_ends_and_rejoins_wrapped_lines():
    text = (
        'The company said "it shipped on time." Then the reviews\ncame in! Were they good? Mostly.'
        "\n\nA new paragraph starts here and keeps going for a while."
    )
    assert sentences(text) == [
        'The company said "it shipped on time.',
        "Then the reviews came in!",
        "Were they good?",
        "Mostly.",
        "A new paragraph starts here and keeps going for a while.",
    ]


def test_hard_wrapped_copy_is_the_same_article():
    plain = _english("aggregation")
    wrapped = "\n\n".join(textwrap.fill(paragraph, 72) for paragraph in plain.split("\n\n"))
    assert same_article(fingerprint(plain), fingerprint(wrapped))
    # 邮件那份多了页眉页脚，也认得出来。
    mailed = "Subscriber edition, do not forward.\n\n" + wrapped + "\n\nYou are receiving this because you subscribed."
    assert same_article(fingerprint(plain), fingerprint(mailed))
    # 不同的两篇不算。
    assert not same_article(fingerprint(plain), fingerprint(_english("integration")))
