#!/bin/bash
# Unraid User Scripts plugin: schedule hourly. Pulls the relay repo so the analyzers container sees new runs.
cd /mnt/user/appdata/claude-usage-tracker/relay && git pull -q --ff-only
