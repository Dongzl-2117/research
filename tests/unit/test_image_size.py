from PIL import Image

from researchlib.image_size import image_size, size_images


def _save(root, relative, size, *, orientation=None, fmt="PNG"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, "white")
    if orientation is None:
        image.save(path, fmt)
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(path, fmt, exif=exif)
    return path


def test_media_images_get_size_and_tall_screenshots_are_marked(tmp_path):
    root = tmp_path / "images"
    _save(root, "src/aa/chat.png", (1170, 2532))
    _save(root, "src/bb/chart.png", (1200, 800))
    _save(root, "src/cc/near-square.png", (1000, 1200))
    html = (
        '<p><img id="img-aa" alt="" src="/media/src/aa/chat.png" loading="lazy" decoding="async" /></p>'
        '<p><img alt="" src="/media/src/bb/chart.png"></p>'
        '<p><img alt="" src="/media/src/cc/near-square.png"></p>'
    )
    out = size_images(html, root)
    assert '<img id="img-aa" alt="" src="/media/src/aa/chat.png" loading="lazy" decoding="async" ' \
        'width="1170" height="2532" data-shape="tall" />' in out
    assert '<img alt="" src="/media/src/bb/chart.png" width="1200" height="800">' in out
    # 高不到宽的 1.25 倍：照旧铺满一栏，只补宽高。
    assert '<img alt="" src="/media/src/cc/near-square.png" width="1000" height="1200">' in out
    assert size_images(out, root) == out


def test_exif_rotation_counts_as_displayed(tmp_path):
    """手机竖着拍、横着存（EXIF 方向 6）的图，浏览器转正后是竖图。"""
    root = tmp_path / "images"
    path = _save(root, "src/dd/photo.jpg", (2400, 1080), orientation=6, fmt="JPEG")
    assert image_size(path) == (1080, 2400)
    out = size_images('<img src="/media/src/dd/photo.jpg">', root)
    assert 'width="1080" height="2400" data-shape="tall"' in out


def test_existing_size_kept_and_unknown_images_untouched(tmp_path):
    root = tmp_path / "images"
    _save(root, "src/ee/long.png", (1080, 6000))
    # 原文自带宽高的不改，只按文件实际比例标竖图。
    out = size_images('<img src="/media/src/ee/long.png" width="300">', root)
    assert out == '<img src="/media/src/ee/long.png" width="300" data-shape="tall">'
    untouched = [
        '<img src="/media/src/ff/missing.png">',  # 外链图还没下载
        '<img src="https://example.com/a.png">',
        '<img src="/media/../secret.png">',
        '<img data-src="/media/src/ee/long.png">',
    ]
    for tag in untouched:
        assert size_images(tag, root) == tag
    (root / "src/gg").mkdir(parents=True)
    (root / "src/gg/broken.png").write_bytes(b"not an image")
    assert size_images('<img src="/media/src/gg/broken.png">', root) == '<img src="/media/src/gg/broken.png">'
    assert size_images("", root) == "" and size_images("<p>没有图</p>", root) == "<p>没有图</p>"
