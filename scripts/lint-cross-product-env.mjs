#!/usr/bin/env node
/**
 * Lint rule: no-cross-product-env-vars
 *
 * Viabe Team must never read another product's environment variables.
 * Any identifier prefixed with `REPORTS_` belongs to Viabe Reports and is a
 * cross-product leak — the apps in this repo must own their config surface.
 *
 * Fails CI (exit 1) when a forbidden prefix is referenced anywhere under
 * apps/ or packages/. Exported helpers are unit-tested in the sibling
 * `.test.mjs` file.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Env var name prefixes owned by other Viabe products. */
export const FORBIDDEN_PREFIXES = ['REPORTS_']

/**
 * Deprecated Supabase keys (VT-2 key discipline). Only the publishable +
 * secret keys are permitted; the legacy anon / service-role keys are banned.
 */
export const FORBIDDEN_ENV_NAMES = ['SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY']

const SCAN_DIRS = ['apps', 'packages']
const SCAN_EXTENSIONS = new Set([
  '.ts',
  '.tsx',
  '.js',
  '.jsx',
  '.mjs',
  '.cjs',
  '.py',
  '.yml',
  '.yaml',
])
const IGNORE_DIRS = new Set([
  'node_modules',
  '.next',
  'dist',
  'build',
  '.git',
  '__pycache__',
  '.venv',
])

const PREFIX_PATTERN = new RegExp(
  `\\b(?:${FORBIDDEN_PREFIXES.join('|')})[A-Z0-9_]+\\b`,
  'g',
)

const NAME_PATTERN = new RegExp(`\\b(?:${FORBIDDEN_ENV_NAMES.join('|')})\\b`, 'g')

const isTestFile = (name) => /\.(test|spec)\./.test(name)

/** Return every forbidden env var name referenced in `text`. */
export function scanText(text) {
  return [
    ...new Set([
      ...(text.match(PREFIX_PATTERN) ?? []),
      ...(text.match(NAME_PATTERN) ?? []),
    ]),
  ]
}

function* walk(dir) {
  let entries
  try {
    entries = readdirSync(dir)
  } catch {
    return
  }
  for (const entry of entries) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (!IGNORE_DIRS.has(entry)) yield* walk(full)
      continue
    }
    if (isTestFile(entry)) continue
    if ([...SCAN_EXTENSIONS].some((ext) => entry.endsWith(ext))) yield full
  }
}

/** Scan the repo. Returns `{ violations }` — one entry per forbidden match. */
export function scanRepo(root = process.cwd()) {
  const violations = []
  for (const dir of SCAN_DIRS) {
    for (const file of walk(join(root, dir))) {
      const lines = readFileSync(file, 'utf8').split('\n')
      lines.forEach((line, index) => {
        for (const name of scanText(line)) {
          violations.push({
            file: relative(root, file),
            line: index + 1,
            name,
          })
        }
      })
    }
  }
  return { violations }
}

function main() {
  const { violations } = scanRepo()
  if (violations.length === 0) {
    console.log('no-cross-product-env-vars: ok')
    return
  }
  console.error('no-cross-product-env-vars: forbidden env vars found:\n')
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line}  ${v.name}`)
  }
  console.error(
    `\n${violations.length} violation(s). No cross-product (REPORTS_*) env vars; ` +
      'no deprecated Supabase anon / service-role keys.',
  )
  process.exit(1)
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main()
}
