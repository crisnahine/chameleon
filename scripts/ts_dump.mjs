#!/usr/bin/env node
/**
 * ts_dump.mjs — Phase 1C placeholder.
 *
 * Long-lived Node process that consumes file paths from stdin (NDJSON)
 * and emits AST extraction results to stdout (NDJSON).
 *
 * Phase 2 implementation will:
 *   1. Load TypeScript Compiler API once at startup (vendored at
 *      mcp/node_modules/typescript, integrity-verified by typescript-checksums.json).
 *   2. Read file paths from stdin line-by-line.
 *   3. For each path: parse via ts.createSourceFile (syntax-only), extract
 *      the 7-tuple cluster signature components, emit as NDJSON to stdout.
 *   4. Skip files with > 20 parse diagnostics. Apply 50k AST node ceiling.
 *   5. Worker pool managed by Python parent (mcp/chameleon_mcp/extractors/typescript.py).
 *
 * See ARCHITECTURE.md sections:
 *   - "Cluster signature function" — what to extract
 *   - "TypeScript-first extractor" — vendoring + integrity strategy
 *   - "Performance characteristics" → "ts_dump.mjs batching"
 */

// Phase 1C: minimal stub — exits immediately with no work done.
// Phase 2 replaces with real worker loop.

process.stderr.write("ts_dump.mjs: Phase 1C placeholder. Real implementation in Phase 2.\n");
process.exit(0);
