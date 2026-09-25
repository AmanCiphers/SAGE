import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "sage :: control",
  description: "NVIDIA NIM powered personal AI harness",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-black">{children}</body>
    </html>
  );
}