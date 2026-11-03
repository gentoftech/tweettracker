# Deploying TweetTracker on DigitalOcean

Self-contained runbook for deploying **TweetTracker** ([`gentoftech/tweettracker`](https://github.com/gentoftech/tweettracker)) to DigitalOcean for 24/7 operation. Written so any human operator or automated deployment agent can execute it end-to-end without guesswork.

---

## 1. Architecture & Resource Profile

TweetTracker runs **4 lightweight containers** orchestrated by `docker-compose.yml` behind **Caddy** (automatic Let's Encrypt HTTPS + unbuffered Server-Sent Events):

```text
Internet (HTTPS :443)
   │
   ▼
Caddy Reverse Proxy (Host :80 / :443)
   ├── /api/*  (flush_interval -1 for SSE) ──► 127.0.0.1:8000  [api: FastAPI + Uvicorn]
   └── /*                                  ──► 127.0.0.1:3000  [web: Next.js 15 Standalone]
                                                      │
                                                      ▼
                                               [db: Postgres 16] ◄── [worker: 24/7 Python Loop]
                                               (internal Docker network only; no host port)
```

| Service | Container Role | Steady RAM | Host Binding |
| :--- | :--- | :--- | :--- |
| **`db`** | `postgres:16-alpine` (`pgdata` volume) | `~180–250 MB` | Internal Docker network only (`db:5432`) |
| **`api`** | Python 3.12 FastAPI + SSE stream (`backend/Dockerfile`) | `~300–450 MB` | `127.0.0.1:8000` (loopback only) |
| **`worker`** | 24/7 poller: Radar (`10m`), Network (`1h`), Backtest (`6h`), Algo Sync (`24h`) | `~250–400 MB` | None (background daemon) |
| **`web`** | Node 22 Next.js 15 standalone UI (`web/Dockerfile`) | `~120–200 MB` | `127.0.0.1:3000` (loopback only) |

> **Why a DigitalOcean Droplet + Docker Compose is preferred over App Platform:**
> 1. **Flat predictable cost (`$24/mo`)** vs. paying separately for 2 web services + 1 worker + managed Postgres on App Platform (`~$39–$50+/mo`).
> 2. **Zero SSE proxy timeouts** on `/api/workspaces/{id}/radar/stream`.
> 3. **Exact parity** with the repo's `docker-compose.yml` and localhost-bound container ports (`127.0.0.1:8000` / `127.0.0.1:3000`, which prevents Docker `iptables` from exposing backend ports directly to the public internet).
> *(If you prefer DigitalOcean App Platform anyway, see [Section 9](#9-alternative-digitalocean-app-platform-notes) for the exact component mapping.)*

---

## 2. Recommended Droplet & DNS Setup

### Droplet Specification
- **Image:** `Ubuntu 24.04 (LTS) x64`
- **Plan:** Basic (Shared CPU) → Regular SSD or Premium Intel/AMD
- **Size:** **`s-2vcpu-4gb`** (`2 vCPU / 4 GB RAM / 80 GB SSD` — **`$24/mo`**)
  - *Minimum viable size:* `s-2vcpu-2gb` (`$18/mo`) **only if** the `2 GB` swapfile in Step 3 is enabled so `next build` inside Docker never runs out of memory. Do **not** use `512 MB` or `1 GB` Droplets.
- **Authentication:** SSH Key
- **Backups:** Optional DigitalOcean weekly Droplet backups (`+$4.80/mo`)

### Domain / HTTPS Endpoint (Choose One)
- **Option A — Custom Subdomain (Recommended):** Create an `A` record (e.g., `tracker.yourdomain.com` → `<DROPLET_IP>`, TTL `300`). If using Cloudflare DNS, set proxy status to **DNS Only (grey cloud)** initially or SSL mode to **Full (Strict)** so Caddy obtains Let's Encrypt certs cleanly.
- **Option B — Instant HTTPS Without a Domain:** Use `<DROPLET_IP_DASHED>.sslip.io` (for example, IP `143.198.10.20` becomes `143-198-10-20.sslip.io`). Caddy will automatically provision a trusted Let's Encrypt TLS certificate for it.

Set `DOMAIN` in the steps below to whichever hostname you chose (e.g., `tracker.yourdomain.com`).

---

## 3. Host Provisioning (Docker, Caddy, Firewall & Swap)

SSH into the Droplet as `root` (or a `sudo` user) and run:

```bash
set -euo pipefail

# 1. System updates & base utilities
apt-get update && apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg git ufw debian-keyring debian-archive-keyring apt-transport-https

# 2. Add 2 GB swapfile (prevents OOM spikes during Docker image builds)
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 3. Install official Docker Engine + Docker Compose v2 plugin
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# 4. Install official Caddy reverse proxy
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

# 5. Lock down host firewall (SSH + HTTP/HTTPS only)
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
```

---

## 4. Clone the GitHub Repository (`gentoftech/tweettracker`)

For the public repository, clone directly over HTTPS into `/opt/tweettracker`:

```bash
git clone https://github.com/gentoftech/tweettracker.git /opt/tweettracker
cd /opt/tweettracker
```

*(Optional — if deploying from a **private fork**, use a read-only GitHub Deploy Key scoped strictly to your fork rather than a full-account SSH key:)*

```bash
ssh-keygen -t ed25519 -C "tweettracker-do-prod" -f /root/.ssh/tweettracker_deploy -N ""
cat >> /root/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile /root/.ssh/tweettracker_deploy
  IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
cat /root/.ssh/tweettracker_deploy.pub
# Add as a Read-Only Deploy Key on your fork, then:
ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts
git clone git@github.com:<your-org>/tweettracker.git /opt/tweettracker
cd /opt/tweettracker
```

---

## 5. Configure Runtime Secrets Outside the Git Repo

**Security Rule:** Never create a `.env` file containing real secrets inside `/opt/tweettracker`. Store server environment variables in `/etc/tweettracker/runtime.env` (`chmod 600`) and wrap `docker compose` with `/usr/local/bin/tt-compose` so **both** `${VAR}` build args and pass-through runtime variables are always exported identically.

```bash
mkdir -p /etc/tweettracker
chmod 700 /etc/tweettracker

# Replace DOMAIN and TWEETTRACKER_ADMIN_PASSWORD below before running:
DOMAIN="tracker.yourdomain.com"
ADMIN_PASSWORD="REPLACE_WITH_STRONG_LOGIN_PASSWORD"
PG_PASSWORD="$(openssl rand -hex 24)"
VAULT_SECRET="$(openssl rand -hex 32)"

cat > /etc/tweettracker/runtime.env <<EOF
# Domain & CORS
NEXT_PUBLIC_API_URL=https://${DOMAIN}
CORS_ORIGINS=https://${DOMAIN}

# Clean production boot (0 demo workspaces; seeds baseline x-algorithm v1 @ 9106e9f)
SEED_FIXTURES=false

# Postgres credentials (internal Docker network)
POSTGRES_USER=tweettracker
POSTGRES_PASSWORD=${PG_PASSWORD}
POSTGRES_DB=tweettracker

# Full-screen UI password gate + Fernet master key for in-app encrypted API keys
TWEETTRACKER_ADMIN_PASSWORD=${ADMIN_PASSWORD}
TWEETTRACKER_SECRET_KEY=${VAULT_SECRET}

# Optional provider env fallbacks (leave blank to set/rotate keys inside /settings UI)
TWEETAPI_API_KEY=
GOOGLE_AI_API_KEY=
XAI_API_KEY=
GITHUB_TOKEN=
TWEETTRACKER_WEBHOOK_SECRET=
EOF

chmod 600 /etc/tweettracker/runtime.env
```

Create the `/usr/local/bin/tt-compose` helper so every Docker Compose invocation automatically loads `/etc/tweettracker/runtime.env` into the shell environment:

```bash
cat > /usr/local/bin/tt-compose <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
# shellcheck disable=SC1091
source /etc/tweettracker/runtime.env
set +a
exec docker compose -f /opt/tweettracker/docker-compose.yml "$@"
EOF

chmod +x /usr/local/bin/tt-compose
```

> **Why `TWEETTRACKER_SECRET_KEY` matters:** API keys entered in the `/settings` UI (`TWEETAPI_API_KEY`, `GOOGLE_AI_API_KEY`, etc.) are encrypted in Postgres (`app_secrets`) using `PBKDF2HMAC-SHA256(TWEETTRACKER_SECRET_KEY)`. Keep `/etc/tweettracker/runtime.env` backed up; as long as `TWEETTRACKER_SECRET_KEY` stays constant, your in-app keys survive container rebuilds and image upgrades automatically.

---

## 6. Configure Caddy (HTTPS + Unbuffered SSE) & Launch Stack

Write `/etc/caddy/Caddyfile` (replacing `tracker.yourdomain.com` with your `DOMAIN`):

```bash
DOMAIN="tracker.yourdomain.com"

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    encode zstd gzip

    # Route /api/* directly to FastAPI (preserving /api prefix and disabling SSE buffering)
    handle /api/* {
        reverse_proxy 127.0.0.1:8000 {
            flush_interval -1
        }
    }

    # Route all UI traffic to Next.js standalone server
    handle {
        reverse_proxy 127.0.0.1:3000
    }
}
EOF

caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

Now build and start all 4 containers:

```bash
tt-compose up -d --build
tt-compose ps
```

---

## 7. Post-Deploy Verification Checklist

Run these 4 checks from the Droplet (or any machine) to verify TLS, routing, auth lockdown, and clean boot:

```bash
set -a && source /etc/tweettracker/runtime.env && set +a

# 1. Public health check returns 200 OK and confirms auth_required=true
curl -fsS "${NEXT_PUBLIC_API_URL}/api/health"
echo ""

# 2. Unauthenticated workspace listing MUST be rejected with HTTP 401
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${NEXT_PUBLIC_API_URL}/api/workspaces")
echo "Unauthenticated /api/workspaces status: ${HTTP_CODE} (expected: 401)"

# 3. Authenticated workspace listing returns HTTP 200 and an empty array [] (no demo data)
curl -fsS -H "Authorization: Bearer ${TWEETTRACKER_ADMIN_PASSWORD}" \
  "${NEXT_PUBLIC_API_URL}/api/workspaces"
echo ""

# 4. Next.js UI serves HTTP 200 over HTTPS
curl -fsSI "${NEXT_PUBLIC_API_URL}/" | head -n 5
```

Expected results:
- `/api/health` → `{"status":"ok",...,"upstream_commit_sha":"9106e9f","auth_required":true,...}`
- Unauthenticated `/api/workspaces` → `401`
- Authenticated `/api/workspaces` → `[]`
- `tt-compose logs --tail=30 worker` → shows `Starting TweetTracker background worker` with zero errors.

---

## 8. First-Time Live Setup Inside the App

Once verified, open `https://<YOUR_DOMAIN>` in your browser:

1. **Unlock UI:** Enter `TWEETTRACKER_ADMIN_PASSWORD` on the full-screen lock screen.
2. **Save API Keys (`/settings`):**
   - Click **1. Set API Keys** (or the gear icon in the top-right).
   - Paste `TWEETAPI_API_KEY` (required for live X polling & graph crawling) and optionally `GOOGLE_AI_API_KEY` / `GITHUB_TOKEN` / `TWEETTRACKER_WEBHOOK_SECRET`.
   - Click **Save API Keys** — badges immediately turn green (`Saved in App (••••••••1234)`) and the background worker picks them up on its next tick without a restart.
3. **Create Your Workspace:**
   - Click **2. Create Workspace** (or the top-left workspace selector → **New Workspace**).
   - Set **Workspace Name** (e.g., `GentOfTech`), **Your X Handle** (`@gentoftech`), and **X Data Source** (`TweetAPI.com Live`).
4. **Seed Your Network (`/network`):**
   - Open **Network**, click **Add People** (or **Sync X List**), and paste your initial seed handles, CSV, or X List URL/ID.
   - TweetTracker immediately fetches live profiles, crawls following lists + recent replies/quotes, clusters and colors connected accounts automatically, flags `@gentoftech` `20x` mutuals, snowballs multi-seed-endorsed peers into your graph, and begins polling **Hot Posts** every `10` minutes.

---

## 9. Alternative: DigitalOcean App Platform Notes

If deploying via **DigitalOcean App Platform** instead of a Droplet, map the repo as 4 components in a single App Spec:

| Component | Type | Source Dir / Dockerfile | Run Command / Port | Route |
| :--- | :--- | :--- | :--- | :--- |
| **`db`** | Managed PostgreSQL 16 (or Dev DB) | — | Port `25060` (SSL) | Internal |
| **`api`** | Web Service (`basic-xs` `1 GB`+) | `/backend` · `backend/Dockerfile` | Default `CMD` · Port `8000` | `/api` (**Preserve Path Prefix = `true`**) |
| **`worker`** | Worker (`basic-xs` `1 GB`+) | `/backend` · `backend/Dockerfile` | `python -m tweettracker.worker.main` | None |
| **`web`** | Web Service (`basic-xxs` `512 MB`+) | `/web` · `web/Dockerfile` | Default `CMD` · Port `3000` | `/` |

**Critical App Platform specifics:**
1. **`DATABASE_URL` driver prefix:** DigitalOcean's injected `${db.DATABASE_URL}` uses `postgresql://...?sslmode=require`. Override `DATABASE_URL` on `api` and `worker` using SQLAlchemy's asyncpg scheme:
   `postgresql+asyncpg://${db.USERNAME}:${db.PASSWORD}@${db.HOSTNAME}:${db.PORT}/${db.DATABASE}?ssl=require`
2. **Preserve `/api` route prefix:** When attaching route `/api` to the `api` component, ensure `preserve_path_prefix: true` is set in the App Spec; otherwise App Platform strips `/api` before forwarding to FastAPI.
3. **Build-time `NEXT_PUBLIC_API_URL`:** Set `NEXT_PUBLIC_API_URL=${APP_URL}` with scope `RUN_AND_BUILD_TIME` on `web`, and `CORS_ORIGINS=${APP_URL}`, `SEED_FIXTURES=false`, `TWEETTRACKER_ADMIN_PASSWORD` (`SECRET`), and `TWEETTRACKER_SECRET_KEY` (`SECRET`) on `api` and `worker`.

---

## 10. Day-2 Operations (Updates, Logs & Backups)

### Pulling Updates from `origin/main`
```bash
cd /opt/tweettracker
git pull --ff-only origin main
tt-compose up -d --build
```
*(Note: If you ever change `NEXT_PUBLIC_API_URL` in `/etc/tweettracker/runtime.env`, run `tt-compose build --no-cache web && tt-compose up -d` so Next.js bakes the new URL into client bundles.)*

### Viewing Live Logs
```bash
tt-compose logs -f api worker
```

### Automated Nightly Postgres Backup (Optional Cron)
```bash
mkdir -p /var/backups/tweettracker
chmod 700 /var/backups/tweettracker

cat > /etc/cron.daily/tweettracker-backup <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a && source /etc/tweettracker/runtime.env && set +a
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f /opt/tweettracker/docker-compose.yml exec -T db \
  pg_dump -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip > "/var/backups/tweettracker/db-${STAMP}.sql.gz"
find /var/backups/tweettracker -name "db-*.sql.gz" -mtime +14 -delete
EOF

chmod +x /etc/cron.daily/tweettracker-backup
```
