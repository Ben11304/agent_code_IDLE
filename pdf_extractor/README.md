# Paper PDF Extractor

CPU PyMuPDF extractor for paper text, rendered figure regions and candidate tables.
Checked against `extract.py` and `run_corpus.py` on 2026-09-07. This is a standalone
script/function, not an automatically registered AgentUI MCP tool.

## Setup and single-paper use

From this directory, create a dedicated environment and install the local requirements:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python extract.py --pdf /path/to/paper.pdf --output_dir output/paper --dpi 150
```

Dependencies: PyMuPDF, pdfplumber, tqdm, Pillow. Camelot is an optional fallback
when already installed; it is not in `requirements.txt`. Torch is optional device
detection only: extraction uses the CPU path and does not require a GPU.
No OCR/VLM stage is implemented, so scanned documents may have little usable text.

From repository root, the Python API is:

```python
from pdf_extractor.extract import extract_paper
counts = extract_paper('/path/to/paper.pdf', 'output/paper', dpi=150)
# {"figures": int, "tables": int}
```

`paper_id` / `--paper_id` are accepted but currently do not reach the extraction
writer: metadata uses the source filename stem, and output location is exactly
`output_dir`. `--device` is parsed but not passed to the wrapper; it does not select
a GPU extraction backend.

## Outputs

```text
output/paper/
├── full_text.md
├── metadata.json
├── figures/                 # rendered region PNGs
├── figures_metadata.json    # written only if figures were found
└── tables.json              # written only if tables were found
```

Figure regions are rendered from the page at the requested DPI, including text
inside the crop. Region detection is heuristic; complete figure/label coverage is
not guaranteed. Table extraction tries pdfplumber, optional Camelot, then text/layout
patterns. Layout patterns can mistake ordinary text for tables; inspect source pages.

**Existing hard-coded behavior:** if extracted text contains `Wagner classification`
or `Grade 0`, `extract.py` appends a fixed Wagner table and labels it page 2. That
content is not reconstructed/verified from the source PDF. Treat it as an injected
legacy table, not source evidence. This documentation audit records the behavior;
it does not modify the extractor or reproduce its medical content as guidance.

Reusing an output directory does not clear old conditional JSON/images, so stale
files can remain if a later extraction finds fewer figures/tables. Use a fresh
output directory when comparing results. The function writes files and prints logs;
it is not a pure side-effect-free function.

## Corpus runner

```bash
.venv/bin/python run_corpus.py \
  --corpus /path/to/corpus --output /path/to/extracted --workers 6 --dpi 150
```

It extracts top-level PDFs in parallel to one subdirectory per stem, skips completed
outputs, records failures in `.FAILED`, and copies matching `*_fulltext.md` sources
by default. Options include `--limit`, `--force`, `--no_md`. It is a batch CLI, not
an AgentUI scheduler. `--force` reprocesses; it does not clean every prior artifact.

## Site-specific SLURM wrappers

`jobs/run_extractor_pitzer.sh`, `run_corpus_extract.sh`, and `run_bench_extract.sh`
contain fixed OSC paths, environment locations, accounts/partitions and corpus
locations. Inspect/adapt them to the target host; their presence does not prove
those resources or the scratch venv exist today. Create the configured log folder
before submission. After `sbatch`, report job ID and return; use one-shot status
checks or AgentUI scheduled monitoring, not foreground polling loops.
