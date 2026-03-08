#!/bin/bash
set -e

INTERVAL=${SCAN_INTERVAL:-14400}  # default 4 hours (14400 seconds)

echo "MTS v2 starting — scan interval: ${INTERVAL}s"

while true; do
    python mts.py
    echo "Sleeping ${INTERVAL}s until next scan..."
    sleep "$INTERVAL"
done
