import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  // Serve under a sub-path when NEXT_BASE_PATH is set at build time (e.g. '/solinteg' behind
  // a reverse proxy that does NOT strip the prefix). Unset → served at the root, so local dev
  // and the reference NUC deployment are unaffected. Must be baked at `next build`.
  basePath: process.env.NEXT_BASE_PATH || undefined,
};

export default nextConfig;
