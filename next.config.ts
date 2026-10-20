import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  // This is a live dashboard: do not reuse a five-minute-old App Router payload.
  experimental: { staleTimes: { static: 0 } },
};

export default nextConfig;
