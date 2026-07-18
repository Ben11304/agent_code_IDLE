#!/usr/bin/env python3
"""
Batch runner: extract every PDF in a corpus folder into clean Markdown + figures + tables.

Thin, resumable, error-isolated wrapper around extract.extract_paper (PyMuPDF based).

Usage:
    VENV_PY=/fs/scratch/PGS0407/binben14/envs/paper_parser_venv/bin/python
    $VENV_PY run_corpus.py --corpus <in> --output <out> [--workers N] [--dpi 150]
                            [--limit N] [--force] [--copy_md]

Design:
- One output subdir per PDF stem:  <output>/<stem>/{full_text.md, figures/, *.json}
- Resumable: a stem whose full_text.md already exists is skipped (unless --force).
- Error-isolated: a single failing PDF is logged to <output>/<stem>/.FAILED and does
  not abort the batch.
- .md full-text objects (papers obtained as markdown because the raw PDF was blocked)
  are copied verbatim to <output>/<stem>/full_text.md when --copy_md is set; INDEX.md
  and other non-paper markdown are skipped (only files whose name endswith _fulltext.md).
- Parallel: PDFs are processed through a ProcessPoolExecutor; the per-page rendering is
  CPU-bound so N workers on N cores is a near-linear speedup.
"""

import argparse
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Make extract.py importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import extract_paper  # noqa: E402


def is_done(out_dir: Path) -> bool:
    return (out_dir / "full_text.md").exists()


def process_one(args_tuple):
    """Worker: extract one PDF. Returns (stem, status, detail). Runs in a pool process."""
    pdf_path, output_root, dpi, force = args_tuple
    pdf_path = Path(pdf_path)
    stem = pdf_path.stem  # corpus filename stem (symlinks keep the same name)
    out_dir = Path(output_root) / stem

    if out_dir.exists():
        # clean a stale failure marker if we are re-attempting
        fail_marker = out_dir / ".FAILED"
        if is_done(out_dir) and not force:
            return (stem, "skip", "full_text.md already present")
        if fail_marker.exists() and not force:
            return (stem, "skip", "previously failed (remove .FAILED or use --force to retry)")

    out_dir.mkdir(parents=True, exist_ok=True)
    # remove a prior failure marker before a fresh attempt
    (out_dir / ".FAILED").unlink(missing_ok=True)

    try:
        res = extract_paper(
            pdf_path=str(pdf_path),
            output_dir=str(out_dir),
            paper_id=stem,
            dpi=dpi,
        )
        # sanity: the extractor must have written the markdown
        if not is_done(out_dir):
            raise RuntimeError("extract_paper returned but full_text.md was not written")
        return (stem, "ok", f"figures={res.get('figures')} tables={res.get('tables')}")
    except Exception as e:  # isolate failure
        tb = traceback.format_exc()
        (out_dir / ".FAILED").write_text(
            f"{type(e).__name__}: {e}\n\n{tb}\n", encoding="utf-8"
        )
        # best-effort: keep a partial full_text.md if it exists, else note absence
        return (stem, "fail", f"{type(e).__name__}: {e}")


def copy_md_papers(corpus: Path, output: Path, force: bool):
    """Copy *_fulltext.md papers (raw PDF unobtainable) as full_text.md."""
    done = []
    for md in sorted(corpus.glob("*_fulltext.md")):
        stem = md.stem.replace("_fulltext", "")  # CAND-18_CaptionPS_IEEE-11488027
        out_dir = output / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "full_text.md"
        if dst.exists() and not force:
            done.append((stem, "skip", "md full_text.md already present"))
            continue
        shutil.copy2(md, dst)
        # minimal metadata so downstream tooling sees a uniform shape
        meta = out_dir / "metadata.json"
        if not meta.exists():
            import json
            meta.write_text(json.dumps({
                "paper_id": stem,
                "source": str(md),
                "source_kind": "markdown_fulltext",
                "note": "Raw PDF was not obtainable (paywalled/JS-gated). "
                        "Full text captured as markdown by RESEARCHER; no figures/tables extracted.",
            }, indent=2), encoding="utf-8")
        done.append((stem, "ok", "copied markdown full text"))
    return done


def main():
    ap = argparse.ArgumentParser(description="Batch-extract a corpus of paper PDFs.")
    ap.add_argument("--corpus", required=True, help="input folder of PDFs (+ *_fulltext.md)")
    ap.add_argument("--output", required=True, help="output folder (one subdir per stem)")
    ap.add_argument("--workers", type=int, default=6, help="parallel extract workers")
    ap.add_argument("--dpi", type=int, default=150, help="figure render DPI")
    ap.add_argument("--limit", type=int, default=0, help="process only first N PDFs (0 = all)")
    ap.add_argument("--force", action="store_true", help="re-extract even if full_text.md exists")
    ap.add_argument("--copy_md", action="store_true", default=True,
                    help="copy *_fulltext.md papers (default on)")
    ap.add_argument("--no_md", dest="copy_md", action="store_false")
    args = ap.parse_args()

    corpus = Path(args.corpus).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(p for p in corpus.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
    # p.is_file() follows symlinks -> True only if the target exists
    broken = sorted(p for p in corpus.iterdir()
                    if p.suffix.lower() == ".pdf" and p.is_symlink() and not p.exists())
    if broken:
        print(f"[WARN] {len(broken)} broken symlink(s) skipped: "
              f"{', '.join(p.name for p in broken)}")

    if args.limit > 0:
        pdfs = pdfs[: args.limit]

    print(f"[INFO] corpus    : {corpus}")
    print(f"[INFO] output    : {output}")
    print(f"[INFO] PDFs      : {len(pdfs)}   workers={args.workers}   dpi={args.dpi}")
    print(f"[INFO] force     : {args.force}")

    # 1) markdown-only papers first (cheap, sequential)
    md_results = []
    if args.copy_md:
        md_results = copy_md_papers(corpus, output, args.force)
        for stem, status, detail in md_results:
            print(f"  [md:{status}] {stem} — {detail}")

    # 2) PDFs in parallel
    results = []
    t0 = time.time()
    work = [(p, output, args.dpi, args.force) for p in pdfs]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_one, w): w[0].stem for w in work}
        for i, fut in enumerate(as_completed(futures), 1):
            stem, status, detail = fut.result()
            results.append((stem, status, detail))
            tag = {"ok": "OK ", "skip": "skp", "fail": "ERR"}[status]
            print(f"  [{i}/{len(work)}] {tag} {stem} — {detail}")

    dt = time.time() - t0
    ok = sum(1 for _, s, _ in results if s == "ok")
    skip = sum(1 for _, s, _ in results if s == "skip")
    fail = [(s, d) for s, st, d in results if st == "fail"]
    md_ok = sum(1 for _, s, _ in md_results if s == "ok")

    print("\n================ SUMMARY ================")
    print(f" PDFs  : {len(pdfs)} total | {ok} ok | {skip} skipped | {len(fail)} failed")
    print(f" MD    : {len(md_results)} | {md_ok} copied")
    print(f" time  : {dt/60:.1f} min   workers={args.workers}")
    if fail:
        print(" FAILED PDFs:")
        for stem, detail in fail:
            print(f"   - {stem}: {detail}")
            print(f"       see {output}/{stem}/.FAILED")
    print("========================================")


if __name__ == "__main__":
    main()
