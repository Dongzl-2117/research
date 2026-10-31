"""Stratechery 每日更新开头结尾的套话：检索文字、段落块、单看原文时不要。"""

from researchlib.boilerplate import search_text
from researchlib.chunks import make_chunks
from researchlib.housekeeping import is_signoff, strip_housekeeping

# 2023 年以后的样子：开头有收听入口、问候、作者写给读者的话，结尾有播客、转发须知和道谢。
UPDATE_2023 = """# Widget Earnings, Gadget Strategy

**Listen to this post:**

Good morning,

A quick correction to yesterday's Update: the widget numbers I cited
were for the second quarter, not the third.

On to the Update.

#### Widget Earnings

Widget Corp reported a strong quarter, and the interesting part is how much of it came from subscriptions.

This is the second paragraph of real analysis about the gadget strategy.

This Update will be available as a podcast later today. To receive it in your podcast player,
[visit Stratechery](https://stratechery.example/member).

The Stratechery Update is intended for a single recipient, but occasional forwarding is totally fine!
If you would like to order multiple subscriptions for your team with a group discount
(minimum 5), please contact me directly.

Thanks for being a subscriber, and have a great day!

---
"""

# 2014 年的结尾。
UPDATE_2014 = """Good morning,

On to the update:

#### Thoughts on Gadgets

The gadget market is changing faster than most people expected.

Thanks for being a supporter. If you enjoy these Daily Updates and find them useful, please
[send a friend here](https://stratechery.example/membership) to sign up as well!
In addition, if you would like to order multiple copies for your team, please contact me directly.

Have a great day.
"""

# 2026 年的网页导出：开头一行导语，链接被拆成几行，页脚多一串播客列表。
WEB_2026 = """The new gadget is official, and the question is how big the market is.

Listen to this Update in your podcast player

Good morning,

On
yesterday's episode of Example Show
, the hosts discussed the gadget launch.

On to the Update:

The gadget launch tells us a lot about where the company is heading next.

This Update will be available as a podcast later today. To receive it in your podcast player,
visit Stratechery
.

The Stratechery Update is intended for a single recipient, but occasional forwarding is totally fine!

Thanks for being a subscriber, and have a great day!

Add to your podcast player:
Stratechery
|
Sharp Tech
|
Dithering
"""

INTERVIEW = """**Listen to this post:**

Good morning,

Today's Daily Update Interview is with Jane Doe, an analyst who covers widgets.

As a reminder, all Stratechery content, including interviews, is available as a podcast;
click the link at the top of this email to add Stratechery to your podcast player.

On to the Interview:

**Jane Doe, welcome to Stratechery.**

JD: Thanks for having me, good morning.

This Daily Update Interview is also available as a podcast. To receive it in your podcast player,
[visit Stratechery](https://stratechery.example/member).

The Daily Update is intended for a single recipient, but occasional forwarding is totally fine!
If you would like to order multiple subscriptions for your team with a group discount (minimum 5),
please contact me directly.

Thanks for being a supporter, and have a great day!
"""


def test_update_opening_and_signoff_are_dropped_but_the_note_stays():
    out = strip_housekeeping(UPDATE_2023)
    for gone in ("Listen to this post", "Good morning", "On to the Update", "podcast later today",
                 "single recipient", "group discount", "have a great day"):
        assert gone not in out, gone
    # 作者写给读者的更正、正文、正文里的 subscriptions 都在。
    assert "A quick correction to yesterday's Update" in out
    assert "came from subscriptions." in out
    assert out.startswith("# Widget Earnings, Gadget Strategy\n\nA quick correction")
    assert out.endswith("real analysis about the gadget strategy.\n")


def test_older_and_newer_exports():
    old = strip_housekeeping(UPDATE_2014)
    assert old == "#### Thoughts on Gadgets\n\nThe gadget market is changing faster than most people expected.\n"
    web = strip_housekeeping(WEB_2026)
    assert web.startswith("The new gadget is official")
    assert "yesterday's episode of Example Show" in web
    for gone in ("Listen to this Update", "Good morning", "On to the Update", "visit Stratechery",
                 "single recipient", "great day", "Add to your podcast player", "Dithering"):
        assert gone not in web, gone
    assert web.rstrip().endswith("where the company is heading next.")


def test_interview_reminder_and_transcript():
    out = strip_housekeeping(INTERVIEW)
    assert out.startswith("Today's Daily Update Interview is with Jane Doe")
    assert "As a reminder" not in out and "On to the Interview" not in out
    assert "JD: Thanks for having me, good morning." in out
    assert "also available as a podcast" not in out and "supporter" not in out


def test_lookalikes_stay():
    # 正文中间的「Good morning,」、句子里带的道谢、套话后面还有正文的，都不动。
    body = "\n\n".join(
        [
            "First paragraph about widgets.",
            "Second paragraph.",
            "Third paragraph.",
            "Fourth paragraph.",
            "Good morning,",
            "Thanks for being a subscriber, and have a great day!",
            "The last word is about gadgets, so have a great day thinking about them.",
        ]
    )
    assert strip_housekeeping(body) == body
    # 中文和没有这些句子的英文原样返回，同一个对象。
    chinese = "方伟：好的生意要看现金流。\n\n祝大家周末愉快。"
    assert strip_housekeeping(chinese) is chinese
    fenced = "```\nGood morning,\n```\n\nHave a great day!\n\n```\nThanks for being a subscriber.\n```"
    assert strip_housekeeping(fenced) == fenced


def test_frontmatter_kept():
    front = '---\nid: "2016/widget"\ntitle: "Widget"\n---\n'
    md = front + "\nGood morning,\n\nOn to the update:\n\nBody text here.\n\nHave a great day!\n"
    assert strip_housekeeping(md) == front + "Body text here.\n"


def test_signoff_sentences():
    assert is_signoff("Have a great day!")
    assert is_signoff("Thanks for being a supporter.")
    assert is_signoff("Stratechery gift subscriptions are perfect for Christmas and are available here.")
    assert not is_signoff("Have a great day, and buy more widgets.")
    assert not is_signoff("")


def test_search_text_and_chunks_skip_it():
    text = search_text(UPDATE_2023, "Widget Earnings, Gadget Strategy")
    assert "single recipient" not in text and "Good morning" not in text
    assert "came from subscriptions" in text and "A quick correction" in text
    chunks = make_chunks("Widget Earnings, Gadget Strategy", UPDATE_2023)
    joined = "\n".join(body for _heading, body in chunks)
    assert "group discount" not in joined and "On to the Update" not in joined
    assert "came from subscriptions" in joined
