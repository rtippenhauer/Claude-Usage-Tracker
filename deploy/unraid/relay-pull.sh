#!/bin/bash
# Unraid User Scripts plugin: schedule hourly.
R=/mnt/user/appdata/claude-usage-tracker/relay
cd $R && git pull -q --ff-only
docker exec claude-usage-analyzers python -m server.relay_pull --relay /relay --db /data/tracker.db
if [ -n "$(git status --porcelain spool)" ]; then git add -A spool && git commit -qm "ingested spool" && git push -q; fi
docker exec claude-usage-analyzers python -m analyzers.run --db /data/tracker.db --rules >/dev/null
