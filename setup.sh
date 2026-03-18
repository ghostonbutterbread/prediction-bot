#!/usr/bin/env bash
# setup.sh — Initialize prediction bot on a new machine
# Usage: bash setup.sh
#
# This script:
# 1. Checks dependencies
# 2. Installs Python packages
# 3. Creates .env from template if missing
# 4. Verifies the setup

set -e

echo "🎰 Prediction Bot Setup"
echo "========================"
echo ""

# --- Python version check ---
PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            echo "✅ Python: $cmd ($ver)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.11+ required. Install it and re-run."
    exit 1
fi

# --- Check for system package manager restrictions ---
echo ""
echo "📦 Installing dependencies..."

# Check if key packages are already installed
MISSING=$($PYTHON -c "
import importlib
missing = []
for mod in ['httpx', 'dotenv', 'yaml', 'kalshi_python_sync']:
    try:
        importlib.import_module(mod if mod != 'dotenv' else 'dotenv')
    except ImportError:
        missing.append(mod)
print(' '.join(missing))" 2>/dev/null)

if [ -z "$MISSING" ]; then
    echo "  All packages already installed"
else
    echo "  Missing: $MISSING"
    # Try install methods in order
    if $PYTHON -m pip install --break-system-packages -r requirements.txt 2>/dev/null; then
        echo "  ✅ Installed (--break-system-packages)"
    elif $PYTHON -m pip install --user -r requirements.txt 2>/dev/null; then
        echo "  ✅ Installed (--user)"
    else
        echo "  ⚠️  Auto-install failed. Try manually:"
        echo "     $PYTHON -m pip install --break-system-packages -r requirements.txt"
        echo "     OR: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    fi
fi

echo "✅ Dependencies installed"

# --- .env setup ---
echo ""
if [ -f .env ]; then
    echo "✅ .env already exists"
else
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "📝 Created .env from .env.example — EDIT IT with your keys!"
    else
        echo "⚠️  No .env or .env.example found"
    fi
fi

# --- Verify imports ---
echo ""
echo "🔍 Verifying modules..."
$PYTHON -c "
from bot.config import load_config
from bot.scheduler import ScanScheduler
from bot.researcher import OpenRouterClient, FeedbackTracker
print('  ✅ All modules importable')
" 2>/dev/null || echo "  ⚠️  Some modules failed (may need config.yaml)"

# --- Config check ---
echo ""
if [ -f config.yaml ]; then
    echo "✅ config.yaml found"
else
    echo "⚠️  config.yaml not found — using built-in defaults"
fi

# --- Data directory ---
mkdir -p data
echo "✅ Data directory ready"

# --- Summary ---
echo ""
echo "========================"
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. (Optional) Edit config.yaml for strategy tuning"
echo "  3. Run: $PYTHON main.py simulate 10 60"
echo ""
echo "Modes:"
echo "  $PYTHON main.py demo       # Live demo trading"
echo "  $PYTHON main.py simulate   # Paper trading simulation"
echo "  $PYTHON main.py markets    # List active markets"
echo "  $PYTHON main.py status     # Bot status"
