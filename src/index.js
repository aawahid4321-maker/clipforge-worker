// ClipForge Worker — free front door for server Auto clip.
//
// What it does:
//   POST /api/clip {url, clip_length, count}
//     -> validates the YouTube link, makes a job id, and triggers the
//        GitHub Actions workflow in aawahid4321-maker/clipforge-worker
//        via repository_dispatch. Returns {job_id}.
//   GET /api/job/:id
//     -> finds the workflow run for that job id, and when it is done
//        reads the job's GitHub Release (tag job-<job_id>) for the
//        clip download links, the moments, or the error text.
//        Returns {status: queued|running|done|failed, clips, moments, error}.
//
// FREE PLAN ONLY: no KV, D1, R2, Queues or any paid binding. All state
// lives in GitHub (workflow runs + releases). The only secret is
// CLIPFORGE_GITHUB_PAT, a fine-grained GitHub PAT (Actions: Read and
// write, Contents: Read and write, repo clipforge-worker only) set
// with: npx wrangler secret put CLIPFORGE_GITHUB_PAT --name clipforge-worker
//
// CORS is open to the ClipForge page only:
// https://aawahid4321-maker.github.io

const OWNER = "aawahid4321-maker";
const REPO = "clipforge-worker";
const WORKFLOW_FILE = "auto-clip.yml";
const ALLOWED_ORIGIN = "https://aawahid4321-maker.github.io";
const GH_API = "https://api.github.com";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

function isYouTubeUrl(raw) {
  try {
    const u = new URL(raw);
    const host = u.hostname.replace(/^www\./, "");
    if (host === "youtu.be") return /^\/[A-Za-z0-9_-]{6,}$/.test(u.pathname);
    if (host === "youtube.com" || host.endsWith(".youtube.com")) {
      return (
        u.pathname === "/watch" ||
        u.pathname.startsWith("/live/") ||
        u.pathname.startsWith("/shorts/") ||
        u.pathname.startsWith("/embed/")
      );
    }
    return false;
  } catch {
    return false;
  }
}

function gh(path, token, opts = {}) {
  return fetch(GH_API + path, {
    ...opts,
    headers: {
      Authorization: "Bearer " + token,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "clipforge-worker",
      ...(opts.headers || {}),
    },
  });
}

async function getRelease(jobId, token) {
  const res = await gh(`/repos/${OWNER}/${REPO}/releases/tags/job-${jobId}`, token);
  if (!res.ok) return null;
  return res.json();
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (request.method === "GET" && url.pathname === "/") {
      return json({ ok: true, service: "clipforge-worker" });
    }

    // ---- POST /api/clip: start a job -------------------------------------
    if (request.method === "POST" && url.pathname === "/api/clip") {
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "Invalid JSON body" }, 400);
      }
      const rawUrl = String(body.url || "").trim();
      if (!isYouTubeUrl(rawUrl)) {
        return json({ error: "Not a YouTube watch / live / shorts link" }, 400);
      }
      let clipLength = parseInt(body.clip_length, 10);
      if (!isFinite(clipLength)) clipLength = 30;
      clipLength = Math.max(5, Math.min(600, clipLength));
      let count = parseInt(body.count, 10);
      if (!isFinite(count)) count = 3;
      count = Math.max(1, Math.min(5, count));

      const pat = env.CLIPFORGE_GITHUB_PAT;
      if (!pat) {
        return json(
          { error: "Server token not set up yet (CLIPFORGE_GITHUB_PAT)" },
          500
        );
      }

      const jobId =
        "cf-" + Date.now().toString(36) + "-" + Math.random().toString(16).slice(2, 8);
      const res = await gh(`/repos/${OWNER}/${REPO}/dispatches`, pat, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event_type: "auto-clip",
          client_payload: {
            job_id: jobId,
            url: rawUrl,
            clip_length: clipLength,
            count,
          },
        }),
      });
      if (res.status !== 204) {
        const text = await res.text().catch(() => "");
        return json(
          { error: "GitHub dispatch failed (" + res.status + "): " + text.slice(0, 200) },
          502
        );
      }
      return json({ job_id: jobId });
    }

    // ---- GET /api/job/:id: job status ------------------------------------
    const m = url.pathname.match(/^\/api\/job\/([A-Za-z0-9-]+)$/);
    if (request.method === "GET" && m) {
      const jobId = m[1];
      if (!/^cf-[a-z0-9-]+$/.test(jobId)) {
        return json({ error: "Bad job id" }, 400);
      }
      const pat = env.CLIPFORGE_GITHUB_PAT;
      if (!pat) {
        return json(
          { error: "Server token not set up yet (CLIPFORGE_GITHUB_PAT)" },
          500
        );
      }

      const runsRes = await gh(
        `/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}/runs?per_page=20`,
        pat
      );
      if (!runsRes.ok) {
        return json({ error: "GitHub API error (" + runsRes.status + ")" }, 502);
      }
      const runs = await runsRes.json();
      const run = (runs.workflow_runs || []).find((r) =>
        (r.displayTitle || "").includes(jobId)
      );
      if (!run) return json({ status: "queued", job_id: jobId });
      if (run.status === "queued") {
        return json({ status: "queued", job_id: jobId, run_url: run.html_url });
      }
      if (run.status === "in_progress") {
        return json({ status: "running", job_id: jobId, run_url: run.html_url });
      }

      // Run finished — read the job's release for clips or the error.
      const rel = await getRelease(jobId, pat);
      if (!rel) {
        return json({
          status: "failed",
          job_id: jobId,
          run_url: run.html_url,
          error: "Job finished but no release was published for it.",
        });
      }
      const assets = rel.assets || [];
      const errAsset = assets.find((a) => a.name === "error.txt");
      if (errAsset) {
        let errText = "Job failed.";
        try {
          const t = await fetch(errAsset.browser_download_url);
          errText = (await t.text()).trim() || errText;
        } catch {
          // keep the default
        }
        return json({
          status: "failed",
          job_id: jobId,
          run_url: run.html_url,
          error: errText.slice(0, 500),
        });
      }
      const clips = assets
        .filter((a) => /\.mp4$/i.test(a.name))
        .map((a) => ({ name: a.name, url: a.browser_download_url, size: a.size }));
      let moments = [];
      const momAsset = assets.find((a) => a.name === "moments.json");
      if (momAsset) {
        try {
          const t = await fetch(momAsset.browser_download_url);
          moments = (await t.json()).moments || [];
        } catch {
          // keep moments empty
        }
      }
      if (!clips.length) {
        return json({
          status: "failed",
          job_id: jobId,
          run_url: run.html_url,
          error: "Job finished but no clips were published.",
        });
      }
      return json({
        status: "done",
        job_id: jobId,
        run_url: run.html_url,
        clips,
        moments,
      });
    }

    return json({ error: "Not found" }, 404);
  },
};
