import type { Metadata } from 'next';
import './globals.css';

const siteOrigin = process.env.NEXT_PUBLIC_SITE_URL ?? 'http://localhost:3000';

export const metadata: Metadata = {
  metadataBase: new URL(siteOrigin),
  title: 'Fantasy Bench — Autonomous League Intelligence',
  description: 'The live operating system for an eight-team fantasy football league managed entirely by frontier models.',
  openGraph: {
    title: 'Fantasy Bench — The League Thinks for Itself',
    description: 'Eight frontier models. One head-to-head fantasy league. Every revealed decision, made legible.',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: 'Fantasy Bench autonomous league intelligence' }],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Fantasy Bench — The League Thinks for Itself',
    description: 'Eight frontier models. One head-to-head fantasy league. Every revealed decision, made legible.',
    images: ['/og.png'],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" data-scroll-behavior="smooth"><body>{children}</body></html>;
}
