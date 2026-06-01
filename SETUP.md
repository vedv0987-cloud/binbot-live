# BinBot — fresh VM setup & recovery

How to get BinBot live again on a **brand-new VM** (e.g. Oracle free tier) reusing
your existing keys and trading state, after an old VM breaks.

The flow has three scripts:

| script         | when                 | what it does                                            |
|----------------|----------------------|---------------------------------------------------------|
| `backup.sh`    | on the **live** VM   | snapshots `.env` + state (the gitignored, un-cloneable bits) |
| `bootstrap.sh` | on the **new** VM    | clone + deps + systemd + gate + start (idempotent)      |
| `deploy.sh`    | ongoing              | pull latest code, gate, restart (steady-state updates)  |

## 1. Back up the old VM (do this regularly, keep it OFF the VM)

```bash
bash backup.sh                 # → ./binbot_backups/binbot_backup_<ts>.tgz
# copy it somewhere safe:
scp binbot_backups/binbot_backup_*.tgz you@laptop:~/binbot_backups/
```

A `git clone` does **not** restore `.env`, `bot_state.json`, journals, or ML
models — those are gitignored on purpose (secrets + per-VM state). This tarball
is the only thing that carries them across VMs.

## 2. Bring up a fresh VM

```bash
# unpack your backup somewhere on the new VM first (if restoring):
mkdir -p /tmp/restore && tar xzf binbot_backup_<ts>.tgz -C /tmp/restore

# then one command sets everything up:
sudo bash bootstrap.sh --restore /tmp/restore
```

`bootstrap.sh` will: install OS + Python deps, clone the repo to
`/home/ubuntu/binbot_live`, restore your data, install the systemd units, run
the compile + unit-test gate, **check the Binance API works from this VM's IP**,
and only then enable + start the bot and watchdog.

No backup to restore? Run `sudo bash bootstrap.sh`, then edit
`/home/ubuntu/binbot_live/.env` with your keys and re-run.

Non-default user/paths (e.g. Oracle Linux `opc`):
```bash
RUN_USER=opc sudo -E bash bootstrap.sh
```

## 3. The new-IP gotcha (most common failure)

A new VM has a **new public IP**, and Binance rejects API keys from
un-whitelisted IPs. `bootstrap.sh` prints this VM's public IP and tells you if
the API call was rejected. To fix:

> Binance → **API Management** → edit your key → **add the printed IP** to the
> trusted-IP list → save. Then re-run `sudo bash bootstrap.sh`.

The bot will **not** start until the API check passes, so a forgotten whitelist
can't leave it half-running.

## 4. Day-to-day code updates

Once a VM is up, ship code changes with the existing gate:

```bash
bash deploy.sh   # fetch origin/main, compile+test gate, rollback on failure, restart
```

## Useful checks

```bash
systemctl status binance-bot-v11           # bot service
journalctl -u binance-bot-v11 -f           # live logs
systemctl list-timers binbot-watchdog.timer
```
