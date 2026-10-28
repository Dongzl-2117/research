"""英文文章的提及深度：按句子比例，不按次数。下面的英文句子都是编的。"""

from researchlib.company_seed import SEEDS
from researchlib.mentions import (
    DEPTH_PASSING,
    DEPTH_SECTION,
    DEPTH_WHOLE,
    CompanyMatcher,
    mentions_for_document,
)


def matcher():
    companies = []
    names = {}
    for index, seed in enumerate(SEEDS, start=1):
        companies.append(
            {
                "id": index,
                "ticker": seed.ticker,
                "name_zh": seed.name_zh,
                "name_en": seed.name_en,
                "aliases": seed.aliases,
            }
        )
        names[seed.name_zh] = index
    return CompanyMatcher(companies), names


_FILLER = [
    "The streaming market has matured over the last few years.",
    "Subscriber growth is slowing in most mature regions.",
    "Pricing power is now the main lever for revenue growth.",
    "Advertising tiers have changed the economics of the business.",
    "Churn remains the key metric to watch going forward.",
    "Content costs are finally stabilizing after a decade of growth.",
    "The company reported results that beat expectations.",
    "Management raised guidance for the full year.",
]


def _paragraphs(count, edit):
    paragraphs = []
    for index in range(count):
        sentences = [_FILLER[(index + offset) % len(_FILLER)] for offset in range(5)]
        edit(index, sentences)
        paragraphs.append(" ".join(sentences))
    return paragraphs


def _depths(found, title, body):
    return {cid: depth for cid, depth, _ in mentions_for_document(found, title, body)}


def _streaming_essay(index, sentences):
    if index == 0:
        sentences[0] = "Netflix reported another strong quarter."
    if index in (4, 13, 22):
        sentences[2] = "Apple took a different path with its bundle."
    if index == 17:
        sentences[1] = "This is the same playbook Apple used with the iPhone."
    if index % 6 == 0:
        sentences[3] = "Netflix is still the market leader in streaming."


def test_english_sentences_split_on_periods_and_rejoin_wraps():
    from researchlib.mentions import latin_sentences

    text = (
        "Apple Inc. said revenue rose. iPhone sales were up 3.5 percent.\n"
        "The U.S. Steel deal, e.g. the second bid,\nstalled. J. Smith agreed.\n\nA new paragraph."
    )
    assert latin_sentences(text) == [
        "Apple Inc. said revenue rose.",
        "iPhone sales were up 3.5 percent.",
        "The U.S. Steel deal, e.g. the second bid, stalled.",
        "J. Smith agreed.",
        "A new paragraph.",
    ]


def test_english_comparisons_stay_passing_and_the_subject_is_whole():
    import textwrap

    found, names = matcher()
    paragraphs = _paragraphs(30, _streaming_essay)
    rows = _depths(found, "The Streaming Endgame", "\n\n".join(paragraphs))
    # 苹果在 150 句里只有 4 句类比：按中文规则会是「全文」。
    assert rows[names["苹果"]] == DEPTH_PASSING
    assert rows[names["奈飞"]] == DEPTH_WHOLE
    # 硬换行的另一份（邮件、RSS）切出来一样。
    wrapped = "\n\n".join(textwrap.fill(paragraph, 72) for paragraph in paragraphs)
    assert _depths(found, "The Streaming Endgame", wrapped) == rows


def test_english_article_about_a_company_is_whole():
    found, names = matcher()

    def edit(index, sentences):
        if index % 2 == 0:
            sentences[1] = "Apple is leaning on services to offset hardware."
        if index % 5 == 0:
            sentences[4] = "The question is whether Apple can keep that margin."

    body = "\n\n".join(_paragraphs(25, edit))
    rows = _depths(found, "Services Forever", body)
    assert rows[names["苹果"]] == DEPTH_WHOLE


def test_english_sections_and_headings():
    found, names = matcher()
    filler = " ".join(_FILLER[:5])
    microsoft = "## Cloud Results\n\n" + "\n\n".join(
        [filler + " Microsoft grew the cloud business again." for _ in range(6)]
    )
    headset = "## The Headset Problem\n\n" + "\n\n".join(
        [
            " ".join(["Apple shipped the headset in February.", _FILLER[1], "Apple has not said how many it sold."])
            for _ in range(5)
        ]
    )
    search = "## Search\n\n" + "\n\n".join([filler for _ in range(6)])
    rows = _depths(found, "Three Things", "\n\n".join([microsoft, headset, search]))
    # 苹果的命中全在一节里：单节，不是全文。
    assert rows[names["苹果"]] == DEPTH_SECTION
    # 只在章节标题里点名、正文说 the company：也是那一节在谈。
    quarter = "## Google's Quarter\n\n" + "\n\n".join([" ".join(_FILLER[3:8]) for _ in range(4)])
    rows = _depths(found, "Three Things", "\n\n".join([search, quarter, search]))
    assert rows[names["谷歌"]] == DEPTH_SECTION


def test_english_examples_do_not_count():
    found, names = matcher()
    paragraphs = _paragraphs(20, lambda index, sentences: None)
    paragraphs[3] = "Platforms such as Apple and Google take a cut. " + _FILLER[1]
    paragraphs[9] = "For example, Apple charges thirty percent. " + _FILLER[3]
    paragraphs[15] = "Unlike Apple, Microsoft never built a phone business. " + _FILLER[4]
    paragraphs[16] = "By the way, Apple says the same."
    rows = _depths(found, "Platform Fees", "\n\n".join(paragraphs))
    assert rows[names["苹果"]] == DEPTH_PASSING
    assert rows[names["谷歌"]] == DEPTH_PASSING


def test_short_english_posts_and_chinese_keep_the_count_rule():
    found, names = matcher()
    rows = _depths(found, "note", "Apple raised prices. Apple fans grumbled. Apple did not care.")
    assert rows[names["苹果"]] == DEPTH_WHOLE
    # 中文文章里夹英文名字，仍按中文规则。
    body = "这篇先写 Apple 的产能。Apple 的客户。Apple 的价格，还有 iPhone 的备货和 Netflix 的订阅。"
    rows = _depths(found, "备忘", body)
    assert rows[names["苹果"]] == DEPTH_WHOLE


def test_rules_version_changes_the_fingerprint(monkeypatch):
    import researchlib.mentions as mentions

    rows = [{"id": 1, "name_zh": "腾讯", "name_en": "Tencent", "ticker": "0700.HK", "aliases": [], "alias_rules": {}}]
    before = mentions.companies_fingerprint(rows)
    monkeypatch.setattr(mentions, "MENTION_RULES_VERSION", mentions.MENTION_RULES_VERSION + "-next")
    assert mentions.companies_fingerprint(rows) != before
