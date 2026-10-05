// Runs in the browser before Paged.js paginates. window.BOOK = { textWidthMm, imgMaxMm } comes from build.py.

const ICON_PX = 64;        // images sized below this are inline icons, not figures
const MM_PER_PX = 25.4 / 96;

// Figures: any image outside tables/video cards, unless the wiki sized it as a small icon
function markFigures() {
    for (const img of document.querySelectorAll(".chapter img")) {
        if (img.closest("table, .video-card")) continue;
        const sized = Number(img.getAttribute("height") || img.getAttribute("width")) || 0;
        if (sized && sized < ICON_PX) continue;
        img.classList.add("fig");
        // Respect a height the wiki asked for, but never exceed the figure cap
        if (img.getAttribute("height")) {
            img.dataset.capPx = img.getAttribute("height");
            img.style.maxHeight = `min(var(--img-max), ${img.dataset.capPx}px)`;
        }
        img.removeAttribute("height");
        img.removeAttribute("width");
    }
}

const isGap = (node) => node.nodeType === Node.TEXT_NODE ? !node.textContent.trim() : node.nodeName === "BR";

// Figures separated only by line breaks share one row
function rowAdjacentFigures() {
    for (const img of document.querySelectorAll("img.fig")) {
        if (img.closest(".figs")) continue;
        const run = [img];
        for (let n = img.nextSibling; n && (isGap(n) || n.classList?.contains("fig")); n = n.nextSibling)
            if (n.nodeName === "IMG") run.push(n);
        if (run.length < 2) continue;
        const row = document.createElement("span");
        row.className = "figs";
        img.before(row);
        for (const fig of run) {
            while (row.nextSibling !== fig) row.nextSibling.remove();  // drop the <br>s between figures
            row.append(fig);
        }
    }
}

// A paragraph holding only figures (no text)
const isFigureParagraph = (el) =>
    el?.tagName === "P" && el.textContent.trim() === "" && el.querySelector("img.fig") && !el.querySelector("img:not(.fig)");

// Consecutive figure-only paragraphs share one row
function mergeFigureParagraphs() {
    for (const p of document.querySelectorAll("p")) {
        if (!p.isConnected || !isFigureParagraph(p)) continue;
        while (isFigureParagraph(p.nextElementSibling)) p.append(...p.nextElementSibling.childNodes), p.nextElementSibling.remove();
        if (p.querySelectorAll("img").length < 2) continue;
        p.querySelectorAll("br").forEach((br) => br.remove());
        p.querySelectorAll(".figs").forEach((span) => span.replaceWith(...span.childNodes));
        p.classList.add("figs");
    }
}

// A lone figure that would fill under half the text width floats beside the text instead
function floatNarrowFigures() {
    const { textWidthMm, imgMaxMm } = window.BOOK;
    for (const img of document.querySelectorAll("img.fig")) {
        if (img.closest(".figs, .admonition") || !img.naturalWidth) continue;
        const capMm = Math.min(imgMaxMm, (img.dataset.capPx || Infinity) * MM_PER_PX);
        const shownWidthMm = Math.min(textWidthMm, capMm * img.naturalWidth / img.naturalHeight);
        if (shownWidthMm < textWidthMm * 0.5) img.classList.add("float"), keepFloatWithItsText(img);
    }
}

// A float placed after its text would drift beside the next block. When text precedes it in the
// same list item or paragraph, move it to the start of that block and make the block contain it.
function keepFloatWithItsText(img) {
    const block = img.closest("li, p");
    if (!block || block.classList.contains("figs") || !block.textContent.trim()) return;
    let box = block;
    if (block.tagName === "LI") {
        box = document.createElement("div");
        box.append(...block.childNodes);
        block.append(box);
    }
    box.classList.add("float-box");
    box.prepend(img);
}

// A rendered diagram becomes one image: Paged.js can't split an SVG, and trying aborts the whole layout
function diagramsToImages() {
    for (const svg of document.querySelectorAll(".mermaid svg")) {
        const img = document.createElement("img");
        img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(svg));
        img.alt = "diagram";
        img.style.width = `${svg.viewBox.baseVal.width}px`;
        svg.replaceWith(img);
    }
}

// Paged.js reports a node it can't place with console.warn, then silently drops everything after it.
// Collect those so build.py can fail loudly instead of writing a short book.
window.bookLayoutErrors = [];
const warn = console.warn;
console.warn = (...args) => {
    if (String(args[0]).includes("Unable to layout")) {
        const el = args[1]?.nodeType === 1 ? args[1] : args[1]?.parentElement;
        window.bookLayoutErrors.push(`${el?.tagName} ${(el?.textContent || "").trim().slice(0, 80)}`);
    }
    warn(...args);
};

window.addEventListener("load", async () => {
    renderMathInElement(document.body, {
        throwOnError: false,
        delimiters: [{ left: "\\[", right: "\\]", display: true }, { left: "\\(", right: "\\)", display: false }],
    });
    mermaid.initialize({ startOnLoad: false, theme: "neutral", htmlLabels: false, flowchart: { htmlLabels: false } });
    await mermaid.run({ querySelector: ".mermaid" });
    diagramsToImages();
    window.bookChapters = [...document.querySelectorAll(".chapter[id]")].map((c) => c.id);
    await Promise.all([...document.images].map((img) => img.decode().catch(() => {})));
    markFigures();
    rowAdjacentFigures();
    mergeFigureParagraphs();
    floatNarrowFigures();
    await document.fonts.ready;
    await PagedPolyfill.preview();
    document.head.insertAdjacentHTML("beforeend", '<link rel="stylesheet" href="/preview.css">');
    window.bookReady = true;
});
