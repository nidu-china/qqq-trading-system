#!/bin/bash
set -e
cd /data/code/qqq-trading-system
PYTHONUNBUFFERED=1 /usr/bin/python3 -u live_trader.py >> /tmp/qqq_live_output.log 2>&1
