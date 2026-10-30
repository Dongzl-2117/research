"""知识星球的展示标题（方伟星球、乘风破浪，以及以后接进来的 kind: zsxq 来源）。

星球导出经常没有标题，或者把第一句截成「……」。卡片、文章页和重新导入时用这里的规则：

- 正文里有完整的第一句（问句也算）就用它，去掉句号，一般不超过 40 字。
- 库内标题去掉省略号后还是一句可用的话，优先于摘要，也优先于图片里的字。
- 复述摘要不能当标题。句子里出现「正文几乎」「文中未给」「作者开篇判断」，或「没有段永平本人的话」，就丢掉。
- 摘要和 flash 起的题常带转述的引子（「作者认为」「方伟判断，」「作者以国债利率为背景，主张」），
  卡片上本来就写着作者，引子去掉，留下判断本身。去掉后不够一句话的不动。
- 图片里的字只在标题是「图片」、空标题，或去掉省略号后没有可用原句时才用，不用无关的配售截图。
- 还不够，就用日期加几个字。
- 标题不能空着，不能以「…」或「...」开头或结尾，也不用「无标题」「图片」这类占位（带引号、书名号同样算）。
  「下图」「如下：」这种引出图片的垫话也算占位。整行只是占位的（导出的标题行「「图片」」）不拿来抽句子，
  不然会和下一句粘成「「图片」 补充一句……」；已经粘上的重取。
- 星球接口给的标题只是正文开头的十几个字，不带省略号。正文从这几个字开始、又没在这里断句，就补全成那一句。
  接口先把开头几行用空格接起来再截，标题会跨行，这时把这几行接起来补全。
- 标题以省略号收尾时，先去掉省略号再和正文对齐。正文里那一句不超过 40 字就整句用上；
  正文也是半截时，收到上一个逗号，不把「有两」这种没写完的半截留在标题上。
- 归档头（「- 作者：… - 时间：…」）混进标题的，重新取。
- 问答帖导出的小标题「提问（网友 → 方伟Rey）」「回答（方伟Rey）」不是帖子的话，括号里是网友的昵称：
  抽句子时当成断句，不和「问：…」那行、下面的提问拼成一句。已经拼进标题的（10315）重新取。
- 公司名/话题名加冒号的壳标题（「泡泡玛特：」「西部矿业：」）也要补。
  完整分句以冒号收尾（「有两点感触很深：」）不是这种壳。
- 评论列表的界面残屑要修：「评论（1）」「评论(1)」，「 - 流年（2026-10-28 09」这种名字加钟点，
  以及重复的「话题：」壳贴在评论头上。前面还剩一句可用的话就切掉残屑；只剩壳才起题。
  正文里只是提到「评论」的不算。
- 明显错字走 textutil.fix_obvious_typos（与 present_text.polish_display_text 同一张表），
  不在这里另维护，不改观点和口语。
- 引号、书名号、括号配不上对的（截出来的句子只剩前引号「“估值越低，…越有利」）要补：
  只差这一个符号就去掉它，原标题照用；从正文收出来的句子里落单的符号也去掉。
  开头「1）」「一）」这种编号的半个括号本来就单用，不算。

读者帖的「读者帖」标在作者行，不写进标题。
"""

from __future__ import annotations

import re

from researchlib.boilerplate import strip_meta
from researchlib.textutil import MARK_PAIRS, argumentative_excerpt, balance_marks, fix_obvious_typos, lonely_marks

FANGWEI_ZSXQ = "fangwei-zsxq"
TITLE_LIMIT = 40

_ELLIPSIS_END = re.compile(
    r"(?:(?:…+|⋯+|‥+|\.{3,}|。{2,}|．{3,})[\s）)】\]」』\"'“”‘’》>]*)+$"
)
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
# 问答导出的小标题「提问（网友 → 方伟Rey）」「回答（方伟Rey）」：括号里是网友的昵称，不是帖子的话。
# 抽句子时当成断句，不和上一行的「问：…」、下一行的提问拼成一句；已经拼进标题的要重取。
# 检索文字（body_text）里这两行只剩「提问」「回答」，单独成行的也一样。
_QA_LINE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?(?:提问|回答)[ \t]*(?:[（(][^（）()\n]+[）)])?[ \t]*$", re.M)
_QA_ASKER = re.compile(r"提问\s*[（(][^（）()]*(?:→|->)[^（）()]*[）)]")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`]+")
# 转述的引子：「作者认为」「方伟判断，」「作者以国债利率为背景，主张」「作者借段永平的话判断，」。
# 「方伟说过的」「作者认为的」是句子本身，不是引子。
_NARRATOR_LEAD = (
    r"(?:方伟|作者|星主|笔者)"
    r"(?:(?:以|借|从)[^，,。；;！？!?]{1,30}?(?:[，,]\s*)?)?"
    r"(?:认为|觉得|判断|指出|提到|说|表示|写道|主张|强调)(?![过的了得])"
    r"[，,：:\s]*"
)
_NARRATOR = re.compile("^" + _NARRATOR_LEAD)
_LEAD = re.compile(
    r"^(?:"
    + _NARRATOR_LEAD
    + r"|这篇(?:文章|短帖)?(?:认为|在讲|讨论|说的是|写的是)[，,：:\s]*"
    r"|(?:读者问|作者答|作者补充|方伟|网友)\s*[：:]\s*"
    # 「2026年9月1日，瑞幸咖啡宣布……」开头的日期是电头，卡片上本来就有日期。不去掉的话，
    # 句子太长要在逗号处收时，标题只剩「2026年9月1日」（乘风破浪 1273）。
    r"|(?:(?:20\d{2}年)?\d{1,2}月\d{1,2}日|20\d{2}[-./]\d{1,2}[-./]\d{1,2})\s*[，,]\s*"
    r")"
)
_SPAM = re.compile(r"扫码|二维码|长按识别|加微信|优惠券")
_FILLER = re.compile(
    r"^(?:大家好|各位好|早上好|晚上好|分享一下|转一下|如题|同上|呵呵+|哈哈+|嗯+|这个|就是|如下|见图|看图|上图|下图)"
    r"[，,。！？!?：:\s]*$"
)
_DANGLING = re.compile(r"[，,；;]\s*([^，,；;]{0,3})$")
_CJK = re.compile(r"[一-鿿]")
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")
_PLACEHOLDER = frozenset({"无标题", "未命名", "无题", "暂无标题", "图片", "见图", "短帖"})
# 「「图片」 虽然不体现在资产负债表……」：导出的占位标题行和下一句粘成了一个标题（23834）。
_PLACEHOLDER_LEAD = re.compile(r"^[「『“\"]\s*(?:图片|见图|无标题|未命名|无题|暂无标题)\s*[」』”\"]\s+\S")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}(?:\s+短帖)?")
_BREAKS = "，,；;、：:"
# 省略号在开头、结尾或句子中间，都是截断，不是一句完整的话。
_CUT = re.compile(r"…+|⋯+|‥+|\.{3,}|．{3,}|。{2,}")
_EDGE_CUT = re.compile(
    r"^(?:\s*(?:…+|⋯+|‥+|\.{3,}|．{3,}|。{2,}))+"
    r"|(?:(?:…+|⋯+|‥+|\.{3,}|．{3,}|。{2,})[\s）)】\]」』\"'“”‘’》>]*)+$"
)
# 只认开头的省略号。正文同一行「前半...后半」要把后半接回去，不能整行跳过。
_LEADING_CUT = re.compile(r"^(?:…+|⋯+|‥+|\.{3,}|．{3,}|。{2,})+")
_DATE_FALLBACK = re.compile(r"^(?:\d{4}-\d{2}-\d{2}\s+)?短帖$")
# 以前入库补标题时读到了归档头那几行，标题成了「「图片」 - 作者：方伟Rey - 时间：2026-10-19 18」。
_ARCHIVE_META = re.compile(r"(?:^|\s)-\s*(?:作者|时间|类型|topic_id)\s*[：:]")
_SENTENCE_STOP = "。！？!?；;"
_SPACES = re.compile(r"\s+")
_MISSING_NOTE = re.compile(r"正文(?:为空|没有内容|没有实质|缺失)|标题仅为|只是一张图|没有可摘的正文")
# 摘要在复述「这篇写了什么、没写什么」，不是帖子自己的那句话。
_NARRATION = re.compile(
    r"正文几乎|文中未给|作者开篇判断|没有(?:段永平|作者|他|星主)?本人的话|没有段永平的话"
)
# 识图之前写成的空壳：标题和导读是同一句「没有可读文字 / 正文为空」。
# 「正文为空洞…」这种真标题不要算进去。
_EMPTY_SHELL = re.compile(
    r"没有可读文字"
    r"|无法提炼"
    r"|没有可供概括"
    r"|没有任何(?:实质)?内容"
    r"|正文除标题"
    r"|除标题[^。\n]{0,40}外没有"
    r"|(?:本文|原文)?正文为空(?!洞)"
)
_WRAP = re.compile(r"[\s（）()【】\[\]〔〕〈〉《》<>「」『』“”‘’\"'`＂＇。！？!?…·]+")
_STOP = frozenset("的是了在和与也就都而及或被把对从到这那你我他她它中")
# 「泡泡玛特：」「西部矿业：」：整标题以中文/ASCII 冒号收尾的壳。
_SHELL_COLON = re.compile(r"^.+[：:]\s*$")
# 评论列表粘进标题。分隔符之后才算界面，避免「在评论（1）里」这种正文。
# 名字后面要有钟点，或者日期被截在字符串末尾；「（2026-10-28）」只有日期且括号已闭上，不算。
_UI_EDGE = r"\s：:|｜·•，,。！？!?#）)"
_UI_BEFORE = rf"(?:^|(?<=[{_UI_EDGE}]))"
_COMMENT_COUNT = re.compile(
    _UI_BEFORE + r"#{0,6}\s*评论\s*[（(]\s*\d{1,4}\s*[）)]"
    r"|评论\s*[（(]\s*\d{1,4}\s*[）)](?=\s*[-–—－]\s*[0-9A-Za-z\u4e00-\u9fff])"
)
_COMMENT_STAMP = (
    r"(?:20\d{2}(?:-\d{1,2}){0,2}(?:[ T]\d{1,2}(?:[:：]\d{2}){0,2})?\s*$"
    r"|20\d{2}-\d{1,2}-\d{1,2}[ T]\d{1,2}(?:[:：]\d{2}){0,2}\s*[)）])"
)
_COMMENT_AUTHOR = re.compile(
    _UI_BEFORE + r"[-–—－]\s*(?:\*\*|__)?\s*"
    r"[0-9A-Za-z\u4e00-\u9fff·._]{1,16}\s*(?:\*\*|__)?\s*"
    r"[（(]\s*" + _COMMENT_STAMP
)
# 「泡泡玛特： 泡泡玛特：」重复的话题壳。后面还粘着评论头时，不把重复本身当成一句标题。
_DUP_LEAD = re.compile(r"^(.{1,16}?)[：:]\s*(?:\1[：:]\s*)+")


def repairs_source(source_id: str, kind: str = "") -> bool:
    """标题按这里的规则补：方伟星球，以及别的 kind 是 zsxq 的来源（乘风破浪）。

    识图后的标题重排（followups）只传 source_id，仍只排方伟星球。
    """
    return source_id == FANGWEI_ZSXQ or kind == "zsxq"


def is_empty_shell(title: str) -> bool:
    """导读在正文还是空的时候写成的元标题。

    24012 的库内标题就是这一句：正文除标题「图片」外没有可读文字，无法提炼作者的核心判断及其依据。
    旧的占位表和「正文为空」正则对不上「没有可读文字」，回写会把它当成已经起好的题。
    """
    return _EMPTY_SHELL.search(title or "") is not None


def _comment_list_ui(title: str) -> bool:
    """标题里粘了评论列表的界面：``评论（1）`` / ``评论(1)``，或 `` - 流年（2026-10-28``。"""
    text = title or ""
    return _COMMENT_COUNT.search(text) is not None or _COMMENT_AUTHOR.search(text) is not None


def _comment_ui_stem(title: str) -> str | None:
    """残屑前面的那段。没有残屑时返回 None，残屑就在开头时返回空串。"""
    text = title or ""
    starts: list[int] = []
    for pattern in (_COMMENT_COUNT, _COMMENT_AUTHOR):
        match = pattern.search(text)
        if match:
            starts.append(match.start())
    if not starts:
        return None
    return text[: min(starts)]


def title_needs_repair(title: str, body: str = "") -> bool:
    """空标题、占位（含「图片」这种带引号的）、复述套话、转述的引子、空壳导读、纯话题号、带省略号，
    公司名/话题名冒号壳，明显错字，归档头，评论列表的界面残屑，问答导出的「提问（网友 → 方伟Rey）」，
    或引号、括号落单。

    句子里只是提到「评论」的不算。给了正文时，标题是正文第一句开头截下的半句也算（见 cut_from_body）。
    """
    text = (title or "").strip()
    if _comment_list_ui(text):
        return True
    if not text or _placeholder(text) or _narration(text) or is_empty_shell(text) or _EDGE_CUT.search(text):
        return True
    if strip_narrator(text) != text:
        return True
    if _QA_ASKER.search(text):
        return True
    if _PLACEHOLDER_LEAD.match(text):
        return True
    if _shell_colon(text):
        return True
    if polish_title_typos(text) != text:
        return True
    if re.fullmatch(r"\d{5,}", text):
        return True
    if _DATE_ONLY.fullmatch(text):
        return True
    if _ARCHIVE_META.search(text):
        return True
    if lonely_marks(text):
        return True
    return bool(body) and cut_from_body(text, body)


def title_needs_ai(title: str) -> bool:
    """空/占位/复述套话/空壳导读/省略号截断/冒号壳/归档头，以及切不干净的评论残屑，才打 flash。

    残屑前面还剩一句可用的标题时，只把残屑切掉，不打 flash。
    星球接口半句截断（对着正文能补全的）仍走确定性规则，避免存量几千篇全打 LLM。
    只是错字也只抛光，不起题。引号、括号落单只去掉那个符号，也不起题。
    """
    text = (title or "").strip()
    if _comment_list_ui(text):
        return not bool(_clean_comment_stem(text))
    if not text or _placeholder(text) or _narration(text) or is_empty_shell(text) or _EDGE_CUT.search(text):
        return True
    if _shell_colon(text):
        return True
    if re.fullmatch(r"\d{5,}", text):
        return True
    if _DATE_ONLY.fullmatch(text):
        return True
    if _ARCHIVE_META.search(text):
        return True
    return False


def strip_narrator(title: str) -> str:
    """去掉开头转述的引子（「作者认为」「方伟判断，」）。剩下的不够一句话时原样返回。

    不进 title_needs_ai：引子去掉就是能用的标题，不用再打 flash。
    """
    text = (title or "").strip()
    match = _NARRATOR.match(text)
    if not match:
        return text
    rest = text[match.end() :].strip()
    if len(_CJK.findall(rest)) < 4 and len(_MEANINGFUL.findall(rest)) < 8:
        return text
    return rest


def polish_title_typos(title: str) -> str:
    """改正标题里的明显错字。替换表在 textutil.fix_obvious_typos，与展示层共用。"""
    return fix_obvious_typos(title or "")


def _shell_colon(title: str) -> bool:
    """整标题以冒号收尾的壳：「泡泡玛特：」「看数：」。

    中间有逗号或句号的是完整分句（「有两点感触很深：」），不是话题名。
    没有停顿、又长过公司名的，也不当壳。
    """
    text = (title or "").strip()
    if not text or not _SHELL_COLON.match(text):
        return False
    stem = text.rstrip("：:").strip()
    if not stem:
        return True
    if any(char in stem for char in "。！？!?，,；;"):
        return False
    if len(stem) > 16:
        return False
    return len(text) <= TITLE_LIMIT


def _duplicate_topic_shell(text: str) -> bool:
    """「泡泡玛特： 泡泡玛特」或带结尾冒号的重复话题壳。不是「问：怎么看茅台」。"""
    parts = [re.sub(r"\s+", "", part) for part in re.split(r"[：:]", text or "") if part.strip()]
    if len(parts) < 2:
        return False
    head = parts[0]
    if not head or len(head) > 16 or any(char in head for char in "。！？!?，,；;"):
        return False
    return all(part == head for part in parts)


def _topic_shell_label(text: str) -> str:
    """重复的话题壳收成一个「泡泡玛特：」，好让后面的图片文字还能对上公司。不是壳就返回空串。"""
    raw = re.sub(r"\s+", " ", text or "").strip().rstrip(" \t-–—－|｜·•")
    if not raw or not (_duplicate_topic_shell(raw) or _shell_colon(raw)):
        return ""
    part = re.split(r"[：:]", raw, maxsplit=1)[0].strip()
    if not part or len(part) > 16 or any(char in part for char in "。！？!?，,；;"):
        return ""
    return part + "："


def _tidy_clause_colon(text: str) -> str:
    """完整分句末尾的冒号拿掉。话题壳（「泡泡玛特：」）留着，交给补全。"""
    polished = polish_title_typos(text or "")
    if polished.endswith(("：", ":")) and not _shell_colon(polished):
        clipped = polished.rstrip("：:").strip()
        if clipped:
            return clipped
    return polished


def _without_archive_meta(title: str) -> str:
    """混进标题的归档头（「 - 作者：… - 时间：…」）切掉，留下前面的原标题。"""
    match = _ARCHIVE_META.search(title)
    return title[: match.start()].strip() if match else title


def _cut_line(title: str, body: str) -> str:
    """正文里标题截自的那一行。正文开头不是这几个字，或者这几个字后面就断句了，返回空串。

    标题自己带的省略号先去掉再对齐，否则「有两...」对不上正文里的「有两点」。
    星球接口先把开头几行用空格接起来再截，标题会跨行（23954「……： 混元大模型经历了不」）：
    这时返回接起来的那几行。
    """
    head = _SPACES.sub("", _MARKUP.sub("", _strip_ellipsis(title or "")))
    if len(head) < 4:
        return ""
    lines = [line for line in _plain(strip_meta(body or "")).splitlines() if _SPACES.sub("", line)]
    for index, line in enumerate(lines):
        flat = _SPACES.sub("", line)
        if not flat.startswith(head):
            return _spanning_cut(head, lines[index:])
        rest = flat[len(head) :]
        if not rest:
            # 标题自己那一行（标题行和标题一样）：看下一行。
            continue
        peeled = _LEADING_CUT.sub("", rest, count=1).lstrip("）)】]」』\"'“”‘’》> ")
        if peeled != rest:
            # 导出的标题行「前十几个字...」。后面没字就看下一行；同一行还接着后半句就接上。
            if not peeled:
                continue
            if peeled[0] in _SENTENCE_STOP:
                return ""
            return head + peeled
        return "" if rest[0] in _SENTENCE_STOP else line
    return ""


def _spanning_cut(head: str, lines: list[str]) -> str:
    """标题跨行：前几行整行都在标题里，最后一行只截了开头。

    线上 23954「……： 混元大模型经历了不」截自说话人那一行和下一行「混元大模型经历了不同阶段，……」；
    13778「美团短剧这个创意 目前全网最」截自两行「美团短剧这个创意」「目前全网最佳」。
    标题正好收在行尾、或者收在句号上的不算截断。
    """
    taken: list[str] = []
    remaining = head
    for line in lines[:4]:
        flat = _SPACES.sub("", line)
        if len(flat) < len(remaining) and remaining.startswith(flat):
            taken.append(line.strip())
            remaining = remaining[len(flat) :]
            continue
        if not taken or not flat.startswith(remaining):
            return ""
        rest = flat[len(remaining) :]
        if not rest or rest[0] in _SENTENCE_STOP:
            return ""
        return " ".join([*taken, line.strip()])
    return ""


def cut_from_body(title: str, body: str) -> bool:
    """标题是正文第一句开头截下的半句：星球接口的标题只截前十几个字，不带省略号。"""
    return bool(_cut_line(title, body))


def _completed_from_body(title: str, body: str) -> str:
    """把截断的标题补成它截自的那一句；太长时和别处一样收在逗号处。"""
    line = _cut_line(title, body)
    if not line:
        return ""
    # 问号、叹号和别处一样留着；句号、分号不留。
    sentence = re.match(f"[^{_SENTENCE_STOP}]*[{_SENTENCE_STOP}]?", line).group(0).rstrip("；;")
    fitted = _fit(_prepare(sentence))
    if not fitted or not _ok(fitted) or _contains_cut(fitted):
        return ""
    # 正文那句可能还带着错字。先按共享表抛光，再判断还要不要重抽，
    # 否则「存在运用杠杆」这类替换会把补全挡掉。
    polished = polish_title_typos(fitted)
    if not polished or title_needs_repair(polished):
        return ""
    return polished


def _headline_line(title: str, body: str) -> str:
    """原标题去掉末尾省略号后，在正文里自成一行：那一行就是完整的标题，省略号是导出时加的。

    乘风破浪 1250 的标题是「Temu：值得重视的关税利好 ...」，正文第一行就是「Temu：值得重视的关税利好」。
    不认这一行，标题会换成正文第一句，帖子自己起的标题反而丢了。
    """
    if not _ELLIPSIS_END.search(title or ""):
        return ""
    stem = _strip_ellipsis(title)
    head = _SPACES.sub("", _MARKUP.sub("", stem))
    if len(head) < 4:
        return ""
    if not any(_SPACES.sub("", line) == head for line in _plain(body).splitlines()):
        return ""
    # 和 _salvage_stored 一样，导出粘在末尾的「+」不要。
    fitted = _fit(_prepare(stem).rstrip("+＋").strip())
    if not fitted or not _ok(fitted) or title_needs_repair(fitted):
        return ""
    return fitted


def zsxq_display_title(
    title: str,
    *,
    body: str = "",
    summary: str = "",
    ocr: str = "",
    when: str = "",
) -> str:
    """给卡片和文章页用的标题。原标题能用就原样返回。"""
    stored = _without_archive_meta((title or "").strip())
    # 正文开头的归档头（作者、时间、点赞数）不是帖子的话，不拿来当标题。
    body = strip_meta(body or "")
    # 评论残屑前面还有一句可用的话：切掉残屑，不用正文换一句。
    clean = _clean_comment_stem(stored)
    if clean:
        return clean
    if _comment_list_ui(stored):
        stored = _topic_shell_label(_comment_ui_stem(stored) or "")
    if stored and not title_needs_repair(stored, body):
        return _tidy_clause_colon(stored)
    # 只是错字：抛光后就能用，不必从正文重抽一句盖掉原标题。
    # 原标题是正文半句时不在这里返回：共享错字表一改前缀，就对不上正文。
    polished = polish_title_typos(stored)
    if (
        polished != stored
        and polished
        and not title_needs_repair(polished)
        and not cut_from_body(stored, body)
    ):
        return polished
    # 只是落单了一个引号、括号：去掉那个符号，原标题照用。原标题是正文半句时照旧补全成整句。
    balanced = balance_marks(polished)
    if (
        balanced != polished
        and _ok(balanced)
        and not title_needs_repair(balanced, body)
        and not cut_from_body(stored, body)
    ):
        return _tidy_clause_colon(balanced)
    completed = _completed_from_body(stored, body)
    if completed:
        return polish_title_typos(completed)
    headline = _headline_line(stored, body)
    if headline:
        return polish_title_typos(headline)
    for sentence in _candidates(body):
        return polish_title_typos(sentence)
    # 去掉省略号后还是一句可用的原标题，优先于摘要和图片里的字。
    stem = _salvage_stored(stored)
    if stem:
        return polish_title_typos(stem)
    for sentence in _candidates(summary):
        return polish_title_typos(sentence)
    if _ocr_allowed(stored):
        anchors = _topic_anchors(stored)
        for sentence in _candidates(ocr):
            # 有帖子主题时，图片里的另一则新闻对不上就不拿来当标题。
            if not anchors or _overlaps(sentence, anchors):
                return polish_title_typos(sentence)
    snippet = _short_snippet(body) or _short_snippet(summary)
    if _ocr_allowed(stored):
        snippet = snippet or _short_snippet(ocr)
    date = _date_prefix(when)
    if date and snippet:
        fitted = _fit(f"{date} {snippet}")
        if fitted:
            return polish_title_typos(fitted)
    if snippet:
        return polish_title_typos(snippet)
    if date:
        return f"{date} 短帖"
    return "短帖"


def stored_zsxq_title(
    title: str,
    body: str,
    *,
    summary: str = "",
    ocr: str = "",
    when: str = "",
) -> str | None:
    """导入和回写时要写进库的标题。规则和页面上的 `zsxq_display_title` 是同一套。

    日期加「短帖」这种兜底不写入：下次打开页面还可以用摘要或图片里的字补。
    原标题本来就能用时返回 None。去掉省略号后还剩一句主干，也会写回去，避免库里继续以「…」结尾。
    """
    raw = (title or "").strip()
    if not title_needs_repair(title, body):
        return None
    # 只是错字：直接抛光写回，不必重抽一句。半句截断仍走下面的补全。
    polished = polish_title_typos(raw)
    if (
        polished != raw
        and polished
        and not title_needs_repair(polished)
        and not cut_from_body(raw, body)
    ):
        return polished
    repaired = zsxq_display_title(
        title or "",
        body=body or "",
        summary=summary or "",
        ocr=ocr or "",
        when=when or "",
    )
    if not repaired or repaired == raw:
        return None
    if title_needs_repair(repaired) or _DATE_FALLBACK.fullmatch(repaired):
        return None
    return repaired


def _drop_cut_stubs(text: str) -> str:
    """去掉「前半句...」这种标题行，免得它和下面的完整句粘成一段，整段因为省略号被丢掉。"""
    lines = (text or "").splitlines()
    flats = [_SPACES.sub("", _plain(line)) for line in lines]
    drop: set[int] = set()
    for index, line in enumerate(lines):
        if not _contains_cut(line):
            continue
        stem = _SPACES.sub("", _strip_ellipsis(_plain(line)))
        if len(stem) < 4:
            continue
        for later in flats[index + 1 : index + 6]:
            if later.startswith(stem) and len(later) > len(stem):
                drop.add(index)
                break
    if not drop:
        return text or ""
    return "\n".join(line for index, line in enumerate(lines) if index not in drop)


def _drop_placeholder_lines(text: str) -> str:
    """整行只是「「图片」」「下图」这种占位的不拿来抽句子：导出的标题行会和下一句粘成「「图片」 补充一句……」。"""
    lines = (text or "").splitlines()
    return "\n".join(line for line in lines if not _placeholder(_HEADING.sub("", line)))


def _candidates(text: str) -> list[str]:
    """从一段文字里按顺序抽出能当标题的完整句。

    带省略号的半截、「正文为空」和复述摘要（正文几乎、文中未给、作者开篇判断、没有本人的话）丢掉。
    """
    found: list[str] = []
    for raw in _iter_raw(_drop_placeholder_lines(_drop_cut_stubs(text))):
        if _spammy(raw) or _contains_cut(raw) or _missing_note(raw) or _narration(raw):
            continue
        prepared = _prepare(raw)
        if _dangling(prepared):
            prepared = _clip_dangling(prepared)
        if (
            not prepared
            or _contains_cut(prepared)
            or _missing_note(prepared)
            or _narration(prepared)
            or not _ok(prepared)
        ):
            continue
        fitted = _fit(prepared)
        if (
            not fitted
            or _contains_cut(fitted)
            or _placeholder(fitted)
            or _narration(fitted)
            or _comment_list_ui(fitted)
        ):
            continue
        found.append(fitted)
    return found


def _iter_raw(text: str):
    remaining = _plain(text)
    seen: set[str] = set()
    for _ in range(4):
        raw = argumentative_excerpt(remaining, limit=800)
        if not raw or raw in seen:
            return
        seen.add(raw)
        yield raw
        if raw not in remaining:
            return
        remaining = remaining.replace(raw, "\n", 1)


def _plain(text: str) -> str:
    cleaned = _QA_LINE.sub("。", text or "")
    cleaned = _IMAGE.sub(" ", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _MARKUP.sub("", cleaned)
    return cleaned


def _prepare(sentence: str) -> str:
    current = sentence.strip()
    for _ in range(2):
        match = _LEAD.match(current)
        if not match:
            break
        rest = current[match.end() :].strip()
        if len(_CJK.findall(rest)) < 4 and len(_MEANINGFUL.findall(rest)) < 8:
            break
        current = rest
    current = _strip_ellipsis(current).strip().rstrip("。.")
    if _comment_list_ui(current):
        current = _excise_comment_ui(current)
        if not current:
            return ""
    if current.endswith(("：", ":")) and not _shell_colon(current):
        current = current.rstrip("：:").strip()
    return re.sub(r"\s+", " ", current).strip()


def _ocr_allowed(title: str) -> bool:
    """图片里的字补给空标题、占位、被省略号截断、以及没有汉字的标题。

    正文和摘要都抽不出一句时才用到。原标题去掉省略号后还剩主干的，不会走到这里。
    """
    text = (title or "").strip()
    if title_needs_repair(text):
        return True
    return _CJK.search(text) is None


def _topic_anchors(title: str) -> set[str]:
    """半截标题里的三字片段。占位标题没有主题，图片里的字可以单独当标题。"""
    stem = re.sub(r"[\s+＋]+", "", _strip_ellipsis(title or ""))
    # 占位和复述套话没有帖子主题，图片里的字可以单独当标题。
    if not stem or _placeholder(stem) or _narration(stem):
        return set()
    chars = "".join(_CJK.findall(stem))
    if len(chars) < 4:
        return set()
    anchors = set()
    for index in range(len(chars) - 2):
        gram = chars[index : index + 3]
        if sum(char in _STOP for char in gram) >= 2:
            continue
        anchors.add(gram)
    return anchors


def _overlaps(sentence: str, anchors: set[str]) -> bool:
    if not anchors:
        return True
    hay = "".join(_CJK.findall(sentence))
    return any(anchor in hay for anchor in anchors)


def _clip_ellipsis_tail(title: str, stem: str) -> str:
    """省略号说明后面没写完。半截收到上一个逗号，不留「有两点感」这种半个词。

    没有逗号（「护城河在哪里…」）就留去掉省略号的主干。本来就没有省略号的，仍只剪逗号后不到四个字的尾巴。
    """
    text = (stem or "").strip()
    if not text:
        return ""
    if not _EDGE_CUT.search(title or ""):
        return _clip_dangling(text) if _dangling(text) else text
    cut = max(text.rfind(char) for char in "，,；;")
    if cut >= 8:
        return text[:cut].strip()
    return _clip_dangling(text) if _dangling(text) else text


def _salvage_stored(title: str) -> str:
    """原标题去掉末尾省略号后，还剩一句不带截断符的话，才拿来兜底。"""
    if _placeholder(title):
        return ""
    stem = _prepare(_strip_ellipsis(title)).rstrip("+＋").strip()
    if _contains_cut(stem):
        return ""
    clipped = polish_title_typos(_fit(_clip_ellipsis_tail(title, stem)))
    if clipped and _ok(clipped) and not _contains_cut(clipped) and not title_needs_repair(clipped):
        return clipped
    return ""


def _short_snippet(text: str) -> str:
    if _narration(text):
        return ""
    raw = argumentative_excerpt(_plain(_drop_placeholder_lines(text)), limit=80)
    if _narration(raw):
        return ""
    piece = _prepare(raw)
    piece = _clip_dangling(piece) if _dangling(piece) else piece
    if (
        not piece
        or _comment_list_ui(piece)
        or _placeholder(piece)
        or _spammy(piece)
        or _FILLER.match(piece)
        or _contains_cut(raw)
        or _contains_cut(piece)
        or _narration(piece)
    ):
        return ""
    if len(_CJK.findall(piece)) < 2 and len(_MEANINGFUL.findall(piece)) < 4:
        return ""
    fitted = _fit(piece)
    if not fitted or _contains_cut(fitted):
        return ""
    return balance_marks(fitted[:18])


def _fit(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = _strip_ellipsis(cleaned).strip().rstrip("。.")
    cleaned = cleaned.strip("，,；;、 ")
    if not cleaned:
        return ""
    if len(cleaned) <= TITLE_LIMIT:
        return balance_marks(_strip_ellipsis(cleaned).strip())
    window = cleaned[:TITLE_LIMIT]
    # 顿号是列举，收在那里会剩下半截列表（14885「……护城河；资本开支」）。有逗号、分号就收在那里。
    cut = _last_break(window, "，,；;：:")
    if cut < 8:
        cut = _last_break(window, "、")
    if cut >= 8:
        fitted = window[:cut].strip()
    else:
        fitted = window.rstrip(_BREAKS + " ").strip()
    # 收在逗号处时，后引号常常落在后半句里。
    return balance_marks(_strip_ellipsis(fitted).strip())


def _last_break(window: str, marks: str) -> int:
    """window 里第 8 个字以后最后一个停顿的位置，没有就是 -1。"""
    cut = -1
    for index, char in enumerate(window):
        if char in marks and index >= 8:
            cut = index
    return cut


def unwrap_title(text: str) -> str:
    """去掉包住整行的引号、书名号，再去掉落单的符号。

    「《穷查理宝典》读后感」开头的书名号配得上对，是标题的一部分，留着。
    """
    current = (text or "").strip().strip("\"'‘’ ")
    while len(current) >= 2 and MARK_PAIRS.get(current[0]) == current[-1] and not lonely_marks(current[1:-1]):
        current = current[1:-1].strip().strip("\"'‘’ ")
    return balance_marks(current)


def _ok(text: str) -> bool:
    if (
        not text
        or _comment_list_ui(text)
        or _placeholder(text)
        or _narration(text)
        or _spammy(text)
        or _FILLER.match(text)
    ):
        return False
    if _shell_colon(text):
        return False
    if _contains_cut(text):
        return False
    if re.fullmatch(r"[\d\s:：./\-年月日时分秒]+", text):
        return False
    cjk = len(_CJK.findall(text))
    meaningful = len(_MEANINGFUL.findall(text))
    return cjk >= 4 or meaningful >= 8


def _dangling(text: str) -> bool:
    return _DANGLING.search(text or "") is not None


def _clip_dangling(text: str) -> str:
    match = _DANGLING.search(text or "")
    if match and match.start() >= 4:
        return text[: match.start()].strip()
    return (text or "").strip()


def _usable_comment_stem(prefix: str) -> str:
    """残屑前面的这段能当标题才返回。重复的话题壳、空壳、半截都不要。"""
    stem = re.sub(r"\s+", " ", prefix or "").strip().rstrip(" \t-–—－|｜·•")
    if not stem or _duplicate_topic_shell(stem) or _shell_colon(stem) or _comment_list_ui(stem):
        return ""
    if _dangling(stem):
        stem = _clip_dangling(stem)
    fitted = _fit(polish_title_typos(stem))
    if (
        not fitted
        or _comment_list_ui(fitted)
        or _duplicate_topic_shell(fitted)
        or _shell_colon(fitted)
        or not _ok(fitted)
        or is_empty_shell(fitted)
        or title_needs_repair(fitted)
    ):
        return ""
    return fitted


def _clean_comment_stem(title: str) -> str:
    """标题带评论残屑、且前面还剩一句可用的话时，返回那一句。否则返回空串。"""
    if not _comment_list_ui(title):
        return ""
    return _usable_comment_stem(_comment_ui_stem(title) or "")


def _excise_comment_ui(text: str) -> str:
    """从一句里拿掉评论列表残屑。

    前面已经是一句可用的标题，就只留前面。否则删掉残屑，重复的话题壳也拿掉，留下后面的句子。
    """
    current = text or ""
    for _ in range(3):
        if not _comment_list_ui(current):
            return current.strip()
        usable = _usable_comment_stem(_comment_ui_stem(current) or "")
        if usable:
            return usable
        cleaned = current
        for pattern in (_COMMENT_AUTHOR, _COMMENT_COUNT):
            cleaned = pattern.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(" \t-–—－|｜·•")
        match = _DUP_LEAD.match(cleaned)
        if match:
            cleaned = cleaned[match.end() :].strip()
        if cleaned == current.strip():
            return ""
        current = cleaned
    return "" if _comment_list_ui(current) else current.strip()


def _placeholder(text: str) -> bool:
    """「图片」、『见图』、（无标题）和裸的「图片」都是占位。

    「下图」「如下：」这种引出图片的垫话也是：问答帖按话题号重导时，接口标题取的是回答开头，
    线上 23982 的标题就成了「下图」。
    """
    bare = _WRAP.sub("", text or "")
    return bare in _PLACEHOLDER or _FILLER.match(bare) is not None


def _missing_note(text: str) -> bool:
    return _MISSING_NOTE.search(text or "") is not None or is_empty_shell(text)


def _narration(text: str) -> bool:
    """复述摘要：在说这篇没有什么，不是帖子里的那句判断。"""
    return _NARRATION.search(text or "") is not None


def _contains_cut(text: str) -> bool:
    return _CUT.search(text or "") is not None


def _spammy(text: str) -> bool:
    return _SPAM.search(text or "") is not None


def _strip_ellipsis(text: str) -> str:
    return _ELLIPSIS_END.sub("", text or "").strip()


def _date_prefix(when: str) -> str:
    text = (when or "").strip()
    if not text or text == "日期不详":
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return ""
