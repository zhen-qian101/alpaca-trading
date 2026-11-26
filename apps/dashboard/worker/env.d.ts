// Bindings that `wrangler types` cannot see. worker-configuration.d.ts is
// generated with --env-file=/dev/null so that a local .dev.vars never leaks
// into the committed types, so these are declared here instead.
//
// ACCESS_TEAM_DOMAIN / ACCESS_AUD are Worker SECRETS on the deployed Worker
// (not wrangler.jsonc vars, which every deploy would reset). They are OPTIONAL
// because the Worker must fail closed while either is missing.
//
// DEV_AUTH_BYPASS === "1" (from apps/dashboard/.dev.vars only) skips the
// Cloudflare Access JWT check for local development. OPTIONAL, because a
// deployed Worker must never have it.
//
// DEV_PEER_KEY is the random key vite.config.ts gives the Worker under
// `vite dev` only (devserver/peerStamp.ts); the bypass needs it. Never in
// wrangler.jsonc, a build or a deploy.
declare namespace Cloudflare {
  interface Env {
    ACCESS_TEAM_DOMAIN?: string
    ACCESS_AUD?: string
    DEV_AUTH_BYPASS?: string
    DEV_PEER_KEY?: string
  }
}

interface Env {
  ACCESS_TEAM_DOMAIN?: string
  ACCESS_AUD?: string
  DEV_AUTH_BYPASS?: string
  DEV_PEER_KEY?: string
}
