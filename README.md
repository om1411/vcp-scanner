# VCP Live Scanner — Deployment Guide

## Step 1 — GitHub (free, 2 min)
1. Go to https://github.com and create a free account
2. Click **New Repository** → name it `vcp-scanner` → Public → Create
3. Upload these 4 files: `app.py`, `requirements.txt`, `Procfile`, `render.yaml`

## Step 2 — Render (free hosting, 3 min)
1. Go to https://render.com → Sign up free (use GitHub login)
2. Click **New → Web Service**
3. Connect your GitHub → select `vcp-scanner` repo
4. Settings:
   - Name: `vcp-scanner`
   - Region: Singapore (closest to India)
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --timeout 120 --workers 1`
5. Scroll down → **Environment Variables** → Add:
   - `KITE_API_KEY` = your api key
   - `KITE_API_SECRET` = your api secret
6. Click **Create Web Service**
7. Wait ~3 min → you get a URL like: `https://vcp-scanner.onrender.com`

## Step 3 — Update Kite Redirect URL (IMPORTANT)
1. Go to https://kite.trade → My Apps → Your App → Edit
2. Change Redirect URL to:
   `https://vcp-scanner.onrender.com/callback`
3. Save

## Step 4 — Daily Login (takes 10 seconds)
- Open your app URL every morning before market opens
- Click "Login with Zerodha"
- Enter your Zerodha credentials
- Scanner starts automatically ✅

## How it works
- Scans Nifty 500 every 10 minutes during market hours (9:15–15:30)
- Filters stocks with 3M return > 30% AND avg volume > 2L
- Detects C1→C2 inside bar formation
- Fires 🟢 BUY signal on C3 breakout with R_Vol ≥ 110%
- Shows Stop Loss and 1:5 / 1:10 targets automatically
