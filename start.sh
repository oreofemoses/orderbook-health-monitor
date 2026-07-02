#!/bin/sh
# Start the background monitor, then the web server
python debug.py &
exec uvicorn api:app --host 0.0.0.0 --port 8080