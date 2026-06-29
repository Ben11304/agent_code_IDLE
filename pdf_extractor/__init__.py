"""
Paper PDF Extractor package.

Main public API:
    from pdf_extractor import extract_paper
    extract_paper(pdf_path, output_dir, paper_id=None, dpi=150)
"""

from .extract import extract_paper, get_device, extract_with_pymupdf

__all__ = ["extract_paper", "get_device", "extract_with_pymupdf"]