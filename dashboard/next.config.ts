import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard reads the Python side's §8 telemetry from <repo>/logs/telemetry.jsonl at request time
  // (see app/api/telemetry/route.ts). Deployed on Vercel; the API route is server-rendered (dynamic).
};

export default nextConfig;
