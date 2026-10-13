import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The browser talks to /graphql on this origin; Next proxies it to the
  // Python API, so no CORS setup is needed in development or production.
  async rewrites() {
    return [{ source: "/graphql", destination: `${backendUrl}/graphql` }];
  },
};

export default nextConfig;
