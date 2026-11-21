#!/usr/bin/env python3
"""Extract menu text from PDFs: text layer first, macOS Vision OCR as fallback.

Usage (from repo root, using the venv):
    .venv/bin/python scripts/extract_pdf.py data/pdf/bovas.pdf [more.pdf ...]

Writes data/raw/<pdfname>.txt and prints a JSON summary per file.
Needs: pymupdf, ocrmac (macOS only; uses the built-in Vision framework).
"""
import json
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pymupdf  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "raw"
PRICE = re.compile(r"\$?\s?\d{1,3}(?:\.\d{2})\b|\$\s?\d{1,3}")
MIN_TEXT = 80  # chars per page below which we treat a page as image-only


def ocr_page(page, tmp: Path) -> str:
    from ocrmac import ocrmac
    pix = page.get_pixmap(dpi=200)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(tmp))
    anns = ocrmac.OCR(str(tmp), recognition_level="accurate").recognize()
    # annotations: (text, confidence, bbox[x, y, w, h] normalized, origin bottom-left)
    anns.sort(key=lambda a: (-round(a[2][1] + a[2][3] / 2, 2), a[2][0]))
    rows, cur, cur_y = [], [], None
    for text, _conf, (x, y, w, h) in anns:
        yc = y + h / 2
        if cur_y is None or abs(yc - cur_y) < 0.012:
            cur.append((x, text))
            cur_y = yc if cur_y is None else cur_y
        else:
            rows.append(" ".join(t for _, t in sorted(cur)))
            cur, cur_y = [(x, text)], yc
    if cur:
        rows.append(" ".join(t for _, t in sorted(cur)))
    return "\n".join(rows)


def extract(path: Path) -> dict:
    doc = pymupdf.open(path)
    pages, methods = [], []
    for i, page in enumerate(doc):
        text = page.get_text().strip()
        if len(text) >= MIN_TEXT:
            pages.append(text)
            methods.append("text")
        else:
            pages.append(ocr_page(page, OUT / "ocr_tmp" / f"{path.stem}_p{i + 1}.png"))
            methods.append("ocr")
    full = "\n\n".join(pages)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{path.stem}.txt").write_text(full, encoding="utf-8")
    priced = [l.strip() for l in full.splitlines() if PRICE.search(l)]
    return {"file": path.name, "pages": len(doc), "methods": methods,
            "chars": len(full), "priced_lines": len(priced), "samples": priced[:6]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    print(json.dumps([extract(Path(p)) for p in sys.argv[1:]], indent=2))
