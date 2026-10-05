"""Run: .venv/Scripts/python test_build.py"""
from build import GH_SLUG, add_video_qr, find_legends, signature_layout

# Wiki links use GitHub anchors; "+" and "()" are dropped, spaces kept as separators
assert GH_SLUG("Archimedean chords + YOLO (Recommended)", "-") == "archimedean-chords--yolo-recommended"

# One 2-sheet signature (8 pages): outer sheet carries 8|1 on the front and 2|7 on the back
assert list(signature_layout(8, 2)) == [[((7, 0), (1, 6)), ((5, 2), (3, 4))]]

# 18 pages, 4-sheet signatures: one full 16-page signature, then a 1-sheet signature padded with blanks
sigs = list(signature_layout(18, 4))
assert [len(s) for s in sigs] == [4, 1]
assert sigs[1] == [((None, 16), (17, None))]

# Every page lands on exactly one sheet side
pages = [p for sig in signature_layout(37, 3) for sheet in sig for side in sheet for p in side if p is not None]
assert sorted(pages) == list(range(37))
# Videos: a thumbnail link becomes a card with a QR; a text link to the same video gets no second QR
html = ('<a class="orca-video-poster-link" href="https://www.youtube.com/watch?v=gVU5If1VsAM"><img src="t.jpg"></a>'
        '<a href="https://www.youtube.com/watch?v=gVU5If1VsAM">same video</a>'
        '<a href="https://youtu.be/F-In1N7AquQ">other video</a>')
out = add_video_qr(html)
assert out.count('class="video-card"') == 1 and out.count("<svg") == 2 and 'class="qr qr-inline"' in out
# Option labels repeat one text; real references vary. Only the former become legends.
labels = [("Mode", "option_mode")] * 30 + [(f"Pattern {i}", "infill") for i in range(30)]
legends, hits = find_legends(labels, {"option_mode": None, "infill": None})
assert legends == {"option_mode"} and hits["infill"] == 30
# Release cadence: one edition per minor, after it settles; long-lived minors get a refresh
from datetime import date
from release_check import pick
app = [("v2.4.0", date(2026, 6, 20)), ("v2.4.1", date(2026, 6, 28)), ("v2.4.2", date(2026, 7, 7)), ("v2.4.0-beta", date(2026, 6, 9))]
assert pick(app, [], date(2026, 7, 20)) is None                       # 13 days after 2.4.2: still settling
assert pick(app, [], date(2026, 7, 28)) == "v2.4.2"                   # settled: first 2.4 edition
assert pick(app, [("wiki-v2.4.2", date(2026, 7, 28))], date(2026, 10, 1)) is None   # already built
app23 = [("v2.3.0", date(2025, 3, 20)), ("v2.3.1", date(2025, 10, 5))]
assert pick(app23, [("wiki-v2.3.0", date(2025, 4, 10))], date(2025, 11, 1)) == "v2.3.1"  # edition 6 months old: refresh
assert pick(app23, [("wiki-v2.3.0", date(2025, 9, 1))], date(2025, 11, 1)) is None       # edition recent: skip
assert pick([("v1.6.4-beta3", date(2023, 8, 14))], [], date(2024, 1, 1)) is None         # beta tags ignored
print("ok")
