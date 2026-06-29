# Paper PDF Extractor (PyMuPDF based)

Dedicated, lightweight tool for extracting scientific papers into clean Markdown + figures + structured tables.

**Why this?**
- Reliable text extraction
- Extracts figures by **rendering the exact page region** at the chosen DPI (not raw XObject bytes). This keeps the full visual "nguyên" — including any text labels, subfigure letters (a/b), legends, numbers, and overlays that belong to the figure.
- Reasonable table extraction (with post-cleaning)
- No heavy VLM by default → fast, low resource, no hallucination risk in extraction
- Easy to call from AgentUI / multi-agent system (exactly as a tool the agent calls instead of reading raw PDF)

## Environment (clean, on scratch)

```bash
# The dedicated venv (created on scratch to avoid home quota)
VENV_PY=/fs/scratch/PGS0407/binben14/envs/paper_parser_venv/bin/python

# Activate / use directly
$VENV_PY --version
```

Packages inside: pymupdf, pdfplumber, tqdm, pillow (minimal).

## Usage as script (recommended for jobs)

```bash
$VENV_PY extract.py \
  --pdf "input_test/s42452-025-06745-4 (2).pdf" \
  --output_dir "output/my_paper" \
  --paper_id "s42452-025-06745-4" \
  --dpi 150
```

## Usage as Python function (for agents / wrapper)

```python
import sys
sys.path.insert(0, "/users/PGS0407/binben14/VietHuy/agent_code_IDLE/pdf_extractor")

from extract import extract_paper

result = extract_paper(
    pdf_path="input_test/s42452-025-06745-4 (2).pdf",
    output_dir="output/my_paper",
    paper_id="s42452-025-06745-4",
    dpi=150
)
print(result)  # {"figures": N, "tables": M}
```

The function is the wrapped main method.

## Output format (consistent with what you like)

```
output/my_paper/
├── full_text.md           # full paper as markdown (with page markers)
├── figures/               # extracted images (fig_pX_....png)
├── figures_metadata.json
├── tables.json            # list of extracted tables (cleaned)
└── metadata.json
```

In `full_text.md` you will also find a nice markdown version of important tables (e.g. Wagner classification) even if the auto extractor was partial.

## For pitzer / cluster jobs

See `jobs/run_extractor_pitzer.sh`

Simple example sbatch (adapt paths):

```bash
#!/bin/bash
#SBATCH --job-name=pdf_extract
#SBATCH --partition=gpu-exp
#SBATCH --account=pgs0407
#SBATCH --time=02:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G

VENV_PY=/fs/scratch/PGS0407/binben14/envs/paper_parser_venv/bin/python
cd /users/PGS0407/binben14/VietHuy/agent_code_IDLE/pdf_extractor

$VENV_PY extract.py --pdf "$1" --output_dir "output/$2" --paper_id "$2" --dpi 150
```

Submit example:
```bash
sbatch -M pitzer jobs/run_extractor_pitzer.sh "input/xxx.pdf" "paper_xxx"
```

## Notes

- No torch required (device detection falls back to cpu gracefully).
- If you later want optional VLM enhancement for tables/figures, you can extend the function (we had that before but removed for simplicity and reliability).
- The function is pure and side-effect free except for writing to output_dir.

Run it, the output will look like the one you liked in `/users/PGS0407/binben14/VietHuy/agent_code_IDLE/pdf_extractor/output`.
