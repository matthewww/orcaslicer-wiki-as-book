"""Build a printable, hand-bindable book from selected OrcaSlicer Wiki pages.

    python build.py             # book.toml -> out/book.pdf (reading copy) + out/book-imposed.pdf (sheets to print)
    python build.py --serve     # live preview as spreads in the browser; refresh to rebuild
    python build.py -c volumes/*.toml     # several configs, built one after another
    python build.py --proof 9   # impose pages 9-16 of an already built book onto 2 sheets for a test print

Pipeline: wiki Markdown -> one print HTML -> Paged.js (in Edge via Playwright) -> PDF -> pypdf imposition.
"""
import argparse
import datetime
import html
import posixpath
import re
import subprocess
import textwrap
import threading
import tomllib
import urllib.parse
import webbrowser
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import markdown
import segno
from pygments.formatters import HtmlFormatter
from pymdownx.slugs import slugify
from pymdownx.superfences import fence_div_format

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SITE = "https://www.orcaslicer.com/wiki/"

SIZES = {"A5": (148, 210), "A4": (210, 297)}       # mm; an imposed sheet holds two pages side by side
SHEET = {"A5": "A4", "A4": "A3"}
# chapter_start -> CSS break-before. "fit" continues on the same page unless less than chapter_min_space is left.
BREAKS = {"none": "auto", "fit": "auto", "page": "page", "right": "right"}
MARGINS = {"A5": (12, 14, 17, 11), "A4": (15, 18, 22, 14)}  # top, bottom, inner (spine), outer

GH_IMG = re.compile(r"https://github\.com/OrcaSlicer/OrcaSlicer_WIKI/(?:blob|raw)/main/(images/[^?)\"'\s]+)(?:\?raw=true)?")
GH_ICON = re.compile(r"https://github\.com/OrcaSlicer/OrcaSlicer/blob/main/([^?)\"'\s]+)\?raw=true")
ALERT = re.compile(r"^> ?\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*\n((?:>.*(?:\n|$))*)", re.M)
MINI_TOC = re.compile(r"(?:^[ \t]*[-*] \[[^\]]+\]\(#[^)]+\)[ \t]*\n)+", re.M)  # in-page link lists: noise on paper
# Note: regexes don't skip fenced code; a wiki link inside a code block would be rewritten too
LINK = re.compile(r"(?<!!)\[([^\[\]]*)\]\(([^)\s]+)\)")
LEGEND_MIN = 25  # links to one page at least this often in a book -> treated as option labels
H1 = re.compile(r"^# (.+)$", re.M)
YT = r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})[^\"]*"
YT_LINK = re.compile(r'<a ([^>]*?)href="' + YT + r'"([^>]*)>(.*?)</a>', re.S)

GH_SLUG = slugify(case="lower")


def load_config(path):
    cfg = tomllib.loads(path.read_text("utf-8"))
    lay = cfg.setdefault("layout", {})
    lay.setdefault("page", "A5")
    lay.setdefault("signature_sheets", 4)
    lay.setdefault("duplex_flip", "short")
    lay.setdefault("chapter_start", "none")
    lay.setdefault("chapter_min_space", 0.33)
    lay.setdefault("font_size", "8pt")
    lay.setdefault("toc_depth", 2)
    lay.setdefault("image_max", 0.26)
    lay.setdefault("margins_mm", MARGINS[lay["page"]])
    if lay["page"] not in SIZES:
        raise SystemExit(f"layout.page must be one of {list(SIZES)}")
    cfg["wiki"] = (path.parent / cfg.get("wiki", "../OrcaSlicer_WIKI")).resolve()
    cfg["name"] = path.stem
    # Configs in the same folder that set `volume` form a set: links into a sibling volume say so
    cfg["elsewhere"] = {}
    if "volume" in cfg:
        for sib in sorted(path.parent.glob("*.toml")):
            other = tomllib.loads(sib.read_text("utf-8"))
            if sib != path and "volume" in other:
                cfg["elsewhere"].update({pid: other["volume"] for part in other.get("part", []) for pid in part["pages"]})
    return cfg


def index_wiki(wiki):
    skip = {"wiki", "docs", ".git", "node_modules"}
    return {p.stem: p for p in wiki.rglob("*.md") if not skip & set(p.relative_to(wiki).parts)}


# ---------- Markdown -> HTML ----------

def alerts_to_admonitions(md):
    def sub(m):
        body = re.sub(r"^> ?", "", m[2], flags=re.M)
        return f'!!! {m[1].lower()} "{m[1].title()}"\n{textwrap.indent(body, "    ")}\n'
    return ALERT.sub(sub, md)


def link_targets(md, pid):
    """(link text, wiki page id) per link on this page; external links and same-page anchors excluded."""
    return [(text, t.partition("#")[0]) for text, t in LINK.findall(md)
            if not re.match(r"[a-z][a-z0-9+.-]*:", t, re.I) and t.partition("#")[0] not in ("", pid)]


def find_legends(links, pages):
    """Pages linked often and almost always with the same text (e.g. [Mode], [Type]) are option-label
    legends, not references. Returns ({page id}, {page id: link count})."""
    by_page = {}
    for text, page in links:
        by_page.setdefault(page, Counter())[text] += 1
    hits = {page: sum(texts.values()) for page, texts in by_page.items()}
    legends = {page for page, texts in by_page.items()
               if page in pages and hits[page] >= LEGEND_MIN and texts.most_common(1)[0][1] >= 0.8 * hits[page]}
    return legends, hits


def rewrite_links(md, pid, included, pages, wiki, elsewhere, legends=frozenset()):
    def sub(m):
        text, target = m[1], m[2]
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):
            return f"[{text}]({target}){{.ext}}"
        page, _, frag = target.partition("#")
        page = page or pid
        if page in legends:  # option labels like [Mode]: explained once in the imprint, not on every use
            return f"[{text}](#{page}){{.legend}}"
        if page in included:
            anchor = f"{page}--{frag}" if frag else page
            return f"[{text}](#{anchor}){{.xref}}"
        if page in pages:  # wiki page not in the book: point at the website
            rel = pages[page].relative_to(wiki).with_suffix("").as_posix()
            url = SITE + urllib.parse.quote(rel) + "/" + (f"#{frag}" if frag else "")
            if page in elsewhere:
                return f'[{text}]({url}){{.xvol data-vol="{elsewhere[page]}"}}'
            return f"[{text}]({url}){{.ext}}"
        return m[0]
    return LINK.sub(sub, md)


def rewrite_images(md):
    md = GH_IMG.sub(lambda m: "/wiki/" + urllib.parse.quote(m[1]), md)
    return GH_ICON.sub(r"https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/main/\1", md)


def render_page(pid, path, included, pages, wiki, elsewhere, legends):
    """Returns (title, html, [(id, h2 title)])."""
    md = path.read_text("utf-8")
    if not H1.search(md):
        md = f"# {pid.replace('_', ' ').title()}\n\n{md}"
    md = MINI_TOC.sub("", md)
    md = alerts_to_admonitions(md)
    md = rewrite_images(md)
    md = rewrite_links(md, pid, included, pages, wiki, elsewhere, legends)

    conv = markdown.Markdown(
        extensions=["tables", "toc", "attr_list", "md_in_html", "admonition", "pymdownx.details",
                    "pymdownx.superfences", "pymdownx.arithmatex", "pymdownx.highlight", "pymdownx.inlinehilite"],
        extension_configs={
            "toc": {"slugify": lambda value, sep: f"{pid}--{GH_SLUG(value, sep)}"},
            "pymdownx.superfences": {"custom_fences": [{"name": "mermaid", "class": "mermaid", "format": fence_div_format}]},
            "pymdownx.arithmatex": {"generic": True},
            "pymdownx.highlight": {"use_pygments": True},
        },
    )
    body = conv.convert(md)
    h1 = next(t for t in conv.toc_tokens if t["level"] == 1) if conv.toc_tokens else None
    title = html.unescape(h1["name"]) if h1 else H1.search(md)[1]
    body = body.replace("<h1 ", '<h1 class="chapter-title" ', 1)
    body = add_video_qr(body)
    sections = [(c["id"], html.unescape(c["name"])) for t in conv.toc_tokens for c in t["children"] if c["level"] == 2]
    return title, body, sections


def qr_svg(video_id):
    return segno.make(f"https://youtu.be/{video_id}", error="m").svg_inline(dark="#2b2b2b", border=2, omitsize=True)


def add_video_qr(body):
    """Thumbnail links become a compact card with a QR; text links get a small QR in the margin side. One QR per video per page."""
    seen = set()

    def card(m):
        attrs, vid, rest, inner = m[1], m[2], m[3], m[4]
        if "<img" not in inner:
            return m[0]
        seen.add(vid)
        label = re.search(r'aria-label="([^"]+)"', attrs + rest)
        label = html.escape(label[1]) if label else "Video"
        return (f'<span class="video-card"><a {attrs}href="https://youtu.be/{vid}"{rest}>{inner}</a>'
                f'<span class="video-text">{label}<br><span class="video-url">youtu.be/{vid}</span></span>'
                f'<span class="qr">{qr_svg(vid)}</span></span>')

    def inline(m):
        vid = m[2]
        if vid in seen or "video-card" in m[0]:
            return m[0]
        seen.add(vid)
        return f'<span class="qr qr-inline">{qr_svg(vid)}</span>{m[0]}'

    return YT_LINK.sub(inline, YT_LINK.sub(card, body))


def chapter_fit_css(lay, text_height_mm):
    """For chapter_start = "fit": the unbreakable chapter heading carries invisible padding as tall as the
    space a chapter needs to open on this page; a matching negative margin pulls the text back up under it.
    If that space isn't left, the heading (and so the chapter) moves to the next page."""
    if lay["chapter_start"] != "fit":
        return ""
    need = round(text_height_mm * lay["chapter_min_space"])
    return f".chapter-head {{ padding-bottom: {need}mm; margin-bottom: -{need}mm; }}"


def legend_note(legends, hits, included, pages, cfg):
    """One imprint line saying where each option label (Mode, Type, ...) is explained."""
    if not legends:
        return ""
    items = []
    for pid in sorted(legends, key=lambda p: -hits[p]):
        md = pages[pid].read_text("utf-8")
        title = html.escape(H1.search(md)[1] if H1.search(md) else pid.replace("_", " ").title())
        if pid in included:
            items.append(f'<a class="xref" href="#{pid}">{title}</a>')
        elif pid in cfg["elsewhere"]:
            items.append(f'{title} (Vol. {cfg["elsewhere"][pid]})')
        else:
            items.append(f"{title} (orcaslicer.com/wiki)")
    return f'<p>Option labels such as <em>Mode</em> and <em>Type</em> are explained in: {", ".join(items)}.</p>'


def git_stamp(wiki):
    try:
        out = subprocess.run(["git", "-C", str(wiki), "log", "-1", "--format=%h %cs"],
                             capture_output=True, text=True, check=True).stdout.split()
        return f"commit {out[0]} ({out[1]})"
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "an unknown revision"


def build_html(cfg):
    wiki, lay = cfg["wiki"], cfg["layout"]
    pages = index_wiki(wiki)
    included = [p for part in cfg["part"] for p in part["pages"]]
    missing = [p for p in included if p not in pages]
    if missing:
        raise SystemExit(f"Unknown wiki page id(s) in config: {', '.join(missing)}")

    # A page linked this often is a legend (the generated [Mode]/[Type] labels), not a reference
    legends, hits = find_legends([l for pid in included for l in link_targets(pages[pid].read_text("utf-8"), pid)], pages)

    esc = html.escape
    toc, body = [], []
    chapter = part_no = 0
    for part in cfg["part"]:
        if part.get("title"):
            part_no += 1
            body.append(f'<section class="part" id="part-{part_no}"><p class="part-num">Part {part_no}</p>'
                        f'<h1 class="part-title">{esc(part["title"])}</h1></section>')
            toc.append(f'<li class="toc-part"><a href="#part-{part_no}"><span class="t">{esc(part["title"])}</span></a></li>')
        for pid in part["pages"]:
            chapter += 1
            title, page_html, sections = render_page(pid, pages[pid], set(included), pages, wiki, cfg["elsewhere"], legends)
            # Number and title travel together so a chapter never opens with a stranded label
            page_html = page_html.replace('<h1 class="chapter-title"', f'<div class="chapter-head"><p class="chapter-num">Chapter {chapter}</p><h1 class="chapter-title"', 1)
            page_html = page_html.replace("</h1>", "</h1></div>", 1)
            body.append(f'<section class="chapter" id="{pid}">{page_html}</section>')
            toc.append(f'<li class="toc-ch"><a href="#{pid}"><span class="n">{chapter}</span><span class="t">{esc(title)}</span></a></li>')
            if lay["toc_depth"] >= 2:
                toc += [f'<li class="toc-sec"><a href="#{sid}"><span class="t">{esc(name)}</span></a></li>' for sid, name in sections]

    w, h = SIZES[lay["page"]]
    top, bottom, inner, outer = lay["margins_mm"]
    img_max = round((h - top - bottom) * lay["image_max"])
    page_css = f"""
@page {{ size: {w}mm {h}mm; margin: {top}mm {outer}mm {bottom}mm {inner}mm; }}
@page :left {{ margin-left: {outer}mm; margin-right: {inner}mm; }}
@page :right {{ margin-left: {inner}mm; margin-right: {outer}mm; }}
:root {{ font-size: {lay["font_size"]}; --img-max: {img_max}mm; --text-h: {h - top - bottom - 8}mm; }}
.chapter {{ break-before: {BREAKS[lay["chapter_start"]]}; }}
{chapter_fit_css(lay, h - top - bottom)}
"""
    fonts = "".join(f'<link rel="stylesheet" href="/node_modules/@fontsource/{f}.css">' for f in (
        "source-serif-4/400", "source-serif-4/400-italic", "source-serif-4/600", "source-serif-4/600-italic",
        "source-sans-3/400", "source-sans-3/600", "jetbrains-mono/400"))
    today = datetime.date.today().isoformat()
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{esc(cfg["title"])} – {esc(cfg.get("subtitle", ""))}</title>
{fonts}
<link rel="stylesheet" href="/node_modules/katex/dist/katex.min.css">
<link rel="stylesheet" href="/print.css">
<style>{page_css}{HtmlFormatter(style="friendly").get_style_defs(".highlight")}</style>
<script>window.PagedConfig = {{ auto: false }};</script>
<script src="/node_modules/pagedjs/dist/paged.polyfill.min.js"></script>
<script src="/node_modules/katex/dist/katex.min.js"></script>
<script src="/node_modules/katex/dist/contrib/auto-render.min.js"></script>
<script src="/node_modules/mermaid/dist/mermaid.min.js"></script>
<script>window.BOOK = {{ textWidthMm: {w - inner - outer}, imgMaxMm: {img_max} }};</script>
<script src="/layout.js"></script>
</head><body>
<section class="cover">
  <img src="/wiki/web_extras/OrcaSlicer.png" alt="OrcaSlicer">
  <h1>{esc(cfg["title"])}</h1>
  <p class="subtitle">{esc(cfg.get("subtitle", ""))}</p>
  {f'<p class="volume">Volume {cfg["volume"]}</p>' if "volume" in cfg else ""}
  <p class="source">From the OrcaSlicer Wiki</p>
</section>
<section class="imprint">
  <p>Compiled from the OrcaSlicer Wiki (github.com/OrcaSlicer/OrcaSlicer_WIKI) at {git_stamp(wiki)}, built {today}.</p>
  <p>Content by the OrcaSlicer contributors. The online wiki at orcaslicer.com/wiki is the current version.</p>
  {legend_note(legends, hits, included, pages, cfg)}
</section>
<nav class="toc"><h1>Contents</h1><ol>{"".join(toc)}</ol></nav>
{"".join(body)}
</body></html>"""
    OUT.mkdir(exist_ok=True)
    (OUT / f"{cfg['name']}.html").write_text(doc, "utf-8")


# ---------- Serving (Paged.js needs http, not file://) ----------

class Handler(SimpleHTTPRequestHandler):
    cfg = None
    live = False

    def translate_path(self, path):
        clean = posixpath.normpath(urllib.parse.unquote(urllib.parse.urlsplit(path).path))
        if clean.startswith("/wiki/"):
            return str(self.cfg["wiki"] / clean.removeprefix("/wiki/"))
        return super().translate_path(path)

    def do_GET(self):
        if self.live and urllib.parse.urlsplit(self.path).path == f"/out/{self.cfg['name']}.html":
            try:
                build_html(self.cfg)
            except SystemExit as e:  # bad config: show it instead of a stale book
                self.send_error(500, str(e))
                return
        super().do_GET()

    def log_message(self, *args):
        pass


def serve(cfg, port, live):
    Handler.cfg, Handler.live = cfg, live
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(HERE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/out/{cfg['name']}.html"


# ---------- PDF and imposition ----------

def render_pdf(url, pdf):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page()
        page.on("response", lambda r: r.status >= 400 and not r.url.endswith("favicon.ico")
                and print(f"  missing ({r.status}): {r.url}"))
        page.goto(url, wait_until="commit")  # big books load images for minutes; bookReady below is the real wait
        page.wait_for_function("window.bookReady === true", timeout=1_200_000, polling=500)
        errors = page.evaluate("window.bookLayoutErrors")
        lost = page.evaluate("window.bookChapters.filter(id => !document.querySelector(`.pagedjs_pages [id='${id}']`))")
        if errors or lost:  # Paged.js gave up part-way; a PDF now would silently be missing content
            browser.close()
            raise SystemExit(f"Layout failed. Paged.js could not place: {errors or 'n/a'}. "
                             f"Chapters missing from the output: {lost or 'none'}")
        page.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True)
        browser.close()


def signature_layout(n_pages, sheets_per_sig):
    """Yields, per signature, a list of sheets as ((front_left, front_right), (back_left, back_right)).
    Indices are 0-based book pages; None is a blank. The last signature shrinks to fit (multiple of 4)."""
    per = 4 * sheets_per_sig
    for start in range(0, n_pages, per):
        count = min(per, n_pages - start)
        count += -count % 4
        sig = [start + k if start + k < n_pages else None for k in range(count)]
        yield [((sig[-1 - 2 * s], sig[2 * s]), (sig[2 * s + 1], sig[-2 - 2 * s])) for s in range(count // 4)]


def impose(src, dst, sheets_per_sig, flip, first=0, count=None):
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation
    pages = list(PdfReader(src).pages)[first:None if count is None else first + count]
    w, h = float(pages[0].mediabox.width), float(pages[0].mediabox.height)
    out, sigs = PdfWriter(), []
    for sig in signature_layout(len(pages), sheets_per_sig):
        for front, back in sig:
            for side, pair in enumerate((front, back)):
                sheet = PageObject.create_blank_page(width=2 * w, height=h)
                for x, idx in zip((0, w), pair):
                    if idx is not None:
                        sheet.merge_transformed_page(pages[idx], Transformation().translate(x, 0))
                if side == 1 and flip == "long":  # long-edge duplex turns the back upside down
                    sheet.rotate(180)
                out.add_page(sheet)
        sigs.append(sig)
    out.write(dst)
    return len(pages), sigs


def report(cfg, n, sigs):
    lay, name = cfg["layout"], cfg["name"]
    sheets = sum(len(s) for s in sigs)
    blanks = sum(4 * len(s) for s in sigs) - n
    print(f"\nout/{name}.pdf  {n} {lay['page']} pages (reading copy; view as two-page with cover)")
    print(f"out/{name}-imposed.pdf  {sheets} {SHEET[lay['page']]} sheets, {len(sigs)} signature(s), {blanks} blank page(s) at the end")
    page_no = 1
    for i, sig in enumerate(sigs, 1):
        print(f"    signature {i}: pages {page_no}-{page_no + 4 * len(sig) - 1}, {len(sig)} sheet(s)")
        page_no += 4 * len(sig)


def build(cfg):
    lay, name = cfg["layout"], cfg["name"]
    build_html(cfg)
    server, url = serve(cfg, 0, live=False)
    print(f"[{name}] laying out pages (Paged.js in Edge)...")
    render_pdf(url, OUT / f"{name}.pdf")
    server.shutdown()
    report(cfg, *impose(OUT / f"{name}.pdf", OUT / f"{name}-imposed.pdf", lay["signature_sheets"], lay["duplex_flip"]))


def proof(cfg, first_page):
    """Pages first_page..+7 of the built book as one 2-sheet signature, for a test print."""
    lay, name = cfg["layout"], cfg["name"]
    src = OUT / f"{name}.pdf"
    if not src.exists():
        raise SystemExit(f"Build {name} first: out/{name}.pdf is missing")
    dst = OUT / f"{name}-proof.pdf"
    n, _ = impose(src, dst, 2, lay["duplex_flip"], first=first_page - 1, count=8)
    print(f"out/{dst.name}: pages {first_page}-{first_page + n - 1} on 2 {SHEET[lay['page']]} sheets (4 sides). "
          f"Print double-sided, flip on {lay['duplex_flip']} edge, fold both sheets together.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", nargs="+", default=[HERE / "book.toml"], type=Path)
    ap.add_argument("--serve", action="store_true", help="live browser preview of the first config; refresh to rebuild")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--proof", type=int, metavar="PAGE", help="impose 8 pages from PAGE of the built book for a test print")
    args = ap.parse_args()
    configs = [load_config(c.resolve()) for c in args.config]

    if args.serve:
        _, url = serve(configs[0], args.port, live=True)
        print(f"Preview: {url}\nEdit the config / print.css / wiki pages, then refresh. Ctrl+C to stop.")
        webbrowser.open(url)
        threading.Event().wait()

    for cfg in configs:
        if args.proof:
            proof(cfg, args.proof)
        else:
            build(cfg)

    if not args.proof:
        lay = configs[0]["layout"]
        print(f"\nPrint the -imposed.pdf on {SHEET[lay['page']]} at actual size (100%, no fit-to-page), double-sided, "
              f"flip on {lay['duplex_flip']} edge.\nKeep each signature's sheets together in output order, fold the stack in half, "
              f"then stack the folded signatures in order and sew through each fold.")


if __name__ == "__main__":
    main()
