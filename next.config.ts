import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  // Serve under a sub-path when NEXT_BASE_PATH is set at build time (e.g. '/solinteg' behind
  // a reverse proxy that does NOT strip the prefix). Unset → served at the root, so local dev
  // and the reference NUC deployment are unaffected. Must be baked at `next build`.
  basePath: process.env.NEXT_BASE_PATH || undefined,
  // Surface the base path to the client bundle (raw fetch() isn't auto-prefixed — see
  // lib/base-path.ts). Empty string when unset, so nothing changes off the sub-path deploy.
  env: {
    NEXT_PUBLIC_BASE_PATH: process.env.NEXT_BASE_PATH || '',
  },
};

export default nextConfig;
