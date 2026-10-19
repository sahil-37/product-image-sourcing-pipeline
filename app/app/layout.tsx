import type { Metadata } from "next";

import "./globals.css";
// Detail-page, review-control and banner styles. Never imported before, so
// /product/[id] rendered unstyled (single column, bare browser buttons).
import "../globals.css";

export const metadata: Metadata = {
  title: "Catalogue Image Review",
  description: "Verify auto-sourced product images and supply replacements.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
