# Deployment System — HF Space Sync (v4.8)

## Current setup

- **Primary bot host**: `test02-HF05/QuizBot` (Hugging Face Space, Docker SDK)
- **Fallback bot host**: `hamza-02/QuizBot` (old HF Space — kept running as standby,
  code still syncs here too, but its webhook is NOT active — only one host should
  serve live Telegram traffic at a time, per ToS: dual-webhook/dual-instance caused
  a past account ban)
- **CF Worker** (`atlasquizbotpro`) forwards Telegram webhook traffic to whichever
  host's URL is set in the Worker's `RENDER_URL` env var (Cloudflare dashboard →
  Worker → Settings → Variables). Currently pointed at:
  `https://test02-hf05-quizbot.hf.space`

## GitHub Actions auto-sync (`.github/workflows/sync-to-hf.yml`)

On every push to `main`, this workflow automatically:
1. Pushes the full repo to the primary HF Space (`test02-HF05/QuizBot`)
2. Syncs all `.env`-equivalent secrets to that Space via the HF API
   (`huggingface_hub`'s `add_space_secret`)

**No manual secret re-entry needed on future code pushes** — step 2 re-applies
the same secrets every run, so if the HF Space is ever recreated (new name/account),
only two things need to change:

- Update the space name in the `git remote add hf-primary` line
- Update the space name in the `Sync Secrets to Primary HF Space` step
  (`space_id = "test02-HF05/QuizBot"`)

## Required GitHub repo secrets (Settings → Secrets and variables → Actions)

| Secret name | Purpose |
|---|---|
| `HF_TOKEN` | Old token, pushes to fallback `hamza-02/QuizBot` (unchanged, do not touch) |
| `HF_TOKEN_PRIMARY` | Write-access token for `test02-HF05` account, pushes to primary space |
| `SPACE_SECRETS_JSON` | Single JSON blob containing all bot env vars (BOT_TOKEN, SUPABASE_KEY, GEMINI_KEYS, etc.) — pushed to the primary HF Space's own Secrets on every deploy |

## If moving to a brand-new HF Space in the future

1. Create the new Space (Docker SDK) on huggingface.co
2. Generate a write-access token for that HF account
3. Update `HF_TOKEN_PRIMARY` GitHub secret with the new token
4. Update `SPACE_SECRETS_JSON` GitHub secret ONLY if actual bot credentials
   changed (e.g. new BOT_TOKEN) — otherwise reuse as-is, it's account-independent
5. Edit `.github/workflows/sync-to-hf.yml`:
   - `git remote add hf-primary https://<HF_USERNAME>:${HF_TOKEN_PRIMARY}@huggingface.co/spaces/<HF_USERNAME>/<SPACE_NAME>`
   - `space_id = "<HF_USERNAME>/<SPACE_NAME>"` (in the secrets-sync Python step)
6. Push to `main` — GitHub Actions handles the rest automatically (code + secrets,
   zero manual re-entry)
7. Update CF Worker's `RENDER_URL` variable to the new Space's `.hf.space` URL to
   make it the one actually receiving live webhook traffic

## Why this exists

HF Spaces occasionally get stuck in a "Build Queued" state that never progresses,
even after Factory Rebuild — a known HF platform-side bug (not fixable from our
side). When that happens, the fix is: stand up a fresh Space, let this automated
pipeline redeploy code + secrets to it with zero manual secret re-typing, then
repoint the CF Worker.
