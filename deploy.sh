#!/bin/bash
cd "$(dirname "$0")"

# Install/update dependencies
pip install -r requirements.txt --quiet

# Create logs dir if missing
mkdir -p logs

# Restart the Flask app
pkill -f "python run_web.py" || true
sleep 1
nohup python run_web.py --host 0.0.0.0 --port 5001 > logs/web.log 2>&1 &

echo "Deploy complete — app restarted on port 5001"
