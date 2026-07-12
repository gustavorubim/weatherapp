# Unattended RadarVault collection

The templates in this directory are intentionally explicit. Replace every
`/ABSOLUTE/PATH/RadarVault` placeholder with the checkout path and choose the
radar IDs in `ProgramArguments`/`ExecStart`. The collector wrapper owns
`cache/.radarvault.lock`, forwards `TERM`/`INT` to `app.cache_cli`, and releases
the lock on normal or interrupted exit.

## macOS launchd

```bash
cp ops/radarvault.launchd.plist ~/Library/LaunchAgents/local.radarvault.collector.plist
# Edit the copied plist's absolute paths and radar IDs.
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/local.radarvault.collector.plist
launchctl kickstart -k "gui/$(id -u)/local.radarvault.collector"
launchctl print "gui/$(id -u)/local.radarvault.collector"
tail -f data/collector.log data/collector.error.log
```

Stop/restart and uninstall:

```bash
launchctl kill SIGTERM "gui/$(id -u)/local.radarvault.collector"
launchctl kickstart -k "gui/$(id -u)/local.radarvault.collector"
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/local.radarvault.collector.plist
rm ~/Library/LaunchAgents/local.radarvault.collector.plist
```

## Linux systemd (user service)

```bash
mkdir -p ~/.config/systemd/user
cp ops/radarvault.service ~/.config/systemd/user/radarvault.service
# Edit absolute paths and radar IDs in the copied unit.
systemctl --user daemon-reload
systemctl --user enable --now radarvault.service
systemctl --user status radarvault.service
journalctl --user -u radarvault.service -f
```

Stop/restart and uninstall:

```bash
systemctl --user stop radarvault.service
systemctl --user restart radarvault.service
systemctl --user disable radarvault.service
rm ~/.config/systemd/user/radarvault.service
systemctl --user daemon-reload
```

## Safe upgrades and recovery

Stop the service before upgrading source or dependencies. Do not delete
`cache/`, `videos/`, or `data/catalog.sqlite3`; the service lock and catalog
are migration-safe. Start the service after the upgrade and inspect logs. A
dead process leaves owner metadata in `.radarvault.lock`; a subsequent
wrapper invocation can recover a stale lock after checking the recorded PID.
Never manually remove a lock owned by a live collector.

Retention is dry-run first:

```bash
python -m app.catalog_cli retention --database cache/catalog.sqlite3 --max-age-days 30 --dry-run
python -m app.catalog_cli retention --database cache/catalog.sqlite3 --max-age-days 30 --apply
```

The disk guard should be checked before enabling long-running collection. A
critical state means the collector must stop writing until retention or manual
cleanup restores free space.
