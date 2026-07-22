# Deploying the Supermarket Ops Agent

Target: a single always-on Linux box (AWS Lightsail 2 GB, Ubuntu 24.04). App and
Postgres both run there under `docker compose`.

**Why a plain VM and not a PaaS.** The bot long-polls Telegram, which is
*outbound* traffic. Platforms that scale to zero on idle see no inbound requests,
sleep the container, and the bot then never wakes itself — it goes silent
permanently. An always-on VM removes that failure mode, and it runs the exact
two-container topology the project has been developed against.

**Consequence worth knowing:** because polling is outbound, **no inbound port
needs to be open except SSH**. Not 8000, not 5432.

---

## 1. Instance

Lightsail → Create instance → **Ubuntu 24.04 LTS**, **2 GB RAM / 2 vCPU** plan.

> Take 2 GB, not 512 MB. Rendering three matplotlib charts and embedding a font
> with ReportLab happens in memory; 512 MB gets OOM-killed building the deck, and
> it will happen during a demo rather than during a test.

Then:

- **Networking → Static IP** → attach one. (Not strictly needed while polling,
  but it keeps the webhook fallback available and survives a stop/start.)
- **Networking → IPv4 Firewall** → **delete the default HTTP (80) rule**. Leave
  SSH (22) only.

## 2. Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
exit            # log back in so the group membership applies
```

Add swap — insurance for the image build, which is the peak-memory moment:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Code

The repo is private, so give the box a **read-only deploy key** rather than
putting a personal token on a server:

```bash
ssh-keygen -t ed25519 -C "lightsail-deploy" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Paste that into GitHub → repo → **Settings → Deploy keys → Add** (leave *Allow
write access* unchecked). Then:

```bash
git clone git@github.com:mehulp1612/processity_assigment.git
cd processity_assigment
```

## 4. Secrets

`.env` is gitignored and `.dockerignore`d, so it never reaches the repo or the
image. Create it on the box:

```bash
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=<token from @BotFather>

MODEL_API_KEY=<poolside key>
MODEL_BASE_URL=https://inference.poolside.ai/v1
AGENT_MODEL=poolside/laguna-s-2.1

POSTGRES_PASSWORD=$(openssl rand -hex 24)
EOF
chmod 600 .env
```

Leave `TELEGRAM_WEBHOOK_URL` unset — that is what selects long-polling.

Host timezone doesn't matter: `services/analytics.py` pins `Asia/Kolkata`
explicitly, so a UTC box still closes the day at the right moment.

## 5. Launch

```bash
mkdir -p out
docker compose -f docker-compose.prod.yml up -d --build
```

First boot waits for Postgres, applies the schema, and seeds the catalogue only
if it is empty — so this same command is also the redeploy command, and it will
not clobber real data.

## 6. Verify

```bash
docker compose -f docker-compose.prod.yml ps          # both services healthy
docker compose -f docker-compose.prod.yml logs -f app
```

Look for these three lines:

```
[entrypoint] schema applied
telegram polling started
telegram bot live as @processity_kirana_bot
```

Then, from the box:

```bash
curl -s localhost:8000/healthz      # {"ok":true,"db":"up"}
```

And finally the only test that counts: message the bot on Telegram and cut a
bill.

---

## Operating it

```bash
# Redeploy after a push
git pull && docker compose -f docker-compose.prod.yml up -d --build

# Logs
docker compose -f docker-compose.prod.yml logs -f app

# Back up the books  (do this before any risky change)
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U postgres store > ~/store-$(date +%F).sql

# Restore
cat ~/store-2026-07-22.sql | docker compose -f docker-compose.prod.yml exec -T db \
  psql -U postgres store

# Poke the database
docker compose -f docker-compose.prod.yml exec db psql -U postgres store
```

`restart: unless-stopped` on both services means the stack comes back by itself
after a crash or an instance reboot. Nothing else needs to be enabled.

## If the bot goes quiet

1. `docker compose -f docker-compose.prod.yml ps` — is `app` up and healthy?
2. `logs app` — a model-provider error surfaces as a failed agent turn with the
   provider's message. The free poolside tier is the single point of failure
   here; if it rate-limits, swap provider by editing three lines in `.env`
   (`MODEL_API_KEY`, `MODEL_BASE_URL`, `AGENT_MODEL`) and restarting.
3. Telegram `409 Conflict` in the logs means **two pollers are running** — a
   second copy of the stack, or a local dev instance still up. Only one process
   may poll a token at a time. Stop the other one.
