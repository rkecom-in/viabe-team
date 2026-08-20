import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import { DM_Sans, Noto_Sans_Devanagari, Space_Grotesk } from 'next/font/google'
import './globals.css'

// Viabe design scheme type pairing: Space Grotesk carries display + UI-bold
// (headings, buttons, step marks), DM Sans carries body copy. Loaded here so
// every surface inherits them rather than each page linking Google Fonts.
const spaceGrotesk = Space_Grotesk({
  subsets: ['latin'],
  weight: ['500', '600', '700'],
  variable: '--font-space-grotesk',
  display: 'swap',
})

// Neither Space Grotesk nor DM Sans ships Devanagari glyphs, and this product is
// bilingual EN/HI on every surface — without this the Hindi copy silently falls back
// to whatever the OS provides and renders in a different face mid-page. Loaded as the
// next family in the stack so Latin still resolves to DM Sans first.
const notoDevanagari = Noto_Sans_Devanagari({
  subsets: ['devanagari'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-noto-devanagari',
  display: 'swap',
})

const dmSans = DM_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-dm-sans',
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'Viabe Team',
  description: 'Viabe Team',
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${spaceGrotesk.variable} ${dmSans.variable} ${notoDevanagari.variable}`}>
      <body className="font-sans">{children}</body>
    </html>
  )
}
