#!/bin/sh
# Unraid: copy the Claude Usage datasource + dashboard into Grafana's provisioning folder.
# Re-run after a dashboard change on main. Usage:
#   sh install.sh [/mnt/user/appdata/grafana/provisioning]
set -e
DEST="${1:-/mnt/user/appdata/grafana/provisioning}"
RAW=https://raw.githubusercontent.com/rtippenhauer/Claude-Usage-Tracker/main/deploy/grafana
mkdir -p "$DEST/datasources" "$DEST/dashboards/claude-usage"
curl -fsSL "$RAW/provisioning/datasources/claude-usage.yml" -o "$DEST/datasources/claude-usage.yml"
curl -fsSL "$RAW/provisioning/dashboards/claude-usage.yml"  -o "$DEST/dashboards/claude-usage.yml"
curl -fsSL "$RAW/dashboards/claude-usage.json"              -o "$DEST/dashboards/claude-usage/claude-usage.json"
echo "installed into $DEST; restart Grafana to load the datasource"
