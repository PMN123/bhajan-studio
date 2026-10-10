#!/bin/bash
# Double-click this file in Finder to start Bhajan Studio.
# It opens a Terminal window; close that window (or press Ctrl+C in it) to stop the app.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start.sh "$@"
