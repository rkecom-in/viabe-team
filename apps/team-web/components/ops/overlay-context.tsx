'use client'

/**
 * VT-290 — Ops Console V2 overlay primitive.
 *
 * Binding design rule (VT-189): NO separate detail pages — deep views open as
 * right-drawer OVERLAYS over the dimmed listing. This is the shared primitive every
 * sub-row (VT-291..298) uses: a stack-based overlay manager.
 *
 * `useOverlay()` exposes open/close/back; `OverlayPortal` renders the dimmed backdrop +
 * right-drawer for the top of the stack. Each overlay carries its own title + content
 * (the content gets its own filter/action state — it's a normal React subtree).
 */

import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

interface OverlayEntry {
  key: string
  title: string
  content: ReactNode
}

interface OverlayApi {
  open: (entry: OverlayEntry) => void
  close: () => void
  back: () => void
  stack: OverlayEntry[]
}

const OverlayContext = createContext<OverlayApi | null>(null)

export function OverlayProvider({ children }: { children: ReactNode }) {
  const [stack, setStack] = useState<OverlayEntry[]>([])

  const open = useCallback((entry: OverlayEntry) => {
    setStack((s) => [...s, entry])
  }, [])
  const back = useCallback(() => {
    setStack((s) => s.slice(0, -1))
  }, [])
  const close = useCallback(() => {
    setStack([])
  }, [])

  const api = useMemo<OverlayApi>(() => ({ open, close, back, stack }), [open, close, back, stack])

  return (
    <OverlayContext.Provider value={api}>
      {children}
      <OverlayPortal />
    </OverlayContext.Provider>
  )
}

export function useOverlay(): OverlayApi {
  const ctx = useContext(OverlayContext)
  if (!ctx) throw new Error('useOverlay must be used within an OverlayProvider')
  return ctx
}

/** Renders the top overlay as a dimmed backdrop + right-drawer. No-op when empty. */
export function OverlayPortal() {
  const ctx = useContext(OverlayContext)
  if (!ctx || ctx.stack.length === 0) return null
  const top = ctx.stack[ctx.stack.length - 1]
  if (!top) return null
  return (
    <div
      data-ops-overlay-backdrop
      onClick={ctx.close}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.35)',
        zIndex: 1000,
      }}
    >
      <aside
        data-ops-overlay-drawer
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'fixed',
          top: 0,
          right: 0,
          height: '100vh',
          width: 'min(560px, 42vw)',
          background: '#ffffff',
          boxShadow: '-8px 0 24px rgba(0,0,0,0.15)',
          overflowY: 'auto',
          padding: '1rem',
        }}
      >
        <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <strong>{top.title}</strong>
          <span>
            {ctx.stack.length > 1 && (
              <button type="button" onClick={ctx.back} data-ops-overlay-back>
                ← Back
              </button>
            )}
            <button type="button" onClick={ctx.close} data-ops-overlay-close>
              ✕
            </button>
          </span>
        </header>
        <div data-ops-overlay-content>{top.content}</div>
      </aside>
    </div>
  )
}
