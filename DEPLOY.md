# Deployment Checklist

## 1. First-time GitHub setup

1. **Push the repo to GitHub**

   ```bash
   cd sap-gig-finder
   git init
   git add .
   git commit -m "initial commit"
   gh repo create sap-gig-finder --public --source=. --push
   ```

2. **Add repository secrets**

   Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret name        | Value                          |
   |--------------------|--------------------------------|
   | `ANTHROPIC_API_KEY` | Your Anthropic API key         |
   | `RAPIDAPI_KEY`      | Your RapidAPI key              |

3. **Enable GitHub Pages**

   Go to **Settings → Pages → Build and deployment → Source** and select **GitHub Actions**.

4. **Publish the initial dashboard**

   Go to **Actions → Deploy Pages → Run workflow** and trigger it manually. Wait for the run to complete, then confirm the site is live at `https://<your-username>.github.io/sap-gig-finder/`.

5. **Dry-run the pipeline**

   Go to **Actions → Weekly SAP Gig Scan → Run workflow**. Check the **Dry run** checkbox and click **Run workflow**. Verify the job completes without errors in the logs.

6. **First real pipeline run**

   Trigger **Weekly SAP Gig Scan** again, this time with **Dry run unchecked**. After completion, confirm that `docs/gigs_latest.json` was committed and the dashboard shows gig cards.

---

## 2. Chrome PWA install

1. Open Chrome and navigate to:

   ```
   https://<your-username>.github.io/sap-gig-finder/
   ```

2. Look for the **install icon** in the address bar (a monitor with a down-arrow). Alternatively, open the Chrome menu (three dots) and click **Install SAP Gig Radar**.

3. Click **Install**. The app opens in its own standalone window without browser chrome.

4. To pin to your taskbar: right-click the app icon in the Windows taskbar (or Dock on macOS) and choose **Pin to taskbar** (or **Options → Pin to Dock**).

---

## 3. Verifying everything works

- [ ] Dashboard loads at the Pages URL and shows gig cards
- [ ] All four filter buttons (All / Score 8+ / Remote 100% / Part-time) toggle correctly
- [ ] The **Refresh** button fetches the latest JSON and updates the card grid
- [ ] Turn off WiFi — the app still loads from the service worker cache and shows the offline banner
- [ ] After a scheduled pipeline run (Thursday or Friday at 06:00 UTC), a fresh `gigs_latest.json` is committed and the dashboard updates on next page load or within the hourly auto-refresh

---

## 4. Updating the app

After any code change to files in `docs/`:

1. **Bump the cache version** in `docs/sw.js`:

   ```js
   // Before
   const CACHE_NAME = 'gig-radar-v1';

   // After
   const CACHE_NAME = 'gig-radar-v2';
   ```

   This forces every user's browser to discard the old app shell and download the new one.

2. **Push to main** — the Deploy Pages workflow triggers automatically and redeploys the site.

---

## 5. Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| Install icon does not appear in Chrome | The site is not served over HTTPS, or `manifest.json` / `sw.js` failed to load. | Confirm the Pages URL uses `https://`. Open DevTools → Application → Manifest and check for errors. Ensure `manifest.json` and `sw.js` are in the `docs/` directory and accessible. |
| Dashboard shows stale data after a pipeline run | The service worker is serving a cached `gigs_latest.json`. | Click the **Refresh** button in the dashboard. If that doesn't work, open DevTools → Application → Cache Storage, delete the `gig-radar-v1` cache, and reload. |
| Pipeline times out (>10 minutes) | A Playwright scrape is hanging on a page that changed its layout, or the Anthropic API is slow. | Check the workflow logs to identify which stage stalled. Increase `PIPELINE_TIMEOUT_SECONDS` in `config/settings.py` if the API is slow. If a scraper is hanging, inspect the target site for layout changes and update the selectors in `agents/scout.py`. |
| Playwright fails in CI with browser launch error | Chromium dependencies are missing on the runner. | Confirm the workflow runs `playwright install chromium --with-deps`. If the error persists, try pinning a specific Playwright version in `requirements.txt` and re-running. |
