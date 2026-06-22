#!/bin/bash

# Fix for "Too many open files" on macOS
ulimit -n 4096

# --- CONFIGURATION: replace with your real paths ---
export BASE_OUTPUT_PATH="$HOME/apps/ComfyUI/output"
export BASE_INPUT_PATH="$HOME/apps/ComfyUI/input"
export BASE_SMARTGALLERY_PATH="$HOME/ComfyUI/output"
export FFPROBE_MANUAL_PATH="/opt/homebrew/bin/ffprobe"
export SERVER_PORT=8189

# --- OPTIONAL LAUNCH PARAMETERS ---
# Add any of the following to the python command below depending on your scenario:
#
#   --admin-pass yourpassword   Set the admin password (log in as: admin / yourpassword)
#   --force-login               Require login on the Main Interface (use with --admin-pass)
#   --exhibition                Start in Exhibition Mode instead of the Main Interface
#   --port 8190                 Use a different port (default: 8189)
#   --enable-guest-login        Allow anonymous guest access in Exhibition
#   --blind-rating              Hide global averages to prevent user bias
#
# Example – Main Interface with login enforced:
#   python smartgallery.py --port 8189 --admin-pass yourpassword --force-login
#
# Example – Exhibition on port 8190 with Blind Rating:
#   python smartgallery.py --exhibition --port 8190 --admin-pass yourpassword --blind-rating

# --- START ---
uv run python smartgallery.py
