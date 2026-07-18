#!/bin/bash
#SBATCH --job-name=bench_extract
#SBATCH --partition=nextgen
#SBATCH --account=pgs0407
#SBATCH --qos=ascend-default
#SBATCH --time=03:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --output=jobs/logs/bench_%j.out
#SBATCH --error=jobs/logs/bench_%j.err

# Batch-extract the AECPlayGround fulltext_corpus → extracted_paper using the
# PyMuPDF extractor (run_corpus.py). Resumable (skip-if-done), error-isolated.
#
# Submit:
#   sbatch jobs/run_corpus_extract.sh
# Status:
#   squeue -u $USER
# Tail:
#   tail -f jobs/logs/corpus_<jobid>.out

set -euo pipefail

VENV_PY=/fs/scratch/PGS0407/binben14/envs/paper_parser_venv/bin/python
SCRIPT_DIR=/users/PGS0407/binben14/VietHuy/agent_code_IDLE/pdf_extractor

CORPUS=/users/PGS0407/binben14/VietHuy/AECPlayGround-AGENT/RESEARCHER/outputs/benchmark
OUTPUT=/users/PGS0407/binben14/VietHuy/AECPlayGround-AGENT/RESEARCHER/outputs/benchmark/extracted

cd "$SCRIPT_DIR"

echo "=== AEC corpus extraction ==="
echo "job   : $SLURM_JOB_ID  node: $SLURM_NODELIST  cpus: $SLURM_CPUS_PER_TASK"
echo "start : $(date)"
echo "corpus: $CORPUS"
echo "output: $OUTPUT"
echo "venv  : $VENV_PY ($($VENV_PY --version 2>&1))"
echo "------------------------------------------------------------"

# workers < cpus to leave headroom for pdfplumber/PyMuPDF RAM spikes on big PDFs
$VENV_PY run_corpus.py \
  --corpus "$CORPUS" \
  --output "$OUTPUT" \
  --workers 12 \
  --dpi 150

echo "------------------------------------------------------------"
echo "done  : $(date)"
