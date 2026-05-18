#!/usr/bin/env node
/**
 * Lint rule: no-cross-product-env-vars
 *
 * Three checks, all failing CI (exit 1) on violation:
 *
 * 1. Cross-product vars — any `REPORTS_*` identifier (Viabe Reports) under
 *    apps/ or packages/ source. Viabe Team owns its own config surface.
 * 2. Deprecated Supabase keys — `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY`
 *    in source (VT-2 key discipline: publishable + secret keys only).
 * 3. Per-app .env.example separation — server secrets must not appear in the
 *    web app's env file, and NEXT_PUBLIC_* vars must not appear in a backend
 *    app's env file (frontend-leakage / deployment-scoping guard).
 *
 * Exported helpers are unit-tested in the sibling `.test.mjs` file.
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

/** Name suffixes that mark a var as a server secret (must stay backend-only). */
const SECRET_SUFFIX = /(?:_SECRET_KEY|_AUTH_TOKEN|_API_KEY)$/

/**
 * Server-only vars permitted in apps/team-web/.env.example by exact name.
 * Next.js route handlers run server-side and legitimately need these — they
 * are never bundled to the client. Added in VT-3.3b. Any OTHER non-public
 * secret-suffixed var in the web env is still rejected.
 */
const WEB_ENV_WHITELIST = new Set(['TEAM_TWILIO_AUTH_TOKEN', 'INTERNAL_API_SECRET'])

/**
 * Per-app .env.example files and which side of the frontend/backend boundary
 * each sits on. `frontend` files may hold only NEXT_PUBLIC_* (and other
 * non-secret) vars; `backend` files must hold no NEXT_PUBLIC_* vars.
 */
const ENV_EXAMPLE_RULES = [
  { file: 'apps/team-web/.env.example', side: 'frontend' },
  { file: 'apps/team-orchestrator/.env.example', side: 'backend' },
  { file: 'apps/team-ingestion-worker/.env.example', side: 'backend' },
]

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

/**
 * Check one env var name against a per-app .env.example rule.
 * Returns `null` if allowed, or a violation reason string.
 */
export function envVarViolation(side, name) {
  const isPublic = name.startsWith('NEXT_PUBLIC_')
  if (
    side === 'frontend' &&
    !isPublic &&
    SECRET_SUFFIX.test(name) &&
    !WEB_ENV_WHITELIST.has(name)
  ) {
    return 'server secret in the web app env (move to a backend app .env.example)'
  }
  if (side === 'backend' && isPublic) {
    return 'NEXT_PUBLIC_ var in a backend app env (belongs in apps/team-web/.env.example)'
  }
  return null
}

/** Extract `NAME` from each `NAME=...` declaration line of a .env file. */
function envVarDeclarations(text) {
  const decls = []
  text.split('\n').forEach((line, index) => {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/)
    if (match) decls.push({ name: match[1], line: index + 1 })
  })
  return decls
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

  // Checks 1 + 2: forbidden identifiers in source.
  for (const dir of SCAN_DIRS) {
    for (const file of walk(join(root, dir))) {
      const lines = readFileSync(file, 'utf8').split('\n')
      lines.forEach((line, index) => {
        for (const name of scanText(line)) {
          violations.push({ file: relative(root, file), line: index + 1, name })
        }
      })
    }
  }

  // Check 3: per-app .env.example frontend/backend separation.
  for (const { file, side } of ENV_EXAMPLE_RULES) {
    let text
    try {
      text = readFileSync(join(root, file), 'utf8')
    } catch {
      continue
    }
    for (const { name, line } of envVarDeclarations(text)) {
      const reason = envVarViolation(side, name)
      if (reason) violations.push({ file, line, name: `${name} — ${reason}` })
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
    `\n${violations.length} violation(s). No cross-product (REPORTS_*) vars; ` +
      'no deprecated Supabase anon / service-role keys; ' +
      'no server secrets in the web env; no NEXT_PUBLIC_* in a backend env.',
  )
  process.exit(1)
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main()
}
