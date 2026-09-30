const BACKEND = process.env.SAGE_API ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/chat/approve", destination: `${BACKEND}/chat/approve` },
      { source: "/chat/stream", destination: `${BACKEND}/chat/stream` },
      { source: "/chat", destination: `${BACKEND}/chat` },
      { source: "/notifications", destination: `${BACKEND}/notifications` },
      { source: "/info", destination: `${BACKEND}/info` },
      { source: "/api/health", destination: `${BACKEND}/health` },
    ];
  },
};

export default nextConfig;
