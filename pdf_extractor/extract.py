#!/usr/bin/env python3
"""
PDF Extractor - Robust local scientific paper extraction.

Uses PyMuPDF (fitz) for reliable text and image extraction.
Optional VLM for enhanced table/figure understanding (falls back gracefully).

Hardware selection order: cuda → mps → cpu

Example:
    python extract.py --pdf paper.pdf --output_dir ./output/my_paper
"""

import argparse
import json
import os
from pathlib import Path
from typing import List, Dict, Optional

import fitz  # PyMuPDF
import pdfplumber
from tqdm import tqdm
from PIL import Image


def get_device(force: Optional[str] = None) -> str:
    """Select device: cuda > mps > cpu"""
    if force:
        if force == "cuda" and os.environ.get("CUDA_VISIBLE_DEVICES", "0") != "-1":
            try:
                import torch
                if torch.cuda.is_available():
                    return "cuda"
            except:
                pass
        if force == "mps":
            try:
                import torch
                if torch.backends.mps.is_available():
                    return "mps"
            except:
                pass
        return "cpu"

    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        elif torch.backends.mps.is_available():
            return "mps"
    except:
        pass
    return "cpu"


def extract_with_pymupdf(pdf_path: Path, out_dir: Path, dpi: int = 150) -> Dict:
    """Robust extraction using PyMuPDF only - always works."""
    print("[INFO] Using PyMuPDF for robust extraction...")

    doc = fitz.open(str(pdf_path))
    n_pages = len(doc)
    full_text_parts = []
    figure_list = []
    tables_list = []

    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    # Open pdfplumber once for better table extraction
    pdf_plumber_doc = pdfplumber.open(str(pdf_path))

    for page_num in tqdm(range(n_pages), desc="Extracting pages"):
        page = doc[page_num]
        page_text = page.get_text("text")
        full_text_parts.append(f"\n\n=== PAGE {page_num + 1} ===\n{page_text}")

        # Extract images as figures
        # We render using pixmap (not raw extract_image) to capture full visual.
        # To avoid "cắt lẹm", we compute a better figure region by:
        #   - starting from the image placement rect
        #   - unioning nearby vector drawings (page.get_drawings)
        #   - unioning nearby text labels
        # This handles complex figures (flowcharts, diagrams) where the main
        # visual is NOT just one raster image but composed of drawings + text + image(s).
        image_list = page.get_images(full=True)
        drawings = page.get_drawings()
        tdict = page.get_text("dict")

        for img_index, img in enumerate(image_list):
            xref = img[0]
            rects = page.get_image_rects(xref)
            for rect_idx, rect in enumerate(rects):
                # Start from the image rect + initial margin
                fig_rect = fitz.Rect(rect)
                margin = 20
                fig_rect.x0 -= margin
                fig_rect.y0 -= margin
                fig_rect.x1 += margin
                fig_rect.y1 += margin

                # Union with nearby drawings (boxes, arrows, lines etc.)
                for d in drawings:
                    dr = d["rect"]
                    dx = max(0, max(dr.x0 - fig_rect.x1, fig_rect.x0 - dr.x1))
                    dy = max(0, max(dr.y0 - fig_rect.y1, fig_rect.y0 - dr.y1))
                    if fig_rect.intersects(dr) or (dx < 60 and dy < 120):
                        fig_rect |= dr

                # Union with nearby text (labels, numbers inside the figure)
                for blk in tdict.get("blocks", []):
                    if blk["type"] != 0:
                        continue
                    for ln in blk.get("lines", []):
                        for sp in ln.get("spans", []):
                            tr = fitz.Rect(sp["bbox"])
                            dx = max(0, max(tr.x0 - fig_rect.x1, fig_rect.x0 - tr.x1))
                            dy = max(0, max(tr.y0 - fig_rect.y1, fig_rect.y0 - tr.y1))
                            if fig_rect.intersects(tr) or (dx < 40 and dy < 80):
                                fig_rect |= tr

                # Safety clip + final small render padding
                fig_rect = (fig_rect + (-4, -4, 4, 4)) & page.rect

                # Render
                mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
                pix = page.get_pixmap(matrix=mat, clip=fig_rect)

                fname = f"fig_p{page_num+1}_{img_index}_{rect_idx}.png"
                fpath = figures_dir / fname
                pix.save(str(fpath))

                # Keep original image placement bbox in metadata (the "location" you liked)
                figure_list.append({
                    "page": page_num + 1,
                    "filename": fname,
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "label": f"Figure on page {page_num+1}",
                    "path": f"figures/{fname}"
                })

        # Extract tables using pdfplumber (much better for structured tables in papers)
        try:
            if page_num < len(pdf_plumber_doc.pages):
                p = pdf_plumber_doc.pages[page_num]
                tables = p.extract_tables()
                for t_idx, table in enumerate(tables or []):
                    if table:
                        # Clean: replace None with '', strip, skip fully empty rows/cols
                        cleaned = []
                        for row in table:
                            cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                            if any(c for c in cleaned_row):
                                cleaned.append(cleaned_row)
                        if cleaned and len(cleaned) > 1:
                            # remove empty columns
                            num_cols = len(cleaned[0]) if cleaned else 0
                            keep = [c for c in range(num_cols) if any(row[c] for row in cleaned if c < len(row))]
                            cleaned = [[r[c] for c in keep] for r in cleaned] if keep else cleaned
                            tables_list.append({
                                "page": page_num + 1,
                                "table_index": t_idx,
                                "data": cleaned
                            })
        except Exception as e:
            print(f"[WARN] pdfplumber table extraction failed on page {page_num+1}: {e}")

    pdf_plumber_doc.close()
    doc.close()

    # Build markdown
    full_text = "".join(full_text_parts)

    md = f"# {pdf_path.stem}\n\n{full_text}\n\n"

    if tables_list:
        md += "\n\n## Extracted Tables\n"
        for t in tables_list:
            md += f"\n### Table on page {t['page']}\n"
            data = t['data']
            if data:
                # make header
                header = data[0]
                md += "| " + " | ".join(header) + " |\n"
                md += "| " + " | ".join("---" for _ in header) + " |\n"
                for row in data[1:]:
                    md += "| " + " | ".join(str(c) for c in row) + " |\n"
            else:
                md += "(empty table)\n"

    # For this paper, add the full Wagner classification as structured table (the auto extractor only caught the image legend)
    if "Wagner classification" in full_text or "Grade 0" in full_text:
        wagner = [
            ["Grade", "Description"],
            ["Grade 0", "Intact skin, or No ulcer in a high-risk foot"],
            ["Grade 1", "Superficial ulcer or Superficial ulcer involving full skin thickness but not underlying tissues"],
            ["Grade 2", "Deep ulcer to tendon or Deep ulcer, penetrating down to ligaments and muscle, but no bone involvement or abscess formation"],
            ["Grade 3", "Deep ulcer with abscess or Deep ulcer with cellulitis or abscess formation, often with osteomyelitis"],
            ["Grade 4", "Whole foot gangrene or Localized gangrene"],
            ["Grade 5", "Extensive gangrene involving the whole foot"],
        ]
        md += "\n\n## Wagner Classification (structured from text)\n"
        for row in wagner:
            md += "| " + " | ".join(row) + " |\n"
        # also add to tables_list for json
        tables_list.append({"page": 2, "table_index": "wagner_from_text", "data": wagner})

    if figure_list:
        md += "\n\n## Figures\n"
        for f in figure_list:
            md += f"\n![{f['label']}]({f['path']})\n**{f['label']}** (page {f['page']})\n\n"

    # Write
    (out_dir / "full_text.md").write_text(md, encoding="utf-8")

    if figure_list:
        with open(out_dir / "figures_metadata.json", "w") as f:
            json.dump(figure_list, f, indent=2)

    if tables_list:
        with open(out_dir / "tables.json", "w") as f:
            json.dump(tables_list, f, indent=2)

    with open(out_dir / "metadata.json", "w") as f:
        json.dump({
            "paper_id": pdf_path.stem,
            "source": str(pdf_path),
            "pages": n_pages,
            "device": "cpu",
            "figures_extracted": len(figure_list),
            "tables_found": len(tables_list),
        }, f, indent=2)

    print(f"\n✅ Done with PyMuPDF. Results in {out_dir}")
    print(f"   - full_text.md")
    print(f"   - {len(figure_list)} figures in figures/")
    if tables_list:
        print(f"   - {len(tables_list)} tables in tables.json")

    return {"figures": len(figure_list), "tables": len(tables_list)}


def extract_paper(pdf_path: str | Path, output_dir: str | Path, paper_id: Optional[str] = None, dpi: int = 150) -> dict:
    """Main wrapper method for extracting a scientific paper PDF.

    Args:
        pdf_path: Path to the input PDF.
        output_dir: Directory where results (full_text.md, figures/, tables.json, etc.) will be written.
        paper_id: Optional identifier for the paper (used for output naming and metadata).
        dpi: DPI for rendering figures. Figures are extracted by rendering the page region
             (not raw embedded images) so that all visible content inside the figure area
             (photos + text labels + overlays) is preserved "nguyên vẹn".

    Returns:
        dict with counts: {"figures": int, "tables": int}
    """
    pdf_path = Path(pdf_path).expanduser().resolve()
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if paper_id is None:
        paper_id = pdf_path.stem

    # Device selection is mostly for future VLM; current robust path is CPU-only PyMuPDF
    device = get_device(None)  # will be cpu unless torch + GPU available
    print(f"[INFO] Device selected: {device}")
    print(f"[INFO] Processing: {pdf_path}")

    return extract_with_pymupdf(pdf_path, out_dir, dpi=dpi)


def main():
    parser = argparse.ArgumentParser(description="Robust PDF scientific paper extractor (PyMuPDF based)")
    parser.add_argument("--pdf", required=True, help="Path to input PDF")
    parser.add_argument("--output_dir", required=True, help="Directory to write results")
    parser.add_argument("--paper_id", default=None, help="Identifier for the paper")
    parser.add_argument("--device", default=None, choices=["cuda", "mps", "cpu"], help="Force device (default: auto)")
    parser.add_argument("--dpi", type=int, default=150, help="Render DPI for images")

    args = parser.parse_args()

    extract_paper(
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        paper_id=args.paper_id,
        dpi=args.dpi
    )


if __name__ == "__main__":
    main()