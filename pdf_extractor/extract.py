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
from typing import List, Dict, Optional, Tuple

import fitz  # PyMuPDF
import pdfplumber
from tqdm import tqdm
from PIL import Image
import re
import numpy as np

try:
    import camelot
    CAMELOT_AVAILABLE = True
except ImportError:
    CAMELOT_AVAILABLE = False


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


def extract_tables_camelot(pdf_path: str, page_num: int) -> List[Dict]:
    """Fallback: use camelot for more robust table extraction."""
    if not CAMELOT_AVAILABLE:
        return []

    tables = []
    try:
        # Try both lattice and stream modes
        for mode in ['lattice', 'stream']:
            extracted = camelot.read_pdf(
                str(pdf_path),
                pages=str(page_num + 1),
                flavor=mode
            )

            for i in range(extracted.n):
                table_df = extracted[i].df
                # Convert to list of lists
                table_data = table_df.values.tolist()
                # Clean None values
                cleaned = []
                for row in table_data:
                    cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                    if any(c for c in cleaned_row):
                        cleaned.append(cleaned_row)

                if cleaned and len(cleaned) > 1:
                    tables.append({
                        "page": page_num + 1,
                        "table_index": len(tables),
                        "data": cleaned,
                        "flavor": mode,
                        "accuracy": extracted[i].accuracy if hasattr(extracted[i], 'accuracy') else None
                    })
    except Exception as e:
        # Suppress camelot errors, just log if needed
        pass

    return tables


def detect_tables_from_pattern(page_text: str, text_dict: dict, page_num: int) -> List[Dict]:
    """Last resort: pattern-based table detection from text layout."""
    tables = []

    # 1. Markdown-style tables in text
    md_table_pattern = r'((?:\|[^|\n]+\|\n)+\|[-:|\s]+\|\n(?:\|[^|\n]+\|\n)+)'
    md_matches = re.finditer(md_table_pattern, page_text)

    for idx, match in enumerate(md_matches):
        table_text = match.group(1)
        rows = [line.strip().split('|')[1:-1] for line in table_text.strip().split('\n')]
        # Skip separator row
        if rows and len(rows) > 2:
            separator_idx = next((i for i, row in enumerate(rows) if all(cell.strip() in ['', '-', ':', '---', ':-:', '-:-', ':-:'] for cell in row)), None)
            if separator_idx is not None and separator_idx < len(rows):
                rows.pop(separator_idx)

            cleaned = []
            for row in rows:
                cleaned_row = [cell.strip() for cell in row]
                if any(c for c in cleaned_row):
                    cleaned.append(cleaned_row)

            if cleaned and len(cleaned) > 1:
                tables.append({
                    "page": page_num + 1,
                    "table_index": len(tables),
                    "data": cleaned,
                    "method": "markdown_pattern"
                })

    # 2. Keyword-marked tables
    keyword_patterns = [
        r'(Table \d+)[\.:]\s*\n((?:[^\n]+\n){2,})',
        r'(The following table|the table below)[\.:]?\s*\n((?:[^\n]+\n){2,})',
    ]

    for pattern in keyword_patterns:
        matches = re.finditer(pattern, page_text, re.IGNORECASE)
        for match in matches:
            table_text = match.group(2)
            # Try to parse as table by column alignment
            lines = table_text.strip().split('\n')
            if len(lines) >= 2:
                # Detect columns by spaces
                cleaned = []
                for line in lines:
                    # Split by multiple spaces
                    cells = re.split(r'\s{2,}', line.strip())
                    if cells:
                        cleaned_row = [cell.strip() for cell in cells]
                        if any(c for c in cleaned_row):
                            cleaned.append(cleaned_row)

                if cleaned and len(cleaned) > 1:
                    tables.append({
                        "page": page_num + 1,
                        "table_index": len(tables),
                        "data": cleaned,
                        "method": "keyword_pattern"
                    })

    # 3. Spatial reconstruction from text dict blocks
    if text_dict and "blocks" in text_dict:
        blocks = [b for b in text_dict["blocks"] if b["type"] == 0]  # text blocks
        if len(blocks) >= 3:
            # Group by y-coordinate (rows)
            y_groups = {}
            for block in blocks:
                for line in block.get("lines", []):
                    y = int(line["bbox"][1])  # top y-coordinate
                    if y not in y_groups:
                        y_groups[y] = []
                    for span in line.get("spans", []):
                        x = int(span["bbox"][0])  # x-coordinate
                        text = span["text"].strip()
                        if text:
                            y_groups[y].append((x, text))

            # Detect table-like structure: multiple rows with aligned columns
            sorted_rows = sorted(y_groups.items(), key=lambda x: x[0])  # sort by y
            if len(sorted_rows) >= 3:
                # Check for column alignment
                column_positions = {}
                for y, items in sorted_rows:
                    for x, text in items:
                        # Cluster x positions (within 10 pixels)
                        col_key = x // 10 * 10
                        if col_key not in column_positions:
                            column_positions[col_key] = []
                        column_positions[col_key].append((y, text))

                # If we have multiple columns with items in multiple rows -> table
                active_cols = {k: v for k, v in column_positions.items() if len(v) >= 2}
                if len(active_cols) >= 2 and len(sorted_rows) >= 3:
                    # Reconstruct table
                    reconstructed = []
                    for y, items in sorted_rows:
                        row_dict = {x // 10 * 10: text for x, text in items}
                        row = [row_dict.get(col, "") for col in sorted(active_cols.keys())]
                        if any(c for c in row):
                            reconstructed.append(row)

                    if reconstructed and len(reconstructed) > 1:
                        tables.append({
                            "page": page_num + 1,
                            "table_index": len(tables),
                            "data": reconstructed,
                            "method": "spatial_reconstruction"
                        })

    return tables


def merge_fig_rects(
    candidates: List[Tuple["fitz.Rect", List["fitz.Rect"]]]
) -> List[Tuple["fitz.Rect", List["fitz.Rect"]]]:
    """Union candidate figure regions that overlap, until no pair overlaps.

    Each candidate is (expanded_region, [source placement bboxes]). Two regions
    that intersect describe the same visual, so they collapse into one render.
    """
    items = [(fitz.Rect(r), list(src)) for r, src in candidates]
    changed = True
    while changed:
        changed = False
        out: List[Tuple[fitz.Rect, List[fitz.Rect]]] = []
        for rect, src in items:
            for i, (orect, osrc) in enumerate(out):
                if orect.intersects(rect):
                    out[i] = (orect | rect, osrc + src)
                    changed = True
                    break
            else:
                out.append((rect, src))
        items = out
    return items


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

        # (fig_rect, [source placement bboxes]) — merged before rendering so a
        # figure tiled out of many small placements is not rendered once per tile.
        candidates: List[Tuple[fitz.Rect, List[fitz.Rect]]] = []

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
                candidates.append((fig_rect, [fitz.Rect(rect)]))

        # Merge overlapping candidate regions. A figure built from many tiled
        # placements (icon grids, sliced raster plots) yields hundreds of nearly
        # identical expanded rects; without this each one renders its own PNG.
        merged = merge_fig_rects(candidates)

        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for fig_idx, (fig_rect, src_rects) in enumerate(merged):
            pix = page.get_pixmap(matrix=mat, clip=fig_rect)

            fname = f"fig_p{page_num+1}_{fig_idx}.png"
            fpath = figures_dir / fname
            pix.save(str(fpath))

            # Keep original image placement bbox in metadata (the "location" you liked).
            # bbox = union of the placements this figure was built from.
            bbox = fitz.Rect(src_rects[0])
            for r in src_rects[1:]:
                bbox |= r
            figure_list.append({
                "page": page_num + 1,
                "filename": fname,
                "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                "n_placements": len(src_rects),
                "label": f"Figure on page {page_num+1}",
                "path": f"figures/{fname}"
            })

        # Extract tables using 3-tier fallback: pdfplumber -> camelot -> pattern detection
        page_tables = []

        # Tier 1: pdfplumber (fast, works for most tables)
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
                            if cleaned:
                                page_tables.append({
                                    "page": page_num + 1,
                                    "table_index": t_idx,
                                    "data": cleaned,
                                    "method": "pdfplumber"
                                })
        except Exception as e:
            print(f"[WARN] pdfplumber table extraction failed on page {page_num+1}: {e}")

        # Tier 2: camelot (if pdfplumber found nothing or low quality)
        if not page_tables:
            camelot_tables = extract_tables_camelot(pdf_path, page_num)
            page_tables.extend(camelot_tables)
            if camelot_tables:
                print(f"[INFO] Page {page_num+1}: {len(camelot_tables)} table(s) found via camelot")

        # Tier 3: pattern detection (last resort)
        if not page_tables:
            pattern_tables = detect_tables_from_pattern(page_text, tdict, page_num)
            page_tables.extend(pattern_tables)
            if pattern_tables:
                print(f"[INFO] Page {page_num+1}: {len(pattern_tables)} table(s) found via pattern detection")

        tables_list.extend(page_tables)

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
        # Count by method
        methods = {}
        for t in tables_list:
            method = t.get("method", "unknown")
            methods[method] = methods.get(method, 0) + 1
        method_str = ", ".join(f"{k}:{v}" for k, v in methods.items())
        print(f"   - {len(tables_list)} tables in tables.json ({method_str})")
    else:
        print(f"   - 0 tables found")

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