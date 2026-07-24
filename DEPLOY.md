# Deploying CloudOps-AI (free) — Streamlit Community Cloud

Note (Jul 2026): HuggingFace removed the free path for new Streamlit Spaces
(Streamlit SDK deprecated; Docker SDK is now paid on free accounts). Existing
HF Streamlit Spaces keep running, but for NEW deployments the free route is
Streamlit Community Cloud, which runs this repo unchanged.

## Step 1 — Push to GitHub
```powershell
git init
git add .
git commit -m "CloudOps-AI v1: phases 1-7"
git branch -M main
git remote add origin https://github.com/bvnchari1/cloudops-ai.git
git push -u origin main
```
(Create the empty repo at github.com/new first — name: cloudops-ai, public.)

## Step 2 — Deploy
1. https://share.streamlit.io -> Sign in with GitHub -> New app
2. Repository: bvnchari1/cloudops-ai · Branch: main · Main file: app.py
3. Deploy — build takes ~2 min, you get a public *.streamlit.app URL.

## Updates
```powershell
git add . ; git commit -m "update" ; git push
```
Streamlit Cloud auto-redeploys on push.

## Secrets (optional — live ServiceNow from the public app)
App -> Settings -> Secrets (TOML format):
```toml
SN_INSTANCE = "devXXXXXX"
SN_USER = "admin"
SN_PASSWORD = "..."
```
Caution: with secrets set, public visitors trigger real tickets in your PDI.
Recommended: keep the public app on MockITSM; demo live ServiceNow locally.

## Bonus
The public GitHub repo is itself a portfolio asset — link both the repo and
the live app URL on your resume alongside FinnieAI / CloudBridge.
