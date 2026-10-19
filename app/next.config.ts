import type { NextConfig } from "next";

const config: NextConfig = {
  images: {
    // Stakeholder-supplied override URLs can point anywhere, and the preview
    // is deliberately unprocessed so they get instant confirmation the link
    // resolves. Actual fetching/validation happens in the worker.
    remotePatterns: [{ protocol: "https", hostname: "**" }],
  },
};

export default config;
