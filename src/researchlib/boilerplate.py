"""正文清洗：归档头、公众号和星球的引流尾巴、广告图。

库里的 body_md 保持导入时的原样（改稿、删稿核对都靠它），清洗只发生在两处：

- 文章页显示正文之前；
- 算 body_text 和段落块之前（检索、摘要、观点读的都是清洗后的文字）。

三条规则，都按同一来源多篇文章的统计来判，不写死某个作者的话术：

1. 归档头：正文开头那几行「- 作者：」「- 时间：」「- sn：」「- 原链接：」「- 原始数据：」
   「- 类型：」「- 点赞 · 评论 · 阅读人数」「- topic_id：」「- rss_link：」，整块去掉。
2. 模板句：同一来源里在很多篇（至少 max(5, 2%) 篇）都出现的整行。文末从第一行模板句起，
   后面几乎全是模板句、引流关键词或几个字的碎行时，整段切掉（招募被拆成一行几个字的也切得到）。
   36 字以上、只是顺带提到引流词的句子（「前段时间被一家企业白嫖了两次」）算正文，不算引流。
   「几乎全是」的容差（尾巴里正文不超过 160 字）只给推销块里换了数字、换了措辞的条目：切口和第一处号召之间
   36 字以上的整段正文留下，切口挪到它后面（老文章的入群码插进了最后几段正文中间，见 _after_last_text）。
   正文中间的模板句只在带引流关键词时去掉；飞书那类重复的小标题不带关键词，留着。
3. 广告图：同一来源里至少 5 篇都出现的同一张图（按内容哈希），而且不是成批一起出现
   （飞书父文档会带上子文档的整批插图，那是正文）。二维码、优惠券海报、「长按关注」都是这种。
   模板图是一段时间里几乎篇篇都贴的：从它第一次到最后一次出现，这一来源那段时间的文章至少两成带着它。
   作者隔几个月、几年才重用一次的示意图（Stratechery 的 Smiling Curve、Aggregation Theory）不到这个比例，
   留着（见 _occasional）。
4. 文末号召：就算这句话没有在很多篇里重复，只要落在正文后面，并且是招募、二维码、
   长按关注、加微信这类号召，显示时整段拿掉。文件名或替代文字像二维码的图也拿掉。
   飞书笔记不走这一条，只去归档头。
   - 号召行：关键词只是句子里提到的一样东西（「知识星球是内耗型的产品」「加我微信后直接给我转了钱」）不算，
     要冲着读者说、或者有两类推销信号、或者整行差不多就是号召本身（is_cta_line）。
   - 推销段落：写成带句号的长句也认，看信号组合，不看具体措辞：找作者、请读者加入、谢绝白嫖这几类里至少一类，
     再加上入群、扫码、点赞关注、优惠这些里任意一类（is_promo_paragraph）。正文中间不按这个删。
   - 号召写成小标题、后面再没有 Markdown 小标题、而且后面还有别的号召的，从它起到文末都切，
     「【会员权益】1）……」这种长条目一起去掉。小标题也可以是「关于付费星球」这种单独成行、带推销词的短行，
     这时后面那段里要有冲着读者说的话（欢迎感兴趣的同学加入、加我微信），光是分析知识星球的不算。
   - 文首号召：标题下面、正文第一行之前的号召行和推销段落（「tips：致读者朋友……设为星标⭐️」）也去掉。
   - 号召前面的碎句，连起来带推销信号才一起切；连起来是一句正文的留下。切口那一行开头不带推销信号的整句留下。
   - 句子里括号中的微信号（「（微信交流：abc123）」）拿掉，句子留下。
   - 全篇没有一行实质文字的帖，号召后面不是二维码的图留下（那张截图就是正文）。
5. 星球长文的导出脚手架：「> 以下为话题摘要，全文见下方「长文」。」、后面的话题摘要
   （长文开头的截断，和长文重复）、「## 长文：标题」下一行的「原文链接 · 原始 HTML：raw/articles/…」。
   只留长文正文；摘要的加粗标题和长文标题不一样时留下标题。
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_IMAGE = re.compile(
    r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I
)
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_META_KEYS = (
    r"作者|时间|发布时间|类型|点赞|sn|topic_id|原链接|原文链接|原始数据|图片位置|来源|公众号|阅读人数|阅读|"
    r"ghid|appmsgid|position|author|url|source|rss_link"
)
_META_LINE = re.compile(r"^\s*[-*]\s*(?:" + _META_KEYS + r")\s*(?:[:：]|\s+\d|\s*·)", re.I)
# 带这些词的行在文末算引流。统计尾巴用它；单独出现在研究正文里（比如讨论扫码支付）不够切段。
PROMO = re.compile(
    r"加入(?:会员群|知识星球|星球|微信群|交流群|社群|我们)|知识星球|会员群|交流群|扫码|二维码|长按(?:识别|扫码|关注)|"
    r"白嫖|非诚勿扰|加我|加微信|微信号|私信我|优惠券|立减|免费赠送|直接购买|备注[:：]|点个?在看|点赞.{0,3}在看|"
    r"星标|关注(?:公众号|我们|本号)|转发.{0,4}朋友圈|欢迎(?:关注|转发)|赞赏|打赏|推荐阅读|往期(?:回顾|推荐|文章)"
)
# 不靠跨篇统计、也可以从文末拿掉的号召。不收单独的「扫码」「二维码」，避免误切支付、行业正文。
_CTA = re.compile(
    r"招募|加入(?:会员群|知识星球|星球|微信群|交流群|社群)|知识星球|会员群|"
    r"长按(?:识别|扫码|关注)|扫码关注|识别(?:图中)?二维码|"
    r"关注(?:公众号|本号)|加微信|加我(?:微信|[:：])|微信号|私信我(?!们)|"
    r"白嫖|非诚勿扰|点个?在看|点赞.{0,4}在看|设为星标|"
    r"赞赏码|请赞赏|打赏|推荐阅读|往期(?:回顾|推荐)|"
    r"欢迎关注(?:公众号|本号)|欢迎转发|欢迎加入"
)
# 微信号：「加我：abc123」「加我微信沟通abc123」「微信（abc_x9）」。号里要有数字、下划线或连字符，
# 「微信跟WhatsApp」「微信、Costco」这种正文里的英文名不算。
_HANDLE = re.compile(
    r"(?:加我|私信我?|联系我?|微信号?|v信|(?<![A-Za-z0-9])(?:vx|wx)(?![A-Za-z]))"
    r"\s*(?:微信|好友)?\s*(?:交流|沟通|咨询|联系)?\s*(?:是|为)?\s*[：:（(]?\s*"
    r"(?=[A-Za-z][A-Za-z]*[-_0-9])[A-Za-z][-_A-Za-z0-9]{5,19}(?![-_A-Za-z0-9])",
    re.I,
)
# 括号里只有一个微信号的联系方式，夹在正文句子末尾也拿掉：「……狠狠地买入。（微信交流：abc123）」。
_CONTACT_SPAN = re.compile(r"(?<!\])[（(][^（）()\n]{0,12}?(?:" + _HANDLE.pattern + r")[^（）()\n]{0,12}[）)]", re.I)
# 写成整段长句的推销分几类。每一类单独出现在研究正文里都常见（扫码支付、知识星球的生意、综艺《非诚勿扰》、
# 「感兴趣的朋友可以找来看看」），所以单凭一类不算。
_PROMO_KINDS = {
    # 请读者找作者本人。「有朋友私信我问」「让别人加我为好友」「加微信的人数」这类叙述不算。
    "contact": re.compile(
        r"(?:可以|也可|直接|欢迎|请|就|可)[^，。！？；\n]{0,6}(?:加我|私信我|联系我|找我)|"
        r"加我(?:微信|好友)?\s*[:：（(]|私信我(?:问题|咨询|交流)|我的微信号?\s*(?:是|为|[:：])|加(?:个)?微信(?:聊|交流|沟通|咨询)|"
        r"来[^，。！？；\n]{0,8}找我吧"
    ),
    "group": re.compile(
        r"会员群|群会员|交流群|交流社群|付费(?:群|社群|星球|会员)|知识星球|我的星球|加社群|进群|入群|社群会员|"
        r"加入(?:我们|会员|星球|社群|交流群|微信群|读者群)|星球(?:里|内)(?:发帖|提问)"
    ),
    # 谢绝白嫖。光有「白嫖」是叙述：「能白嫖绝不付费」「买了货先白嫖运费险」「成功白嫖特殊福利」。
    "gate": re.compile(r"白嫖勿扰|(?:拒绝|谢绝)白嫖|非诚勿扰|只跟(?:群)?会员|只回答(?:付费)?会员"),
    "qr": re.compile(r"二维码|扫码|长按(?:识别|扫码|关注)|小程序码"),
    "social": re.compile(
        r"点个?(?:赞|在看)|点.{0,2}在看|星标|置顶|关注(?:公众号|本号|我们|我(?!们))|加(?:个)?关注|"
        r"朋友圈(?:分享|里分享)|赞赏|打赏|转发(?:给|到)"
    ),
    # 「打个广告」是作者自己说在打广告；不带「个」的「在电视上打广告」多是叙述，不算。
    "sale": re.compile(
        r"优惠券|新人立减|立减\s*\d|免费赠送|直接购买|限时优惠|早鸟|名额有限|\d+\s*元\s*/\s*年|无理由退款|打个小?广告"
    ),
    "invite": re.compile(
        r"欢迎(?:感兴趣的|大家|你|各位)?.{0,6}(?:加入|关注|订阅|私信)|"
        r"感兴趣的(?:朋友|同学|读者)[，,\s]{0,2}(?:也|还|都)?可以(?:加|私信|联系|扫码|到|来)|"
        r"(?:可以|欢迎)(?:在|到|来)(?:我的)?(?:知识)?星球(?:里|内|上)?(?:发帖|提问|交流|玩)|"
        r"适合.{0,40}的朋友加入|你也可以选择(?:直接)?(?:私信|购买|加)|新用户加入|致读者"
    ),
}
# 冲着读者或作者本人渠道说的几类：找作者、请读者加入、谢绝白嫖。推销段落至少要有一类。
_DIRECTED = frozenset({"contact", "gate", "invite"})
_HAN = re.compile(r"[一-鿿]")
_QR_NAME = re.compile(r"二维码|qrcode|qr[-_ ]?code|(?:^|[\\/_.-])qr(?:[\\/_.-]|$)", re.I)
_MD_IMAGE_FULL = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_IMAGE_TAG = re.compile(r"<img\b[^>]*>", re.I)
_QR_WIDGET = re.compile(r"<mp-common-qrcode\b[^>]*>(?:.*?</mp-common-qrcode>)?", re.I | re.S)
_HTML_ATTR = re.compile(r"""\b([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(["'])(.*?)\2""", re.I)
_HTML_BLOCK = re.compile(
    r"<p\b[^>]*>.*?</p>|<h[1-6]\b[^>]*>.*?</h[1-6]>|<figure\b[^>]*>.*?</figure>|<img\b[^>]*>",
    re.I | re.S,
)
_BARE_META = re.compile(r"^\s*(?:" + _META_KEYS + r")\s*[:：]\s*\S.{0,160}\s*$", re.I)
_ONLY_URL = re.compile(r"^\s*https?://\S+\s*$", re.I)
_READ_COUNT = re.compile(r"^\s*(?:阅读|在看|点赞|评论)\s*[:：]?\s*[\d,]+\s*$")
_RULE = re.compile(r"^[\s—\-–*·•_=~─]+$")
_SENTENCE_END = re.compile(r"[。！？!?]")
COMMENTS = re.compile(r"^##\s*(?:评论|评论里的回复)")
MIN_LINE = 4  # 归一化后短于这个的行算碎行，不参与统计
MIN_DF = 5
DF_SHARE = 0.02
IMAGE_MIN_DF = 5
QR_ALT_MAX = 40  # 替代文字长过这个就是说明句，不按里面提到二维码判
IMAGE_COVERAGE = 0.2  # 第一次到最后一次出现之间，带着这张图的篇数不到这个比例，算作者重用的示意图
IMAGE_BATCH = 3  # 同一批文章里一起出现的图达到这么多张，算正文插图（飞书父子文档）
TAIL_RATIO = 0.25  # 切掉的尾巴里，正文字数不超过模板字数的这个比例
TAIL_GOOD_MAX = 160
TAIL_START = 0.35
IMAGE_WEIGHT = 400
TAIL_STRONG = 2  # 尾巴里至少两行模板句或模板图，单独一行话题标签不算
# 文档库（飞书）是作者自己整理的笔记，没有引流尾巴；重复的截图是父子文档共用的正文插图。只去归档头。
LIBRARY_KINDS = frozenset({"feishu"})  # 尾巴只能从全文这个位置之后开始


def normalize(text: str) -> str:
    return "".join(_KEEP.findall(text or "")).lower()


@dataclass
class Profile:
    lines: set[str] = field(default_factory=set)
    images: set[str] = field(default_factory=set)

    def common(self, norm: str) -> bool:
        return norm in self.lines


EMPTY = Profile()


def image_refs(line: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _IMAGE.finditer(line)]


def _only_images(line: str) -> bool:
    return bool(_IMAGE.search(line)) and not _IMAGE.sub("", line).strip()


def _html_attr(tag: str, name: str) -> str:
    wanted = name.lower()
    for key, _quote, value in _HTML_ATTR.findall(tag):
        if key.lower() == wanted:
            return value
    return ""


def looks_like_qr(ref: str, alt: str = "") -> bool:
    """文件名或替代文字像公众号二维码。正文里讨论「二维码支付」不算。

    替代文字只看短的（标签）：一整句说明里提到 QR code 的是讲产品的截图（Stratechery 21655 那张
    「配对时扫 Mac 上的 QR code」），不是引流码。
    """
    if len(alt or "") <= QR_ALT_MAX and _QR_NAME.search(alt or ""):
        return True
    name = (ref or "").split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    return bool(name and _QR_NAME.search(name))


def _plain(line: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", line or "").strip()


def _is_archive_header_line(line: str) -> bool:
    """归档头：作者、时间、原链接、阅读数。带句号的判断句不是头。链接里的问号不算句号。"""
    stripped = line.strip()
    if not stripped:
        return False
    if not (_META_LINE.match(line) or _BARE_META.match(line) or _ONLY_URL.match(line) or _READ_COUNT.match(line)):
        return False
    if re.search(r"https?://", stripped):
        return True
    return not re.search(r"[。！？]", stripped)


def is_cta_line(line: str) -> bool:
    """招募、关注、加微信这类号召。一句带句号的研究判断里偶尔出现这些词，留下。

    关键词只是句子里提到的一样东西，也留下：「知识星球是内耗型的产品」「（1）知识星球的20%抽成」
    「去年有一个人，加我微信后直接给我转了小一万块钱」「视频号直播打赏」。号召要么冲着读者说
    （找作者、请加入、谢绝白嫖，见 _DIRECTED），要么有两类推销信号，要么整行差不多就是号召本身
    （「— 加入会员群—」「推荐阅读」「点个在看」）。
    """
    text = _plain(line)
    if not text or not _CTA.search(text):
        return False
    compact = normalize(text)
    if _SENTENCE_END.search(text) and len(compact) > 18:
        return False
    kinds = promo_kinds(text)
    if not kinds & _DIRECTED and len(kinds) < 2 and _cta_share(compact) < 0.5:
        return False
    if len(compact) <= 80:
        return True
    match = _CTA.search(text)
    return bool(match and match.start() <= 6 and len(compact) <= 120)


def _cta_share(compact: str) -> float:
    """引流用词（_CTA、PROMO、_PROMO_KINDS 任一）盖住的字占整行的比例。compact 是 normalize 之后的文字。"""
    if not compact:
        return 0.0
    covered: set[int] = set()
    for pattern in (_CTA, PROMO, *_PROMO_KINDS.values()):
        for match in pattern.finditer(compact):
            covered.update(range(match.start(), match.end()))
    return len(covered) / len(compact)


def promo_kinds(text: str) -> set[str]:
    """一段话里出现了哪几类推销信号（见 _PROMO_KINDS）。带微信号的算 contact。"""
    found = {kind for kind, pattern in _PROMO_KINDS.items() if pattern.search(text or "")}
    if _HANDLE.search(text or ""):
        found.add("contact")
    return found


def is_promo_paragraph(line: str) -> bool:
    """整段写成长句的推销：「适合认可……的朋友加入……私信我（加我：…… 备注：群会员）」
    「我也会在朋友圈分享……只跟群会员沟通……白嫖勿扰」「有问题可以在星球里发帖提问……加我微信……」。

    is_cta_line 为了不误伤研究判断，带句号的长句一律留下；这里看的是信号的组合：至少两类信号，
    而且至少一类是冲着读者说的（_DIRECTED）。只在文末切尾时用，正文中间不按这个删。
    """
    text = _plain(line)
    # 除了微信号，各类信号都是中文词；没有汉字的行（英文正文）最多一类，不可能是推销段落。
    if not text or not _HAN.search(text):
        return False
    # 先看冲着读者说的那几类，绝大多数正文行到这里就结束，不用把每类都查一遍。
    if not (_HANDLE.search(text) or any(_PROMO_KINDS[kind].search(text) for kind in _DIRECTED)):
        return False
    return len(promo_kinds(text)) >= 2


def strip_contact_spans(text: str) -> str:
    """括号里的微信号（「（微信交流：abc123）」）从正文句子里拿掉，句子留下。"""
    return _CONTACT_SPAN.sub("", text or "")


def _has_promo(text: str) -> bool:
    """带任何一种引流用词（_CTA、PROMO、_PROMO_KINDS、微信号）。"""
    return bool(_CTA.search(text) or PROMO.search(text) or promo_kinds(text))


def _qr_only(line: str) -> bool:
    stripped = line.strip()
    if not _only_images(stripped):
        return False
    refs = image_refs(stripped)
    alts = _MD_IMAGE_FULL.findall(stripped)
    if alts and any(looks_like_qr(ref, alt) for alt, ref in alts):
        return True
    return bool(refs) and all(looks_like_qr(ref) for ref in refs)


def is_fragment(line: str) -> bool:
    """文末被拆开的半句。有句号、写完整了的短判断留下。"""
    text = _plain(line)
    if not text or _RULE.match(text):
        return True
    compact = normalize(text)
    if _SENTENCE_END.search(text):
        return len(compact) <= 8
    return len(compact) <= 40


def is_soft_tail(line: str) -> bool:
    text = line.strip()
    if not text or _RULE.match(text):
        return True
    if _only_images(text):
        return _qr_only(text)
    return is_cta_line(line) or is_fragment(line)


def strip_qr_images(text: str) -> str:
    """拿掉二维码图。插图的文件名不像二维码就留下。"""

    def markdown_image(match: re.Match[str]) -> str:
        if looks_like_qr(match.group(2), match.group(1)):
            return ""
        return match.group(0)

    def html_image(match: re.Match[str]) -> str:
        tag = match.group(0)
        src = _html_attr(tag, "data-src") or _html_attr(tag, "src")
        alt = _html_attr(tag, "alt")
        if looks_like_qr(src, alt):
            return ""
        return tag

    cleaned = _MD_IMAGE_FULL.sub(markdown_image, text or "")
    cleaned = _HTML_IMAGE_TAG.sub(html_image, cleaned)
    cleaned = _QR_WIDGET.sub("", cleaned)
    return cleaned


def _lead_sentences(line: str) -> str:
    """一行开头那几句不带任何推销信号的整句（到第一个带信号的句子为止）。太短的不要。"""
    kept: list[str] = []
    for part in re.split(r"(?<=[。！？!?])", line):
        if not part.strip():
            continue
        if promo_kinds(part) or _CTA.search(part) or PROMO.search(part):
            break
        kept.append(part)
    lead = "".join(kept).rstrip()
    return lead if len(normalize(_plain(lead))) >= 12 else ""


def _template_line(line: str, profile: Profile, hashes: dict[str, str]) -> bool:
    """这一来源很多篇都有的整行，或很多篇都有的图（模块说明第 2、3 条）。"""
    stripped = line.strip()
    if not stripped:
        return False
    if _only_images(stripped):
        refs = image_refs(stripped)
        return bool(refs) and all(hashes.get(ref) in profile.images for ref in refs)
    norm = normalize(_plain(line))
    return len(norm) >= MIN_LINE and profile.common(norm)


class _Judge:
    """切尾时对一行的几种判断，按行缓存（一篇里同一行常要判好几次）。"""

    def __init__(self, profile: Profile, hashes: dict[str, str]):
        self.profile = profile
        self.hashes = hashes
        self._template: dict[str, bool] = {}
        self._signal: dict[str, bool] = {}
        self._substantial: dict[str, bool] = {}

    def template(self, line: str) -> bool:
        if line not in self._template:
            self._template[line] = _template_line(line, self.profile, self.hashes)
        return self._template[line]

    def signal(self, line: str) -> bool:
        """号召行、二维码图、推销段落，或带引流词的模板句。"""
        if line not in self._signal:
            self._signal[line] = bool(
                is_cta_line(line)
                or _qr_only(line)
                or is_promo_paragraph(line)
                or (self.template(line) and PROMO.search(line))
            )
        return self._signal[line]

    def substantial(self, line: str) -> bool:
        """实质正文：36 字以上、不是号召、不是推销段落、也不是模板句的一行。"""
        if line not in self._substantial:
            text = _plain(line)
            self._substantial[line] = bool(
                text
                and not self.signal(line)
                and not self.template(line)
                and not _only_images(line.strip())
                and len(normalize(text)) >= 36
            )
        return self._substantial[line]


def cut_cta_tail(lines: list[str], profile: Profile = EMPTY, hashes: dict[str, str] | None = None) -> list[str]:
    """从最后一段实质正文之后，切掉号召、二维码、推销段落和夹在中间的碎句。

    「实质正文」是 36 字以上、不是号召、不是推销段落、也不是这一来源很多篇都有的模板句的一行。
    推销写成长段落、或者被拆成「我们只在能力圈范围内讨论……」这种多篇重复的长碎句时，以前会被当成正文，
    后面的号召就切不掉。从第一处号召往前，空行、模板句、推销段落、连起来带推销信号的碎句一起切；
    正文插图挡住，不往前切。profile、hashes 给了才认得出模板句和模板图。
    """
    if not lines:
        return lines
    judge = _Judge(profile, hashes or {})
    template, signal, substantial = judge.template, judge.signal, judge.substantial
    section = _pitch_section(lines, signal, substantial)
    last_substantial = -1
    for index, line in enumerate(lines[:section]):
        if substantial(line):
            last_substantial = index
    if last_substantial < 0:
        region_start = max(0, len(lines) - max(12, len(lines) // 3))
    else:
        region_start = last_substantial + 1
    region = lines[region_start:]
    signal_at = None
    for index, line in enumerate(region):
        if signal(line):
            signal_at = index
            break
    # 推销段从「关于付费星球」这种本身不算号召的短行起，切口最晚从这一行开始。
    if region_start <= section < len(lines) and (signal_at is None or region_start + signal_at > section):
        signal_at = section - region_start
    if signal_at is None:
        return lines
    cursor = signal_at
    while cursor > 0 and (
        is_soft_tail(region[cursor - 1]) or template(region[cursor - 1]) or is_promo_paragraph(region[cursor - 1])
    ):
        cursor -= 1
    # 号召前面那几行碎句，连起来带推销信号才一起切（「适合认可巴菲特、」「价值投资理念的朋友加入，」）；
    # 连起来是一句正文的（「最后你的每股收益增速只有不到 10%」「，这你是需要知道的。」）留下。
    pieces = [
        index
        for index in range(cursor, signal_at)
        if _plain(region[index])
        and not _only_images(region[index].strip())
        and not template(region[index])
        and not is_cta_line(region[index])
        and not is_promo_paragraph(region[index])
    ]
    if pieces and not _has_promo("".join(_plain(region[index]) for index in pieces)):
        cursor = pieces[-1] + 1
    cut = region_start + cursor
    if last_substantial >= 0 and cut <= last_substantial:
        cut = last_substantial + 1
    kept = lines[:cut]
    # 切口那一行是推销段落或号召时，开头不带推销信号的整句留下：「……关键词就包括公私域、社交传播。
    # 感兴趣的同学可以加微信聊聊（……）」只切后一句。
    boundary = next((line for line in lines[cut:] if line.strip()), "")
    if boundary and not template(boundary) and (is_promo_paragraph(boundary) or is_cta_line(boundary)):
        lead = _lead_sentences(boundary)
        if lead:
            kept.append(lead)
    # 全篇没有一行实质文字时，切掉的那段里不是二维码、也不是多篇共用的模板图的图留下：方伟公众号
    # 「群友提问」这类只有一张长截图的帖，截图排在文首的号召后面，那张图就是正文。有实质文字的帖，
    # 号召后面的图多是入群码、推销截图，照旧切掉。
    if last_substantial < 0:
        pictures = [
            line for line in lines[cut:] if _only_images(line.strip()) and not _qr_only(line) and not template(line)
        ]
        for line in pictures:
            kept += ["", line]
    # 切完以后文末只剩小标题（「## 回答」「——交流社群——」「【会员权益】」）的，小标题也去掉。
    while len(kept) > 1 and (not kept[-1].strip() or _is_title_line(kept[-1])):
        if kept[-1].strip() and not any(line.strip() and not _is_title_line(line) for line in kept[:-1]):
            break
        kept.pop()
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def _pitch_section(lines: list[str], signal: Callable[[str], bool], substantial: Callable[[str], bool]) -> int:
    """号召写成小标题、从它起到文末都是推销的那一段从哪行开始；没有就返回 len(lines)。

    星球长文的文末：「— 扫码下方微信加入会员群—」、两段推销、又一个「— 扫码下方微信加入交流群 —」，
    再跟「【会员权益】1）线下 1 对 1 交流……5）半年已超 75 人加入」这种长条目。条目一条条都像正文，
    只看「最后一段实质正文」就切不到。认作推销段要同时满足：前面已经有实质正文（不是文章自己的标题），
    后面再没有 Markdown 小标题，后面还有别的号召，而且段里号召之外的字不超过号召字数的两倍
    （正文中间插一句号召、后面接着长篇分析的，不算）。
    """
    seen_text = False
    for index, line in enumerate(lines):
        if seen_text and _is_title_line(line) and signal(line):
            if _pitch_rest(lines[index + 1 :], signal):
                return index
        elif seen_text and _promo_label(line) and _pitch_rest(lines[index + 1 :], signal, labelled=True):
            return index
        if substantial(line):
            seen_text = True
    return len(lines)


def _pitch_rest(rest: list[str], signal: Callable[[str], bool], *, labelled: bool = False) -> bool:
    """小标题之后到文末是不是一整段推销（见 _pitch_section）。

    labelled：小标题本身不是号召，只是「关于付费星球」这种带推销词的短行。这时段里要有冲着读者说的话
    （欢迎加入、加我微信、私信我），带任何一类推销信号的行都算号召的字。
    """
    if any(_HEADING.match(item) for item in rest):
        return False
    pitch = other = 0
    directed = False
    for item in rest:
        if not item.strip() or _only_images(item.strip()):
            continue
        size = len(normalize(_plain(item)))
        kinds = promo_kinds(item) if labelled else set()
        directed = directed or bool(kinds & _DIRECTED)
        if signal(item) or kinds:
            pitch += size
        else:
            other += size
    if labelled and not directed:
        return False
    return bool(pitch) and other <= 2 * pitch


def _promo_label(line: str) -> bool:
    """「关于付费星球」这种单独成行、带推销词的短行：不到 12 个字，没有标点。"""
    text = _plain(line).replace("**", "").strip()
    if not text or _only_images(text) or re.search(r"[。！？!?，,；;：:、]", text):
        return False
    return len(normalize(text)) <= 12 and bool(promo_kinds(text))


_DECORATED_TITLE = re.compile(r"^\s*(?:[—–-]+|【)\s*[^—–\-【】\n]{1,20}?\s*(?:[—–-]+|】)\s*$")


def _is_title_line(line: str) -> bool:
    """Markdown 小标题，或者两头加横线、方括号的短标题。"""
    return bool(_HEADING.match(line) or _DECORATED_TITLE.match(_plain(line).replace("**", "")))


def _looks_like_html_body(text: str) -> bool:
    sample = (text or "").lstrip()[:500].lower()
    if sample.startswith(("#", "---", ">")):
        return False
    return sample.startswith(("<!doctype", "<html", "<body", "<div", "<p", "<section"))


def _block_text(block: str) -> str:
    return re.sub(r"<[^>]+>", "", block or "").strip()


def clean_html_document(html: str) -> str:
    """公众号 HTML：去掉开头的归档头、文末招募和二维码图。正文插图留下。"""
    html = strip_qr_images(html or "")
    blocks = list(_HTML_BLOCK.finditer(html))
    if len(blocks) < 2:
        return html
    kinds: list[str] = []
    for match in blocks:
        raw = match.group(0)
        text = _block_text(raw)
        has_img = bool(re.search(r"<img\b", raw, re.I))
        if has_img and not text:
            src = _html_attr(raw, "data-src") or _html_attr(raw, "src")
            alt = _html_attr(raw, "alt")
            kinds.append("qr" if looks_like_qr(src, alt) else "img")
        elif _is_archive_header_line(text):
            kinds.append("meta")
        elif is_cta_line(text) or is_promo_paragraph(text):
            kinds.append("cta")
        elif not text:
            kinds.append("empty")
        else:
            kinds.append("text")
    drop: set[int] = set()
    started = False
    for index, kind in enumerate(kinds):
        if kind in {"meta", "empty"} and not started:
            if kind == "meta" or (kind == "empty" and not started):
                drop.add(index)
            continue
        if kind in {"text", "img", "cta", "qr"}:
            started = True
    last_text = -1
    for index, kind in enumerate(kinds):
        if kind != "text":
            continue
        text = _block_text(blocks[index].group(0))
        if len(normalize(text)) >= 36 and not is_cta_line(text):
            last_text = index
    if last_text < 0:
        tail_from = max(0, len(kinds) - max(8, len(kinds) // 3))
    else:
        tail_from = last_text + 1
    signal = None
    for index in range(tail_from, len(kinds)):
        if kinds[index] in {"cta", "qr"}:
            signal = index
            break
    if signal is not None:
        cursor = signal
        while cursor > tail_from and kinds[cursor - 1] in {"cta", "qr", "empty", "meta"}:
            cursor -= 1
        while cursor > tail_from and kinds[cursor - 1] == "text":
            if not is_fragment(_block_text(blocks[cursor - 1].group(0))):
                break
            cursor -= 1
        for index in range(cursor, len(kinds)):
            drop.add(index)
    if not drop:
        return re.sub(r"\n{3,}", "\n\n", html).strip("\n") + ("\n" if html.strip() else "")
    pieces: list[str] = []
    pos = 0
    for index, match in enumerate(blocks):
        if index in drop:
            pieces.append(html[pos : match.start()])
            pos = match.end()
    pieces.append(html[pos:])
    text = "".join(pieces)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return text + ("\n" if text else "")


def strip_meta(body: str) -> str:
    """去掉开头的归档头。只看标题之后、正文第一句之前那一段。有没有列表横线都认。"""
    lines = body.split("\n")
    out: list[str] = []
    started = False
    for index, line in enumerate(lines):
        if started:
            out.extend(lines[index:])
            break
        stripped = line.strip()
        if not stripped or _HEADING.match(line):
            out.append(line)
            continue
        if _is_archive_header_line(line):
            continue
        started = True
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


_LONGFORM_NOTE = re.compile(r"^\s*>\s*以下为话题摘要[，,]\s*全文见下方「长文」[。.]?\s*$")
_LONGFORM_HEADING = re.compile(r"^\s*##\s*长文\s*[:：]\s*(?P<title>\S.*?)\s*$")
# 「原文链接：https://articles.zsxq.com/… · 原始 HTML：[raw/articles/….html](../../raw/articles/….html)」
_LONGFORM_SOURCE = re.compile(r"^\s*(?:原文链接\s*[:：]\s*\S+\s*·\s*)?原始\s*HTML\s*[:：]", re.I)
_BOLD_LINE = re.compile(r"^\s*\*\*(?P<title>[^*]+)\*\*\s*$")


def drop_longform_scaffold(body: str) -> str:
    """星球「话题 + 长文」帖的导出脚手架（模块说明第 5 条）。没有「## 长文：」小标题时原样返回。"""
    lines = body.split("\n")
    heading = next((i for i, line in enumerate(lines) if _LONGFORM_HEADING.match(line)), None)
    if heading is None:
        return body
    title = _LONGFORM_HEADING.match(lines[heading]).group("title")
    note = next((i for i in range(heading) if _LONGFORM_NOTE.match(lines[i])), None)
    head = lines[:heading] if note is None else lines[:note]
    keep_heading = True
    if note is not None:
        preview = [line for line in lines[note + 1 : heading] if line.strip()]
        bold = _BOLD_LINE.match(preview[0]) if preview else None
        # 摘要的加粗标题就是长文标题：页面顶上已经是这个标题，小标题不再重复。
        keep_heading = not (bold and bold.group("title").strip() == title)
    rest = lines[heading + 1 :]
    first = next((i for i, line in enumerate(rest) if line.strip()), None)
    if first is not None and _LONGFORM_SOURCE.match(rest[first]):
        rest = rest[first + 1 :]
    if keep_heading:
        head = [*head, "", lines[heading]]
    return re.sub(r"\n{3,}", "\n\n", "\n".join([*head, "", *rest]))


def _drop_lead_cta(lines: list[str]) -> list[str]:
    """文首号召：标题下面、正文第一行之前的号召行和推销段落去掉（「tips：致读者朋友……设为星标⭐️」）。

    整篇都是号召的帖原样返回，留给文末的规则判断，不在这里删空。
    """
    for index, line in enumerate(lines):
        if not line.strip() or _HEADING.match(line):
            continue
        if is_cta_line(line) or is_promo_paragraph(line):
            continue
        dropped = {i for i in range(index) if lines[i].strip() and not _HEADING.match(lines[i])}
        if not dropped:
            return lines
        return [item for i, item in enumerate(lines) if i not in dropped]
    return lines


def _split_comments(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if COMMENTS.match(line):
            return index
    return len(lines)


def _classify(line: str, profile: Profile, hashes: dict[str, str]) -> tuple[str, int]:
    """('bad' | 'good' | 'neutral', 字数)。"""
    stripped = line.strip()
    if not stripped:
        return "neutral", 0
    if _only_images(stripped):
        refs = image_refs(stripped)
        if refs and all(hashes.get(ref) in profile.images for ref in refs):
            return "bad", 10
        # 不是模板图的插图算正文，而且分量很重：尾巴不能越过它往前切。
        return "good", IMAGE_WEIGHT
    norm = normalize(stripped)
    if len(norm) < MIN_LINE:
        return ("bad", len(norm)) if PROMO.search(stripped) else ("neutral", 0)
    if profile.common(norm):
        return "bad", len(norm)
    if PROMO.search(stripped):
        # 长句里只是提到（「前段时间被一家企业白嫖了2次」「（备注：……不是我的能力圈范围内）」），
        # 一类推销信号都没有，算正文。
        if len(norm) >= 36 and not promo_kinds(stripped) and not is_cta_line(stripped):
            return "good", len(norm)
        return "bad", len(norm)
    return "good", len(norm)


def _tail_has_hook(lines: list[str], profile: Profile, hashes: dict[str, str], start: int) -> bool:
    """尾巴里必须有引流词或广告图，光是多篇重复的正文套话不够（评测语料会共享细节句）。"""
    for line in lines[start:]:
        if PROMO.search(line):
            return True
        refs = image_refs(line)
        if refs and any(hashes.get(ref) in profile.images for ref in refs):
            return True
    return False


def _tail_cut(lines: list[str], profile: Profile, hashes: dict[str, str]) -> int:
    kinds = [_classify(line, profile, hashes) for line in lines]
    total = sum(size for kind, size in kinds if kind != "neutral") or 1
    prefix = [0]
    for kind, size in kinds:
        prefix.append(prefix[-1] + (size if kind != "neutral" else 0))
    good = bad = 0
    strong = 0
    cut = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        kind, size = kinds[index]
        if kind == "good":
            good += size
        elif kind == "bad":
            bad += size
            norm = normalize(lines[index])
            # 只有统计出来的模板句、模板图才算铁证；光有「扫码」「二维码」这类词可能是正文在谈支付。
            if profile.common(norm) or image_refs(lines[index]):
                strong += 1
        if good > TAIL_GOOD_MAX * 3:
            break
        if (
            kind == "bad"
            and strong >= TAIL_STRONG
            and good <= max(bad * TAIL_RATIO, 0)
            and good <= TAIL_GOOD_MAX
            and prefix[index] >= total * TAIL_START
            and _tail_has_hook(lines, profile, hashes, index)
        ):
            cut = index
    return _after_last_text(lines, cut, _Judge(profile, hashes))


def _after_last_text(lines: list[str], cut: int, judge: _Judge) -> int:
    """统计切尾的切口和第一处号召之间还有实质正文时，切口挪到这段正文之后。

    容差（尾巴里正文不超过 160 字）是给推销块里换了数字、换了措辞的条目留的，不是给推销前面的正文。
    方伟公众号的老文章按段估过图的位置，文末的入群码、优惠券图有时插进了最后几段正文中间；常被引用的文章标题
    （「抄底就是伟大公司遇到了可解决的问题」）也是模板句。统计切尾从这些行起切，后面一两段正文就跟着没了。
    号召之后的条目（持仓、权益）照旧按容差切；夹在中间的模板图由 clean_body 单独去掉。
    """
    if cut >= len(lines):
        return cut
    end = next((index for index in range(cut, len(lines)) if judge.signal(lines[index])), len(lines))
    last = max((index for index in range(cut, end) if judge.substantial(lines[index])), default=-1)
    return last + 1 if last >= 0 else cut


def clean_body(
    body: str,
    profile: Profile = EMPTY,
    hashes: dict[str, str] | None = None,
    *,
    drop_cta: bool = True,
) -> str:
    """body 是去掉 frontmatter 之后的 Markdown。hashes：图片原始引用 → 内容哈希。

    drop_cta 为假时只去归档头和统计出来的模板（飞书笔记用这条）。
    """
    hashes = hashes or {}
    body = drop_longform_scaffold(strip_meta(body or ""))
    lines = body.split("\n")
    split = _split_comments(lines)
    main, rest = lines[:split], lines[split:]
    if drop_cta:
        main = _drop_lead_cta(main)
    cut = _tail_cut(main, profile, hashes)
    main = main[:cut]
    if drop_cta:
        # 先去掉句子里括号中的微信号，再按整段切尾，最后去正文中间的模板句：反过来的话，带关键词的几行先没了，
        # 剩下「适合认可巴菲特、」「我们只在能力圈范围内讨论……」这些不带关键词的碎句留在文末，后面的号召也就切不到了。
        main = cut_cta_tail(strip_contact_spans("\n".join(main)).split("\n"), profile, hashes)
    kept: list[str] = []
    for line in main:
        stripped = line.strip()
        refs = image_refs(stripped)
        if refs and profile.images:
            if any(hashes.get(ref) in profile.images for ref in refs):
                remainder = _IMAGE.sub(
                    lambda m: "" if hashes.get(m.group(1) or m.group(2)) in profile.images else m.group(0), line
                )
                if not remainder.strip():
                    continue
                line = remainder
        norm = normalize(stripped)
        if len(norm) >= MIN_LINE and profile.common(norm) and PROMO.search(stripped):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    # 尾巴前面常留一行「—」「*」之类的分隔符。
    while cut < len(main) and kept and re.fullmatch(r"[\s—\-–*·•_=~─]+", kept[-1]):
        kept.pop()
        while kept and not kept[-1].strip():
            kept.pop()
    text = "\n".join(kept)
    if drop_cta:
        text = strip_contact_spans(strip_qr_images(text))
        text = "\n".join(cut_cta_tail(text.split("\n"), profile, hashes))
    if rest:
        text = text.rstrip() + "\n\n" + "\n".join(rest)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n") + ("\n" if text else "")


# —— 按来源统计 ——

_cache: dict[str, tuple[tuple, Profile]] = {}
_lock = threading.Lock()


def _signature(conn, source_id: str) -> tuple:
    row = conn.execute(
        "SELECT count(*) AS n, max(updated_at) AS t FROM articles WHERE source_id = %s AND duplicate_of IS NULL",
        (source_id,),
    ).fetchone()
    images = conn.execute("SELECT count(*) AS n FROM article_images WHERE source_id = %s", (source_id,)).fetchone()
    return (int(row["n"]), str(row["t"]), int(images["n"]))


def threshold(articles: int) -> int:
    return max(MIN_DF, math.ceil(articles * DF_SHARE))


def build_profile(conn, source_id: str) -> Profile:
    from researchlib.textutil import split_frontmatter

    rows = conn.execute(
        "SELECT id, body_md FROM articles WHERE source_id = %s AND duplicate_of IS NULL", (source_id,)
    ).fetchall()
    df: Counter[str] = Counter()
    for row in rows:
        _front, body = split_frontmatter(row["body_md"] or "")
        lines = body.split("\n")
        seen = {normalize(line) for line in lines[: _split_comments(lines)]}
        df.update(norm for norm in seen if len(norm) >= MIN_LINE)
    need = threshold(len(rows))
    common = {norm for norm, count in df.items() if count >= need}
    image_rows = conn.execute(
        """
        SELECT i.content_hash, array_agg(DISTINCT i.article_id ORDER BY i.article_id) AS ids
        FROM article_images i JOIN articles a ON a.id = i.article_id
        WHERE i.source_id = %s AND a.duplicate_of IS NULL
        GROUP BY i.content_hash
        HAVING count(DISTINCT i.article_id) >= %s
        """,
        (source_id, IMAGE_MIN_DF),
    ).fetchall()
    batches = Counter(tuple(row["ids"]) for row in image_rows)
    images = {row["content_hash"] for row in image_rows if batches[tuple(row["ids"])] < IMAGE_BATCH}
    return Profile(common, images - _occasional(conn, source_id, images))


def _occasional(conn, source_id: str, images: set[str]) -> set[str]:
    """重复图里作者隔一阵才用一次的那些（示意图），不是一段时间里篇篇都贴的模板图。

    看这张图第一次和最后一次出现之间，这一来源发的文章里有几成带着它。模板图（二维码、优惠券、星标指引）
    是那段时间里几乎每篇都贴：11-11 线上方伟公众号 18 张，最低 0.30，多数 0.9 以上；孤独进化体的公众号码
    0.80。Thompson 的示意图十几篇隔几年才重用一次，25 张最高 0.04。按时间看，和图落在正文哪个位置无关：
    公众号老文章按段估过图的位置，广告图常在正文中间。没有发布时间的图照旧按模板图算。
    """
    rows = conn.execute(
        """
        WITH spans AS (
          SELECT i.content_hash, count(DISTINCT a.id) AS n,
                 min(a.published_at) AS first_at, max(a.published_at) AS last_at
          FROM article_images i JOIN articles a ON a.id = i.article_id
          WHERE i.source_id = %s AND i.content_hash = ANY(%s)
            AND a.duplicate_of IS NULL AND a.published_at IS NOT NULL
          GROUP BY i.content_hash
        )
        SELECT s.content_hash, s.n,
               (SELECT count(*) FROM articles a
                WHERE a.source_id = %s AND a.duplicate_of IS NULL
                  AND a.published_at BETWEEN s.first_at AND s.last_at) AS span
        FROM spans s
        """,
        (source_id, sorted(images), source_id),
    ).fetchall()
    return {row["content_hash"] for row in rows if row["n"] < IMAGE_COVERAGE * max(int(row["span"]), 1)}


def profile_for(conn, source_id: str) -> Profile:
    kind = conn.execute("SELECT kind FROM sources WHERE id = %s", (source_id,)).fetchone()
    if kind is not None and kind["kind"] in LIBRARY_KINDS:
        return EMPTY
    signature = _signature(conn, source_id)
    with _lock:
        hit = _cache.get(source_id)
        if hit and hit[0] == signature:
            return hit[1]
    found = build_profile(conn, source_id)
    with _lock:
        _cache[source_id] = (signature, found)
    return found


def image_hashes(conn, article_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT original_ref, content_hash FROM article_images WHERE article_id = %s", (article_id,)
    ).fetchall()
    return {row["original_ref"]: row["content_hash"] for row in rows}


def clean_markdown(
    body_md: str,
    profile: Profile = EMPTY,
    hashes: dict[str, str] | None = None,
    *,
    drop_cta: bool = True,
) -> str:
    """整篇 body_md（可能带 frontmatter）→ 清洗后的正文，不带 frontmatter。"""
    from researchlib.textutil import split_frontmatter

    _front, body = split_frontmatter(body_md or "")
    if drop_cta and _looks_like_html_body(body):
        return clean_html_document(body)
    return clean_body(body, profile, hashes, drop_cta=drop_cta)


def clean_article(conn, article_id: int, source_id: str, body_md: str) -> str:
    kind = conn.execute("SELECT kind FROM sources WHERE id = %s", (source_id,)).fetchone()
    drop_cta = kind is None or kind["kind"] not in LIBRARY_KINDS
    if not drop_cta:
        return clean_markdown(body_md, EMPTY, image_hashes(conn, article_id), drop_cta=False)
    return clean_markdown(body_md, profile_for(conn, source_id), image_hashes(conn, article_id), drop_cta=True)


def search_text(cleaned: str, title: str) -> str:
    """清洗后的正文 → articles.body_text（检索、摘要、观点、卡片摘录读的文字）。

    和标题重复的那行去掉；问答帖的「提问（网友 → 方伟Rey）」只留「提问」，网友的昵称不进来。
    Stratechery 每日更新开头的「Good morning,」「On to the Update:」和结尾的转发须知不进来（housekeeping）。
    """
    from researchlib.housekeeping import strip_housekeeping
    from researchlib.present_text import plain_qa_markdown
    from researchlib.textutil import markdown_to_text, strip_duplicate_title

    return markdown_to_text(plain_qa_markdown(strip_duplicate_title(strip_housekeeping(cleaned), title)))


# —— 全库重洗 ——


def rewash(conn, source_ids: list[str] | None = None, dry_run: bool = False) -> dict:
    """按当前规则重算 body_text 和原文段落块。body_md 不动。返回各来源改动篇数。"""
    from researchlib.chunks import COMMIT_EVERY, replace_chunks

    where = "WHERE duplicate_of IS NULL"
    params: list = []
    if source_ids:
        where += " AND source_id = ANY(%s)"
        params.append(source_ids)
    rows = conn.execute(
        f"SELECT id, source_id, title, body_md, body_text FROM articles {where} ORDER BY id", params
    ).fetchall()
    changed: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for row in rows:
        seen[row["source_id"]] += 1
        cleaned = clean_article(conn, row["id"], row["source_id"], row["body_md"])
        text = search_text(cleaned, row["title"])
        if text == (row["body_text"] or ""):
            continue
        changed[row["source_id"]] += 1
        if dry_run:
            continue
        conn.execute("UPDATE articles SET body_text = %s WHERE id = %s", (text, row["id"]))
        replace_chunks(conn, row["id"], row["title"], row["body_md"])
        # 段落的向量在 replace_chunks 里当场算，一篇一两秒：按改了的篇数分批提交，一批的锁不拿太久，
        # 向量服务中途停了也只退回这一批。
        if sum(changed.values()) % COMMIT_EVERY == 0:
            conn.commit()
    if not dry_run:
        conn.commit()
    return {"articles": dict(seen), "changed": dict(changed)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.embeddings import EmbeddingError, all_or_nothing

    parser = argparse.ArgumentParser(description="按清洗规则重算全库文章的检索文字和段落块（body_md 不动）")
    parser.add_argument("--source", action="append", help="只洗这个来源，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只数会改多少篇，不写库")
    args = parser.parse_args(argv)
    conn = prepare(Settings.from_env())
    try:
        # 改了字的段落当场算向量，和新字一起提交；算不出来就停下，不留下只能按字面找到的段落。
        with all_or_nothing():
            report = rewash(conn, args.source, args.dry_run)
    except EmbeddingError as exc:
        conn.rollback()
        print(
            f"向量服务算不出来，停在这里：{exc}\n已经提交的那些篇字和向量都换好了，没提交的还是旧的。"
            "开着向量服务再跑一遍，从还没改的那篇接着来。",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
