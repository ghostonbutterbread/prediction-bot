#!/bin/bash
# Worker instance 1
cd ~/projects/prediction-bot
export PAPER_MODE=true
export DATA_DIR=data/instances/worker3/data
export LOG_DIR=data/instances/worker3/data
export PAPER_LOG_FILE=data/instances/worker3/data/paper_loop.log
mkdir -p "$DATA_DIR"
exec .venv/bin/python paper_loop.py
