#!/usr/bin/env python3
"""Compare what ImageIO decoded from the encoder's output with what went in.

    check_facts.py <facts of exported stickers> <facts of the source images> <expected.json> [IMPORT...]

Both facts files come from ci/inspect_images.swift. Each IMPORT says what one
sticker in the export was made from, as NUMBER=SOURCE:MAXEDGE:up or :noup, for
example "2=anim.gif:768:noup" for sticker 2 imported with --no-upscale.
"""
import json
import math
import sys

# role, longest side (None = --max-edge, "preview" = max(--max-edge, 320)), may enlarge
STILL_ROLES = [
    ("still", None, True),
    ("keyboard", 100, False),
    ("stillVariant_320", "preview", True),
    ("stillVariant_160", 160, False),
    ("stillVariant_96", 96, False),
    ("stillVariant_64", 64, False),
    ("stillVariant_40", 40, False),
]

OPAQUE_ALPHA = 250      # at least this
CLEAR_ALPHA = 6         # at most this
COLOR_SLACK = 48        # lossy HEVC, per channel
STRIPE_ALPHA_SLACK = 20
STRIPE_COLOR_SLACK = 64
STRIPE_MIN_HEIGHT = 100  # below this the stripe is only a few pixels tall
DELAY_SLACK = 0.011


def fit(width, height, edge, upscale):
    """The encoder's sizing rule (Swift rounds halves away from zero)."""
    scale = edge / float(max(width, height))
    if not upscale and scale > 1.0:
        scale = 1.0
    return (max(1, int(math.floor(width * scale + 0.5))),
            max(1, int(math.floor(height * scale + 0.5))))


def even(size):
    return tuple(max(2, side - side % 2) for side in size)


class Report:
    def __init__(self):
        self.failures = []
        self.checked = 0

    def check(self, where, what, ok, detail):
        self.checked += 1
        if not ok:
            self.failures.append("{}: {} -> {}".format(where, what, detail))

    def pixel(self, where, label, got, want, alpha_slack, color_slack):
        if want[3] == 0:
            self.check(where, label + " is transparent", got[3] <= CLEAR_ALPHA, got)
            return
        if want[3] == 255:
            self.check(where, label + " is opaque", got[3] >= OPAQUE_ALPHA, got)
        else:
            self.check(where, label + " alpha near {}".format(want[3]),
                       abs(got[3] - want[3]) <= alpha_slack, got)
        close = all(abs(got[i] - want[i]) <= color_slack for i in range(3))
        self.check(where, label + " colour near {}".format(want[:3]), close, got)


def samples_of(frame):
    return dict((item["name"], item["rgba"]) for item in frame["samples"])


def frame_delay(frame):
    return frame.get("unclampedDelay", frame.get("delay"))


def check_frames(report, where, facts, want_frames, size):
    report.check(where, "decodes", not facts.get("error"), facts.get("error"))
    report.check(where, "frame count {}".format(len(want_frames)),
                 facts["frameCount"] == len(want_frames), facts["frameCount"])
    for frame, want in zip(facts["frames"], want_frames):
        spot = "{} frame {}".format(where, frame["index"])
        report.check(spot, "size {}x{}".format(*size),
                     (frame["width"], frame["height"]) == size, (frame["width"], frame["height"]))
        got = samples_of(frame)
        report.pixel(spot, "block", got["center"], want["center"], 0, COLOR_SLACK)
        report.pixel(spot, "corner", got["corner"], want["corner"], 0, COLOR_SLACK)
        if want["stripe"][3] == 0 or size[1] >= STRIPE_MIN_HEIGHT:
            report.pixel(spot, "stripe", got["stripe"], want["stripe"], STRIPE_ALPHA_SLACK, STRIPE_COLOR_SLACK)
        if want["delay"] is not None:
            delay = frame_delay(frame)
            ok = delay is not None and abs(delay - want["delay"]) <= DELAY_SLACK
            report.check(spot, "delay {}s".format(want["delay"]), ok, delay)


def as_written(frames):
    """The frames the encoder writes for these source frames.

    A sequence written by ImageIO plays its last frame for as long as the one
    before it. Where those two delays differ, the encoder writes the last
    picture twice at half its delay each, so the picture is still on screen for
    the time the source asked for.
    """
    frames = [dict(frame) for frame in frames]
    if len(frames) >= 2 and abs(frames[-1]["delay"] - frames[-2]["delay"]) > 0.0005:
        frames[-1]["delay"] = max(frames[-1]["delay"] / 2.0, 0.005)
        frames.append(dict(frames[-1]))
    return frames


def parse_import(text):
    """'2=anim.gif:768:noup' -> (2, 'anim.gif', 768, False, False)

    A trailing ':only' means the sticker was imported with --animation-only.
    """
    number, rest = text.split("=", 1)
    only = rest.endswith(":only")
    if only:
        rest = rest[:-len(":only")]
    name, edge, scaling = rest.rsplit(":", 2)
    if scaling not in ("up", "noup"):
        raise ValueError("expected :up or :noup in {!r}".format(text))
    return int(number), name, int(edge), scaling == "up", only


def main():
    facts_path, source_path, expected_path = sys.argv[1:4]
    imported = dict((entry[0], entry[1:]) for entry in map(parse_import, sys.argv[4:]))
    with open(facts_path) as handle:
        exported = dict((item["file"], item) for item in json.load(handle))
    with open(source_path) as handle:
        sources = dict((item["file"], item) for item in json.load(handle))
    with open(expected_path) as handle:
        expected = json.load(handle)
    canvas = tuple(expected["canvas"])
    report = Report()

    # 1. ImageIO must read the source images the way the generator wrote them,
    #    in particular the APNG's frames and delays.
    for name, spec in sorted(expected["fixtures"].items()):
        facts = sources.get(name)
        report.check("source " + name, "was inspected", facts is not None, "missing")
        if facts:
            check_frames(report, "source " + name, facts, spec["frames"], canvas)
            if len(spec["frames"]) > 1:
                report.check("source " + name, "loops forever", facts.get("loopCount") == 0, facts.get("loopCount"))

    # 2. What the tool stored must be the same picture, in the right containers.
    expected_files = set()
    for number, (name, edge, upscale, animation_only) in sorted(imported.items()):
        want_frames = expected["fixtures"][name]["frames"]
        if len(want_frames) == 1:
            for position, (role, role_edge, role_upscale) in enumerate(STILL_ROLES):
                if role_edge is None:
                    role_edge = edge
                elif role_edge == "preview":
                    role_edge = max(edge, 320)
                size = fit(canvas[0], canvas[1], role_edge, role_upscale and upscale)
                filename = "sticker{}_{}_{}.heic".format(number, position, role)
                expected_files.add(filename)
                facts = exported.get(filename)
                report.check(filename, "was exported", facts is not None, "missing")
                if facts:
                    report.check(filename, "is HEIC", facts["type"] == "public.heic", facts["type"])
                    check_frames(report, filename, facts, want_frames, size)
        else:
            size = even(fit(canvas[0], canvas[1], edge, upscale))
            if not animation_only:
                # The animation comes with a still of its first frame, at the
                # animation's size, and a small keyboard preview.
                first_frame = [dict(want_frames[0], delay=None)]
                for position, role, role_size in (
                        (0, "still", size), (1, "keyboard", fit(canvas[0], canvas[1], 100, False))):
                    filename = "sticker{}_{}_{}.heic".format(number, position, role)
                    expected_files.add(filename)
                    facts = exported.get(filename)
                    report.check(filename, "was exported", facts is not None, "missing")
                    if facts:
                        report.check(filename, "is HEIC", facts["type"] == "public.heic", facts["type"])
                        check_frames(report, filename, facts, first_frame, role_size)
            filename = "sticker{}_2_animated.heics".format(number)
            expected_files.add(filename)
            facts = exported.get(filename)
            report.check(filename, "was exported", facts is not None, "missing")
            if facts:
                report.check(filename, "is an HEIF sequence", facts["type"] == "public.heics", facts["type"])
                report.check(filename, "loops forever", facts.get("loopCount") == 0, facts.get("loopCount"))
                check_frames(report, filename, facts, as_written(want_frames), size)
                print("{}: {}x{}, source delays {} -> stored delays {}".format(
                    filename, size[0], size[1], [frame["delay"] for frame in want_frames],
                    [frame_delay(frame) for frame in facts["frames"]]))
    extra = sorted(set(exported) - expected_files)
    report.check("export", "holds nothing unexpected", not extra, extra)

    print("{} checks, {} failed".format(report.checked, len(report.failures)))
    for line in report.failures:
        print("FAIL  " + line)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
