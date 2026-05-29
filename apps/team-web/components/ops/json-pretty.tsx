/** VT-234 — minimal JSON pretty-print + collapse + copy.
 *
 * No new deps. Native browser clipboard API; collapsed by default.
 */

'use client'

import { useState } from 'react'

interface JsonPrettyProps {
  label: string
  value: unknown
  defaultOpen?: boolean
}

export function JsonPretty({ label, value, defaultOpen = false }: JsonPrettyProps) {
  const [open, setOpen] = useState(defaultOpen)
  const [copied, setCopied] = useState(false)

  const serialized =
    value === null || value === undefined
      ? ''
      : JSON.stringify(value, null, 2)

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(serialized)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      // Clipboard may be unavailable (insecure context, blocked permission).
      // Silent — operator can still read inline.
    }
  }

  if (serialized === '') {
    return (
      <div data-element="json-empty" className="text-xs text-gray-400 italic">
        {label}: —
      </div>
    )
  }

  return (
    <div
      data-element="json-pretty"
      data-label={label}
      data-open={open ? '1' : '0'}
      className="mt-2"
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="text-xs font-mono text-blue-700 hover:underline"
          data-element="json-toggle"
        >
          {open ? '▼' : '▶'} {label}
        </button>
        {open ? (
          <button
            type="button"
            onClick={onCopy}
            className="text-xs text-gray-500 hover:text-gray-700"
            data-element="json-copy"
          >
            {copied ? 'copied' : 'copy'}
          </button>
        ) : null}
      </div>
      {open ? (
        <pre
          data-element="json-body"
          className="bg-gray-100 rounded p-4 text-xs font-mono overflow-x-auto mt-1"
        >
          {serialized}
        </pre>
      ) : null}
    </div>
  )
}
