import "./globals.css";

export const metadata = {
  title: "sage :: terminal",
  description: "SAGE personal intelligence console",
};

export const viewport = {
  themeColor: "#000000",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-black">{children}</body>
    </html>
  );
}
