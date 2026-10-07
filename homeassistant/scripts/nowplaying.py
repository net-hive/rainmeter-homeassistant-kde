#!/usr/bin/env python3
"""Publish now-playing metadata and album art for the HA Now Playing skin.

Home Assistant already knows what every media player is doing. Rainmeter
cannot ask it directly without an access token living in a plain-text skin
file, so this script runs *inside* HA (as a `command_line` sensor) and drops
two files into `www/`, which HA serves at `/local/` with no authentication:

    <name>.json         title, artist, album, state, source, accent colour
    <name>_cover.png    the artwork, re-encoded, at a stable path

Nothing secret ever leaves HA: `/local/` is unauthenticated by design, and
these two files contain only what is already on screen.

The accent colour is the interesting part. It is picked from the artwork's
pixels by a hue histogram weighted toward vivid colour, so the skin can tint
text to match the album without looking washed out. See pick_accent().

Source-agnostic: it takes whatever entity_picture / title / artist it is
handed, so Music Assistant, Spotify, Sonos, Cast, Plex, Jellyfin, Squeezebox,
a Universal media player, or anything else exposing the standard media_player
attributes all work identically. See docs/sources.md.

Usage (see homeassistant/packages/rainmeter_nowplaying.yaml for the wiring):

    nowplaying.py --www /config/www --name nowplaying \
        --ha-base http://127.0.0.1:8123 \
        --picture "/api/media_player_proxy/media_player.x?token=..." \
        --title-b64 <b64> --artist-b64 <b64> --album-b64 <b64> \
        --source-b64 <b64> --state playing

Text arrives base64-encoded so quotes, backslashes, emoji and CJK survive the
trip through the shell intact. This is not optional - track titles are hostile
input, and a stray apostrophe in a band name will otherwise truncate the
command.

Prints the JSON payload on stdout so the calling command_line sensor can use
it directly via json_attributes.
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import hashlib
import io
import json
import math
import os
import sys
import urllib.request

# --- accent tuning ----------------------------------------------------------
# The accent is picked per pixel: a hue histogram in which every pixel counts
# by its chroma squared (max-min of RGB - zero for black, white and grey), so
# vivid colour dominates and muddy shading barely registers. The peak hue wins
# and the accent is the average of the pixels around it.
#
# This replaced scoring a 12-colour quantised palette on HLS saturation. HLS
# rates near-black as fully saturated (#020001 is S=1.0), so on dark covers a
# speck of black won and was lifted into a neon tint - The Witcher 3's
# black-and-red cover came out hot pink - and quantising averaged small vivid
# accents away into the dark mass.
ACCENT_HUE_BINS = 36            # 10 degrees each
ACCENT_MIN_CHROMA = 0.18        # below this a pixel is black/white/grey
ACCENT_MIN_VALUE = 0.20         # and anything this dark is too
ACCENT_MIN_SHARE = 0.006        # under 0.6% vivid pixels the cover is neutral
ACCENT_SPREAD = 1.5             # bins either side of the peak that form the colour
# Floors, so washed-out art still produces something readable against a dark
# panel rather than near-black text.
ACCENT_LIGHT_MIN = 0.58
ACCENT_LIGHT_MAX = 0.82
ACCENT_SAT_MIN = 0.45

FALLBACK_RGB = (235, 235, 235)
FETCH_TIMEOUT = 10

# Default cover size. The skin draws the art at 156px, so 320 is already 2x
# oversampled - and small matters more than it looks.
#
# Rainmeter's WebParser downloads to a FIXED path. If a read stalls part way
# through, Rainmeter keeps that file handle open indefinitely: the partial file
# can never be replaced, it stops decoding, and nothing short of restarting
# Rainmeter clears it. The panel then shows title and artist correctly with a
# blank square where the art should be.
#
# That is not hypothetical - it happened on a Wi-Fi-connected machine once the
# covers grew past half a megabyte, wedging at 192 KB of a 289 KB transfer.
# A tenth of the bytes is a tenth of the exposure. Raise --cover-max if you
# scale the skin up, but know what you are trading.
COVER_MAX_DEFAULT = 320
# 256 colours is visually indistinguishable at this size and roughly halves the
# file again.
COVER_COLOURS = 256


def b64arg(value: str) -> str:
    """Decode a base64 CLI argument, tolerating empties and bad padding."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - never crash the sensor over a title
        return ""


def pick_accent(img) -> tuple[int, int, int]:
    """Choose a vibrant, readable accent colour from the artwork.

    Neutral art (black, white, grey, sepia) has no hue worth tinting with, so
    it gets FALLBACK_RGB rather than an invented colour.
    """
    small = img.copy()
    small.thumbnail((96, 96))
    raw = small.tobytes()
    bins = [0.0] * ACCENT_HUE_BINS
    samples = []
    for i in range(0, len(raw), 3):
        r, g, b = raw[i] / 255, raw[i + 1] / 255, raw[i + 2] / 255
        hi, lo = max(r, g, b), min(r, g, b)
        chroma = hi - lo
        if chroma < ACCENT_MIN_CHROMA or hi < ACCENT_MIN_VALUE:
            continue
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        w = chroma * chroma
        bins[int(h * ACCENT_HUE_BINS) % ACCENT_HUE_BINS] += w
        samples.append((h, s, v, w))
    if len(samples) < max(12, (len(raw) // 3) * ACCENT_MIN_SHARE):
        return FALLBACK_RGB

    # Light smoothing so a hue sitting on a bin edge is not split in two.
    n = ACCENT_HUE_BINS
    smooth = [bins[i - 1] * 0.5 + bins[i] + bins[(i + 1) % n] * 0.5 for i in range(n)]
    peak = (max(range(n), key=smooth.__getitem__) + 0.5) / n

    sx = sy = sw = ss = sv = 0.0
    for h, s, v, w in samples:
        d = abs(h - peak)
        if min(d, 1 - d) <= ACCENT_SPREAD / n:
            sx += w * math.cos(2 * math.pi * h)
            sy += w * math.sin(2 * math.pi * h)
            sw += w
            ss += w * s
            sv += w * v
    hue = (math.atan2(sy, sx) / (2 * math.pi)) % 1.0
    _, light, sat = colorsys.rgb_to_hls(*colorsys.hsv_to_rgb(hue, ss / sw, sv / sw))
    light = min(max(light, ACCENT_LIGHT_MIN), ACCENT_LIGHT_MAX)
    sat = max(sat, ACCENT_SAT_MIN)
    return tuple(int(round(v * 255)) for v in colorsys.hls_to_rgb(hue, light, sat))


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def write_atomic(path: str, data: bytes) -> None:
    """Write via a temp file and rename.

    Rainmeter polls these paths continuously and must never read a half-written
    file - a torn PNG shows as a broken image until the next track.
    """
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--www", default="/config/www", help="HA www folder")
    ap.add_argument("--name", default="nowplaying", help="output basename")
    ap.add_argument(
        "--ha-base",
        default="http://127.0.0.1:8123",
        help="base URL used to resolve relative entity_picture paths, and "
        "baked into cover_url for Rainmeter. Use the address the Rainmeter "
        "PC can reach, e.g. http://192.168.1.10:8123",
    )
    ap.add_argument("--picture", default="", help="entity_picture (absolute or /api/...)")
    ap.add_argument("--title-b64", default="")
    ap.add_argument("--artist-b64", default="")
    ap.add_argument("--album-b64", default="")
    ap.add_argument("--source-b64", default="", help="friendly name of the player")
    ap.add_argument("--state", default="unknown")
    ap.add_argument(
        "--cover-max",
        type=int,
        default=COVER_MAX_DEFAULT,
        help="longest edge of the published cover in px (default %d). See the "
        "comment by COVER_MAX_DEFAULT before raising it." % COVER_MAX_DEFAULT,
    )
    args = ap.parse_args()

    title = b64arg(args.title_b64)
    artist = b64arg(args.artist_b64)
    album = b64arg(args.album_b64)
    source = b64arg(args.source_b64)

    json_path = os.path.join(args.www, f"{args.name}.json")
    cover_name = f"{args.name}_cover.png"
    cover_path = os.path.join(args.www, cover_name)

    payload = {
        "title": title,
        "artist": artist,
        "album": album,
        "source": source,
        "state": args.state,
        "ver": "none",
        "art": "none",
        "rgb": list(FALLBACK_RGB),
        "hex": "#%02x%02x%02x" % FALLBACK_RGB,
        # Rainmeter's FontColor wants bare "r,g,b" - handing it a preformatted
        # string keeps arithmetic out of the skin.
        "rgb_rm": "%d,%d,%d" % FALLBACK_RGB,
        "cover_url": "",
    }

    def emit(code: int = 0) -> int:
        try:
            write_atomic(
                json_path, json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(f"cannot write {json_path}: {err}\n")
            return 1
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return code

    picture = (args.picture or "").strip()
    if not picture or picture in ("None", "unknown", "unavailable"):
        payload["art"] = "no-art"
        return emit()

    try:
        from PIL import Image
    except ImportError:
        # Pillow ships with the official HA container. On a bare venv install
        # the metadata still works, just without artwork or an accent.
        payload["art"] = "err:pillow-missing"
        return emit()

    # Spotify and friends hand back absolute https URLs; HA-proxied artwork is
    # a relative /api/... path that needs the base prefixed.
    url = picture if picture.startswith("http") else args.ha_base.rstrip("/") + picture
    try:
        data = fetch(url)
    except Exception as err:  # noqa: BLE001 - degrade, never crash the sensor
        payload["art"] = f"err:fetch:{type(err).__name__}"
        return emit()

    # Cache-buster derived from the image bytes, so Rainmeter re-downloads
    # exactly when the artwork really changed - not on every poll, and not
    # never. Some players serve every cover from one fixed URL, which is why
    # keying off the URL alone does not work.
    payload["ver"] = hashlib.sha1(data).hexdigest()[:10]

    try:
        from PIL import ImageOps

        img = Image.open(io.BytesIO(data)).convert("RGB")
        accent = pick_accent(img)
        payload.update(
            rgb=list(accent),
            hex="#%02x%02x%02x" % accent,
            rgb_rm="%d,%d,%d" % accent,
        )
        # Square, and never upscaled: enlarging a small cover adds no detail,
        # it only adds bytes for the download to stall on. Rainmeter scales it
        # to the art box either way.
        side = min(args.cover_max, min(img.size))
        square = ImageOps.fit(img, (side, side), Image.LANCZOS, centering=(0.5, 0.5))
        buf = io.BytesIO()
        square.convert("P", palette=Image.ADAPTIVE, colors=COVER_COLOURS).save(
            buf, "PNG", optimize=True
        )
        write_atomic(cover_path, buf.getvalue())
        payload["art"] = "ok"
        payload["cover_url"] = "%s/local/%s?v=%s" % (
            args.ha_base.rstrip("/"),
            cover_name,
            payload["ver"],
        )
    except Exception as err:  # noqa: BLE001
        payload["art"] = f"err:decode:{type(err).__name__}"

    return emit()


if __name__ == "__main__":
    sys.exit(main())
