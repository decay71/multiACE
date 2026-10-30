"""The slicer preview image in the gcode header: parse_meta captures it on the
way past (it is far longer than the 300-line head window) and the report
carries it to the preflight dialog."""
import base64

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.web.backend import preflight_core as pc

BODY = ("; filament_colour = #FF0000;#00FF00\n"
        "; filament_type = PLA;PLA\n"
        "T0\n;LAYER_CHANGE\n; Change Tool 0 -> Tool 1\nT1\n")


def _block(kind, w, h, payload, width=78):
    """A slicer-style comment block around base64 `payload`."""
    b64 = base64.b64encode(payload).decode()
    lines = [b64[i:i + width] for i in range(0, len(b64), width)]
    out = ["; %s begin %dx%d %d\n" % (kind, w, h, len(b64))]
    out += ["; %s\n" % ln for ln in lines]
    out.append("; %s end\n" % kind.split()[0])
    return "".join(out), b64


def _meta(text):
    return pc.parse_meta(pp, text.splitlines(True))[5]


def test_png_thumbnail_is_captured_whole():
    # Far over 300 lines: would be cut off if it relied on the head window.
    block, b64 = _block("thumbnail", 300, 300, b"\x89PNG" + bytes(range(256)) * 400)
    assert block.count("\n") > 300
    thumb = _meta("; header\n" + block + BODY)["thumbnail"]
    assert thumb == {"mime": "image/png", "w": 300, "h": 300, "b64": b64}


def test_largest_of_several_wins_and_jpg_is_typed():
    small, _ = _block("thumbnail", 48, 48, b"small")
    big, b64 = _block("thumbnail_JPG", 300, 300, b"big" * 50)
    thumb = _meta(small + big + BODY)["thumbnail"]
    assert (thumb["mime"], thumb["w"], thumb["b64"]) == ("image/jpeg", 300, b64)


def test_no_thumbnail_is_none():
    assert _meta(BODY)["thumbnail"] is None


def test_oversized_image_is_skipped():
    block, _ = _block("thumbnail", 900, 900, b"x" * (pc.THUMB_MAX_B64 + 10))
    assert _meta(block + BODY)["thumbnail"] is None


def test_unterminated_block_is_dropped():
    block, _ = _block("thumbnail", 100, 100, b"abc" * 40)
    cut = block.rsplit("; thumbnail end", 1)[0]
    assert _meta(cut + BODY)["thumbnail"] is None


def test_thumbnail_does_not_disturb_the_rest_of_the_meta():
    block, _ = _block("thumbnail", 100, 100, b"abc" * 40)
    colors, types, _n, used, _plan, _m = pc.parse_meta(
        pp, (block + BODY).splitlines(True))
    assert used == {0, 1}
    assert set(colors) == {0, 1}


def test_image_past_the_header_is_ignored():
    block, _ = _block("thumbnail", 100, 100, b"abc" * 40)
    filler = "; x\n" * pc.THUMB_SCAN_LINES
    assert _meta(filler + block + BODY)["thumbnail"] is None
