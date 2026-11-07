"""下载来的外链图不算进译文回执：对照按占位那一份核验，显示时把译文里的占位按顺序换成图。"""

from researchlib.translate.provenance import receipt, source_manifest
from researchlib.translate.remote_media import PLACEHOLDER, PairSources, mappings, pair_sources

IMG_A = '<img id="img-aaaaaaaaaaaa" alt="" src="/media/remote/aa/' + "a" * 64 + '.png" />'
IMG_B = '<img id="img-bbbbbbbbbbbb" alt="Revenue" src="/media/remote/bb/' + "b" * 64 + '.png" />'
PLAIN = (f"<p>First paragraph 10 dollars.</p><p>{PLACEHOLDER}</p>"
         f'<p>Second paragraph 20 dollars <a href="https://i0.wp.com/x.png">{PLACEHOLDER}</a> here.</p>')
FULL = (f"<p>First paragraph 10 dollars.</p><p>{IMG_A}</p>"
        f'<p>Second paragraph 20 dollars <a href="https://i0.wp.com/x.png">{IMG_B}</a> here.</p>')
ZH = (f"<p>第一段10美元。</p><p>{PLACEHOLDER}</p>"
      f'<p>第二段20美元<a href="https://i0.wp.com/x.png"><em>【外部图片已省略】</em></a>在这里。</p>')
ZH_SHOWN = (f"<p>第一段10美元。</p><p>{IMG_A}</p>"
            f'<p>第二段20美元<a href="https://i0.wp.com/x.png">{IMG_B}</a>在这里。</p>')


def test_translation_from_before_the_download_is_checked_against_the_placeholders():
    provenance = receipt(source_manifest(PLAIN), ZH, model="synthetic")
    got = pair_sources(FULL, PLAIN, ZH, provenance)
    # 回执照旧按占位那一份核验；配对用现在的原文，译文里的占位（括号写成全角的也算）换成同一张图。
    assert got == PairSources(check=PLAIN, original=FULL, translated=ZH_SHOWN)
    # 旧稿没有回执：译文里的占位和原文没下载时一样多，原文现在又少了占位，就是图后来才下下来。
    assert pair_sources(FULL, PLAIN, ZH, None) == got


def test_translation_made_with_the_images_is_left_alone():
    provenance = receipt(source_manifest(FULL), ZH_SHOWN, model="synthetic")
    assert pair_sources(FULL, PLAIN, ZH_SHOWN, provenance) == PairSources(FULL, FULL, ZH_SHOWN)
    assert pair_sources(FULL, PLAIN, ZH_SHOWN, None) == PairSources(FULL, FULL, ZH_SHOWN)
    # 没有下载来的图：两份一样，照常配对。
    assert pair_sources(PLAIN, PLAIN, ZH, None) == PairSources(PLAIN, PLAIN, ZH)


def test_placeholders_that_do_not_line_up_keep_the_old_pairing():
    # 占位在英文句子中间时整段送模型，模型把它丢了：位置对不上，对照照旧用占位那一份，「原文」页仍有图。
    plain = f"<p>First paragraph 10 dollars {PLACEHOLDER} more.</p>"
    full = f"<p>First paragraph 10 dollars {IMG_A} more.</p>"
    dropped = "<p>第一段10美元，更多。</p>"
    provenance = receipt(source_manifest(plain), dropped, model="synthetic")
    assert pair_sources(full, plain, dropped, provenance) == PairSources(plain, plain, dropped)
    # 正文 HTML 里的 <img> 没下载时整个不显示、不留占位：下下来以后找不到对应的位置。
    plain = "<p>First paragraph 10 dollars.</p><p></p><p>Second paragraph 20 dollars.</p>"
    full = f"<p>First paragraph 10 dollars.</p><p>{IMG_A}</p><p>Second paragraph 20 dollars.</p>"
    zh = "<p>第一段10美元。</p><p></p><p>第二段20美元。</p>"
    provenance = receipt(source_manifest(plain), zh, model="synthetic")
    assert pair_sources(full, plain, zh, provenance) == PairSources(plain, plain, zh)
    # 旧稿的占位比原文没下载时少：不知道是照哪一份译的，和以前一样按现在的原文配。
    assert pair_sources(FULL, PLAIN, f"<p>第一段10美元。</p><p>{PLACEHOLDER}</p><p>第二段。</p>", None).check == FULL


def test_mappings_leave_out_downloaded_images():
    rows = [
        {"original_ref": "images/a.png", "storage_path": "ab/a.png", "remote": False},
        {"original_ref": "https://i0.wp.com/x.png", "storage_path": "remote/cd/c.png", "remote": True},
    ]
    full, plain = mappings(rows)
    assert full == {"images/a.png": "/media/ab/a.png", "https://i0.wp.com/x.png": "/media/remote/cd/c.png"}
    assert plain == {"images/a.png": "/media/ab/a.png"}
    assert mappings(rows[:1]) == ({"images/a.png": "/media/ab/a.png"}, None)
