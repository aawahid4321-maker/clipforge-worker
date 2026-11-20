// ClipForge Worker config — `cf` CLI style (no wrangler.json).
// See https://developers.cloudflare.com/cf/get-started/first-worker/
import { defineConfig } from "cf/config";
import * as entrypoint from "./src/index.js" with { type: "cf-worker" };

export default defineConfig({
  worker: {
    name: "clipforge-worker",
    compatibilityDate: "2026-11-20",
    entrypoint,
    // No bindings: free plan only. The only secret is
    // CLIPFORGE_GITHUB_PAT, set with:
    //   npx wrangler secret put CLIPFORGE_GITHUB_PAT --name clipforge-worker
  },
});
