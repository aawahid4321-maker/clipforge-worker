# ClipForge Worker — free server for live-link Auto clip

The static ClipForge page (GitHub Pages) cannot download or analyse a
YouTube link — a browser page has no ffmpeg and cannot fetch the video
file. This folder is the **$0 server** that does that job:

```
ClipForge page (paste link, tap "Auto clip from link")
        │  POST /api/clip {url, clip_length, count}
        ▼
Cloudflare Worker  (free plan, src/index.js)  ── front door only
        │  repository_dispatch (GitHub API, PAT)
        ▼
GitHub Actions  (free 2,000 min/month, .github/workflows/auto-clip.yml)
  1. yt-dlp reads the link (VOD or live stream)
  2. audio-only download -> per-second loudness (scripts/auto_clip.py)
  3. top 3 loudest non-overlapping windows of clip_length seconds
  4. cut vertical 1080x1920 MP4 clips (H.264 + AAC, original audio)
  5. publish clips on a GitHub Release tagged job-<job_id>
        │  GET /api/job/:id  (Worker reads the run + release)
        ▼
ClipForge page lists the moments + clip download links in My Clips
```

No paid Cloudflare plan, no paid APIs, no paid GitHub features.
No secret or token is stored in any file in this folder.

---

## Manual deploy steps (do these in order)

### 1. Create the GitHub repo
- On github.com, create a **Public** repo named `clipforge-worker`
  under `aawahid4321-maker`. Public matters: the clip download links
  are GitHub Release assets, and they must open without sign-in.
- Push this folder's contents to the repo's `main` branch
  (`.github/workflows/auto-clip.yml`, `scripts/auto_clip.py`,
  `src/index.js`, `cloudflare.config.ts`, `package.json`,
  `.gitignore`, `README.md`).

### 2. Create the fine-grained PAT (owner's GitHub account)
- GitHub → Settings → Developer settings → Personal access tokens →
  Fine-grained tokens → Generate new token.
- Token name: `clipforge-worker-dispatch`
- Resource owner: `aawahid4321-maker`
- Repository access: **Only select repositories** → `clipforge-worker`
- Repository permissions:
  - **Actions: Read and write** (dispatch the workflow + list runs)
  - **Contents: Read and write** (read releases)
  - Nothing else.
- Expiry: 90 days is fine (set a reminder to rotate it), or No
  expiration. Copy the token once — it is shown only once.
- Do NOT paste the token into any file, chat, or the repo.

### 3. Install the Cloudflare CLI and sign in (owner's machine)
```sh
npm install -g cf        # needs Node.js 22.18 or later
cf auth login            # opens the browser; sign in to Cloudflare
```

### 4. Deploy the Worker
```sh
cd clipforge-worker
cf deploy
```
`cf deploy` prints the Worker URL, e.g.
`https://clipforge-worker.<your-subdomain>.workers.dev` — copy it.

### 5. Set the Worker secret (the PAT from step 2)
`cf` cannot upload a single secret yet, so use Wrangler for this one
command (it does not add a wrangler.json to the project):
```sh
npx wrangler secret put CLIPFORGE_GITHUB_PAT --name clipforge-worker
```
Paste the PAT when asked. It is stored on Cloudflare, never in a file.

### 6. Point ClipForge at the Worker
- Open https://aawahid4321-maker.github.io/clipforge/
- In step 02 "Source & preview", under the YouTube link box, paste the
  Worker URL from step 4 into the Worker URL field and tap Save.
- Paste a YouTube link. The button changes to
  "Auto clip from link (free server)". Tap it and wait — the page polls
  every 20 seconds and the clips appear in My Clips marked
  "Server clip".

---

## Testing without the page

```sh
# Start a job
curl -s -X POST https://clipforge-worker.<subdomain>.workers.dev/api/clip \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=<id>","clip_length":30,"count":3}'
# -> {"job_id":"cf-..."}

# Check it (repeat until status is "done" or "failed")
curl -s https://clipforge-worker.<subdomain>.workers.dev/api/job/cf-...
```

Or run the workflow by hand: GitHub repo → Actions → "ClipForge Auto
Clip" → Run workflow, fill in `job_id` (must match `cf-[a-z0-9-]+`),
`url`, `clip_length`, `count`.

---

## Free-tier limits — read before promising anything

- **GitHub Actions: 2,000 minutes/month free.** A VOD job usually
  takes 2–8 minutes (audio download + analysis + section cuts, no
  full-video download). A live-stream job downloads up to 2 hours of
  video first, so it takes longer. At ~10 jobs/day the budget lasts
  roughly 1–2 weeks; after that jobs queue until the month resets.
  The page shows "Free server: ~2,000 min/month" next to the button.
- **Slowness:** a job is not instant. Expect 2–10 minutes for a VOD,
  longer for a live stream (the runner downloads first, then cuts).
  The page polls every 20 seconds; the user can leave the tab open.
- **YouTube bot-wall (biggest risk):** YouTube sometimes blocks
  downloads from datacenter IPs with "Sign in to confirm you're not a
  bot". When that happens the job fails and the page shows the plain
  error text. There is no permanent fix on the free tier — retry later,
  or use the video file instead. (The page's file-based Auto clip is
  unaffected and stays the fast path.)
- **Live streams:** a stream that is still live has no end, so the job
  uses only the first 2 hours from the stream start and says so in the
  job summary. A finished stream (VOD) is analysed in full.
- **Workers free: 100,000 requests/day** — far above this use.
  The Worker does no video work itself (10 ms CPU limit on free),
  it only passes the link to GitHub and reads back the result.
- **Cut accuracy:** clips start at the nearest keyframe (up to ~2 s
  early) — that is how fast stream cutting works. The clip length is
  exact.
- **Abuse note:** anyone who knows the Worker URL can trigger jobs,
  and the URL is visible in the public page. The only cost is GitHub
  minutes, and the Worker only accepts YouTube links, at most 5 clips
  per job and 600 s per clip. If minutes get burned by strangers, the
  fix is a shared-secret header — not built yet, because a secret in a
  public page is not a secret.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Page button says "Server not set up yet" | Worker URL not pasted/saved in the settings field (or it still contains `YOUR_SUBDOMAIN`). |
| POST /api/clip returns 500 "token not set up" | Step 5 not done, or the secret name is not exactly `CLIPFORGE_GITHUB_PAT`. |
| POST /api/clip returns 502 "dispatch failed (404)" | PAT scope wrong (needs Actions: Read and write on repo `clipforge-worker`), or the repo name is wrong. |
| Job stuck on "queued" for minutes | GitHub Actions queue is busy, or the dispatch did not reach the repo — check the repo's Actions tab. |
| Job "failed" with a yt-dlp / bot message | YouTube blocked the download — see "bot-wall" above. |
| Job "failed", "no release was published" | The workflow crashed before publishing — open the run link the page returns (`run_url`). |
| Clips download but have no audio | The source stream had no audio track, or ffmpeg failed on that format — check the run log. |
