#!/bin/bash
#SBATCH --job-name=pdf_extract
#SBATCH --partition=gpu-exp
#SBATCH --account=pgs0407
#SBATCH --time=02:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --output=jobs/logs/extract_%j.out
#SBATCH --error=jobs/logs/extract_%j.err

# Simple sbatch for pitzer using the dedicated scratch venv + PyMuPDF extractor
# Usage: sbatch -M pitzer --export=ALL jobs/run_extractor_pitzer.sh /path/to/pdf "paper_id"

set -euo pipefail

VENV_PY=/fs/scratch/PGS0407/binben14/envs/paper_parser_venv/bin/python
SCRIPT_DIR=/users/PGS0407/binben14/VietHuy/agent_code_IDLE/pdf_extractor

cd $SCRIPT_DIR

PDF=${1:-"input_test/s42452-025-06745-4 (2).pdf"}
PAPER_ID=${2:-"auto"}

echo "Running extractor on $PDF (id=$PAPER_ID) at $(date)"
echo "Using venv python: $VENV_PY"

$VENV_PY extract.py --pdf "$PDF" --output_dir "output/$PAPER_ID" --paper_id "$PAPER_ID" --dpi 150

echo "Done at $(date)"