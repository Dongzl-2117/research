"""Conservative bubble-color evidence from the original OCR image, not x position.

The array is RGB with shape (height, width, channels). No imaging dependency is
needed here; the OCR-only adapter supplies OpenCV pixels. A neutral page background
is not a guest bubble: require a matching fill around the text and visible bounds.
Unrecognized themes, clipped bubbles, and mixed colors remain unknown.
"""

from __future__ import annotations

from collections import Counter


def _green(rgb) -> bool:
    r, g, b = (int(value) for value in rgb[:3])
    # The standard dark theme is roughly (41, 180, 97), much darker than
    # the light-theme (149, 236, 105). Both retain a strong green channel.
    return 15 <= r <= 205 and g >= 140 and g - r >= 25 and g - b >= 45 and b <= 170


def _neutral(rgb) -> bool:
    values = [int(value) for value in rgb[:3]]
    return max(values) - min(values) <= 18


def _gray_fill(rgb) -> bool:
    # Dark guest bubbles are about (44, 44, 44), with a (17, 17, 17)
    # exterior. Do not mistake black text glyphs for the bubble fill.
    return min(int(value) for value in rgb[:3]) >= 28 and _neutral(rgb)


def bubble_color(image, box: tuple[float, float, float, float]) -> str:
    """Return green / gray only for supported, bounded bubble fill; otherwise ''.

    Sampling ignores horizontal location. Gray also includes WeChat's light-theme
    white guest bubbles. Both need a uniform surround and contrasting boundaries,
    so colored glyphs or a whole neutral/green page do not become speaker evidence.
    """
    if image is None or len(image.shape) != 3 or image.shape[2] < 3:
        return ""
    height, width = image.shape[:2]
    left, top, right, bottom = (int(value) for value in box)
    if not (0 <= left < right < width and 0 <= top < bottom < height):
        return ""
    text_height = bottom - top
    samples = [
        tuple(int(value) for value in image[y, x][:3])
        for y in range(top, bottom + 1, max(1, text_height // 6))
        for x in range(left, right + 1, max(1, (right - left) // 12))
    ]
    green = [rgb for rgb in samples if _green(rgb)]
    neutral = [rgb for rgb in samples if _gray_fill(rgb)]
    if len(green) >= 0.6 * len(samples):
        kind, fill = "green", green
    elif len(neutral) >= 0.6 * len(samples):
        kind, fill = "gray", neutral
    else:
        return ""
    # Quantized mode, not a mean contaminated by black OCR glyphs.
    common, count = Counter(tuple(value // 8 for value in rgb) for rgb in fill).most_common(1)[0]
    if count < 0.6 * len(fill):
        return ""
    color = tuple(value * 8 + 4 for value in common)

    def matches(x: int, y: int) -> bool:
        return max(abs(int(a) - b) for a, b in zip(image[y, x][:3], color, strict=True)) <= 10

    cx, cy = (left + right) // 2, (top + bottom) // 2
    # Start just outside the text rectangle. At least one pixel of surrounding
    # fill is required; colored text on a neutral page is insufficient.
    rays = [(left - 2, cy, -1, 0), (right + 2, cy, 1, 0),
            (cx, top - 2, 0, -1), (cx, bottom + 2, 0, 1)]
    bounded = []
    for x, y, dx, dy in rays:
        if not (0 <= x < width and 0 <= y < height) or not matches(x, y):
            return ""
        found = False
        for distance in range(1, max(12, text_height * 12)):
            px, py = x + dx * distance, y + dy * distance
            if not (0 <= px < width and 0 <= py < height):
                break  # Image clipping is not a bubble boundary.
            if matches(px, py):
                continue
            exterior = image[py, px][:3]
            # Text from another row is not a boundary. Require a neutral exterior
            # and a short stable run beyond the transition.
            points = [(px + dx * n, py + dy * n) for n in range(3)]
            if _neutral(exterior) and all(
                0 <= qx < width and 0 <= qy < height
                and _neutral(image[qy, qx][:3]) and not matches(qx, qy)
                for qx, qy in points
            ):
                found = True
                break
        bounded.append(found)
    # One horizontal and one vertical border, independent of left/right placement.
    return kind if any(bounded[:2]) and any(bounded[2:]) else ""
