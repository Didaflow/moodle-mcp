# Qdrant deploy on df VPS alpha

Reference setup for the vector store consumed by `moodle-ingest` (issue #5) and
later by downstream consumers (didaflow-agent, didaflow-labs).

## Design choices

- **Native binary, not Docker.** The host (`ubuntu-4gb-hel1-1`, Hetzner) already
  hosts `didaflow-agent` and `didaflow-labs` as native systemd services + Python
  venvs. Introducing Docker just for Qdrant would add ~200MB of container engine
  on a small 4GB VPS for no isolation benefit. The Qdrant binary is a single
  ~75MB static executable; systemd's sandbox directives (`ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, dedicated `qdrant` user) provide adequate
  isolation.
- **Bind to `127.0.0.1` only.** Never `0.0.0.0`. Consumers running on the same
  VPS reach Qdrant as a localhost peer. The laptop reaches it via SSH tunnel
  during ingestion. Public exposure (Caddy / nginx + TLS + bearer at the proxy)
  is intentionally out of scope — adding it is a separate decision that only
  matters once browser clients need direct access.
- **API key via env file, not config.yaml.** `/etc/qdrant/qdrant.env` is
  mode 600 root:root and read by systemd at unit start. The key never sits in a
  group-readable file. Rotation = edit the env file, `systemctl restart qdrant`.
- **No backups in this deploy.** Qdrant has a snapshot API; once real data
  matters, schedule snapshots via cron. Not included here.

## One-time install

Run as `root` on the VPS:

```bash
# 1. Pre-create data + snapshots dirs (wipe any prior state with explicit consent first)
rm -rf /var/lib/qdrant/.qdrant-initialized /var/lib/qdrant/storage /var/lib/qdrant/snapshots
mkdir -p /var/lib/qdrant/storage /var/lib/qdrant/snapshots

# 2. Create the qdrant system user
id qdrant >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin qdrant
chown -R qdrant:qdrant /var/lib/qdrant
chown root:qdrant /etc/qdrant && chmod 750 /etc/qdrant

# 3. Drop in the config (this file's sibling: config.yaml.example)
install -m 640 -o root -g qdrant deploy/qdrant/config.yaml.example /etc/qdrant/config.yaml

# 4. Generate the API key and write the env file
APIKEY=$(openssl rand -hex 32)
umask 077
printf 'QDRANT__SERVICE__API_KEY=%s\n' "${APIKEY}" > /etc/qdrant/qdrant.env
chmod 600 /etc/qdrant/qdrant.env
chown root:root /etc/qdrant/qdrant.env
# Note the key — you'll need it on your laptop. Or read it back later:
#   cat /etc/qdrant/qdrant.env

# 5. Install the systemd unit
install -m 644 deploy/qdrant/qdrant.service /etc/systemd/system/qdrant.service
systemctl daemon-reload
systemctl enable --now qdrant

# 6. Verify
systemctl is-active qdrant         # → active
ss -tlnp | grep ':633'             # → 127.0.0.1:6333 + 127.0.0.1:6334
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:6333/collections      # → 401
curl -sS -H "api-key: ${APIKEY}" http://127.0.0.1:6333/collections                # → {"result":{"collections":[]},...}
```

## Laptop access

For `moodle-ingest` running from your laptop on the BBS / UniBo tenant VPN:

```bash
# Open an SSH tunnel in the background — local :6333 forwards to VPS Qdrant
ssh -L 6333:127.0.0.1:6333 root@<vps-host> -N &

# Export creds (read API key from your password manager or via SSH)
export QDRANT_URL=http://127.0.0.1:6333
export QDRANT_API_KEY="$(ssh root@<vps-host> 'cat /etc/qdrant/qdrant.env' | cut -d= -f2)"

# Smoke test
curl -sS -H "api-key: ${QDRANT_API_KEY}" http://127.0.0.1:6333/collections
```

## Operations

### Rotate the API key

```bash
NEW=$(openssl rand -hex 32)
umask 077
printf 'QDRANT__SERVICE__API_KEY=%s\n' "${NEW}" > /etc/qdrant/qdrant.env
chmod 600 /etc/qdrant/qdrant.env
systemctl restart qdrant
# Distribute the new key to every consumer (laptop env, didaflow-agent .env, etc.)
```

### Snapshot a collection (manual backup)

```bash
APIKEY=$(grep -oP '(?<=QDRANT__SERVICE__API_KEY=).*' /etc/qdrant/qdrant.env)
curl -sS -X POST -H "api-key: ${APIKEY}" http://127.0.0.1:6333/collections/<name>/snapshots
# Snapshot file appears in /var/lib/qdrant/snapshots/<name>/
```

### Tail logs

```bash
journalctl -u qdrant -f --since '5 min ago'
```

### Stop / start

```bash
systemctl stop qdrant
systemctl start qdrant
systemctl restart qdrant
```

## Upgrade path

When a new Qdrant minor release ships:

1. Stop the service: `systemctl stop qdrant`
2. Snapshot every collection first (see above).
3. Replace the binary: `curl -L -o /usr/local/bin/qdrant.new https://github.com/qdrant/qdrant/releases/download/vX.Y.Z/qdrant-x86_64-unknown-linux-gnu`
   then `chmod +x /usr/local/bin/qdrant.new && mv /usr/local/bin/qdrant.new /usr/local/bin/qdrant`
4. Start: `systemctl start qdrant`
5. Verify: `systemctl is-active qdrant` + `curl ${URL}/collections`
6. If anything's wrong, restore from snapshot.

## Decommission

```bash
systemctl disable --now qdrant
rm /etc/systemd/system/qdrant.service
systemctl daemon-reload
rm -rf /etc/qdrant /var/lib/qdrant
userdel qdrant
# Binary at /usr/local/bin/qdrant can stay if you might redeploy.
```
