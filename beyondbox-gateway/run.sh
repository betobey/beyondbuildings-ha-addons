#!/bin/bash
# run.sh – BeyondBox Gateway Add-on entry point
# client.py handles everything: config rendering, telegraf lifecycle, heartbeat loop
set -e
exec python3 /app/client.py
