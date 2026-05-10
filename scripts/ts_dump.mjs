#!/usr/bin/env node
/**
 * ts_dump.mjs — TypeScript AST extractor.
 *
 * Long-lived Node process consuming file paths from stdin (one per line)
 * and emitting NDJSON ParsedFile records to stdout.
 *
 * Per ARCHITECTURE.md "Cluster signature function" → "Compiler API mode":
 *   - Uses ts.createSourceFile (syntax-only, no type checker)
 *   - Per-file caps: 50k AST nodes, 1 MB file size, 20 parse diagnostics
 *   - Files exceeding any cap emit { error: "<reason>" } and continue
 *   - One file's parse crash never aborts the whole bootstrap
 *
 * Per ARCHITECTURE.md "Performance characteristics" → "ts_dump.mjs batching":
 *   - Long-lived process; TS Compiler loaded once at startup
 *   - Read paths from stdin, emit NDJSON to stdout
 *   - Worker pool managed by Python parent (extractors/typescript.py)
 *
 * Output schema for one parsed file (matches extractors/_base.ParsedFile):
 *   {
 *     "path": str,                          // absolute path
 *     "content_first_200_bytes": str,       // for content_signal matching
 *     "top_level_node_kinds": [str, ...],   // SourceFile.statements[*].kind names
 *     "default_export_kind": str | null,    // FunctionDeclaration | ClassDeclaration | etc., or null
 *     "named_export_count": int,
 *     "import_specifiers": [[str, str], ...], // [(module_name, kind)] where kind ∈ {default, named, namespace}
 *     "has_jsx": bool,
 *     "parse_diagnostics_count": int
 *   }
 *
 * On error for a file:
 *   { "path": str, "error": "<reason>", ... }
 *
 * Reasons: file_too_large | read_error | too_many_parse_errors |
 *          ast_node_ceiling_exceeded | walk_error | extractor_crash
 */

import * as readline from "node:readline";
import * as fs from "node:fs";

// Resolve TypeScript from the Node module path (mcp/node_modules/typescript)
// Phase 2A uses package-managed install (cd mcp && npm install).
// Phase 4 will switch to vendored + checksum-verified.
const ts = await import("typescript").then((m) => m.default);

const MAX_AST_NODES = 50_000;
const MAX_PARSE_DIAGNOSTICS = 20;
const MAX_FILE_SIZE = 1_000_000; // 1 MB

function getScriptKind(filePath) {
  if (filePath.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (filePath.endsWith(".ts")) return ts.ScriptKind.TS;
  if (filePath.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (filePath.endsWith(".mjs") || filePath.endsWith(".cjs") || filePath.endsWith(".js"))
    return ts.ScriptKind.JS;
  return ts.ScriptKind.JS;
}

function getDefaultExportKind(node) {
  // `export default <expr>;` (ExportAssignment)
  if (
    node.kind === ts.SyntaxKind.ExportAssignment &&
    node.isExportEquals !== true
  ) {
    return ts.SyntaxKind[node.expression.kind];
  }
  // `export default function foo() {}` / `export default class Foo {}`
  if (
    (node.kind === ts.SyntaxKind.FunctionDeclaration ||
      node.kind === ts.SyntaxKind.ClassDeclaration) &&
    Array.isArray(node.modifiers) &&
    node.modifiers.some((m) => m.kind === ts.SyntaxKind.DefaultKeyword)
  ) {
    return ts.SyntaxKind[node.kind];
  }
  return null;
}

function isNamedExportTopLevel(node) {
  // Top-level statements like `export const x = ...;` / `export function f() {}`
  // Excludes `export default ...`.
  if (!Array.isArray(node.modifiers)) return false;
  const hasExport = node.modifiers.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
  const hasDefault = node.modifiers.some((m) => m.kind === ts.SyntaxKind.DefaultKeyword);
  if (!hasExport || hasDefault) return false;
  return (
    node.kind === ts.SyntaxKind.VariableStatement ||
    node.kind === ts.SyntaxKind.FunctionDeclaration ||
    node.kind === ts.SyntaxKind.ClassDeclaration ||
    node.kind === ts.SyntaxKind.EnumDeclaration ||
    node.kind === ts.SyntaxKind.InterfaceDeclaration ||
    node.kind === ts.SyntaxKind.TypeAliasDeclaration
  );
}

function importKindFor(importClause) {
  if (!importClause) return "namespace"; // bare `import "module";` — treat as namespace-ish
  if (importClause.name) return "default";
  if (importClause.namedBindings) {
    if (importClause.namedBindings.kind === ts.SyntaxKind.NamedImports) return "named";
    return "namespace";
  }
  return "namespace";
}

function extractFile(filePath) {
  // 1. File size cap
  let stat;
  try {
    stat = fs.statSync(filePath);
  } catch (e) {
    return { path: filePath, error: "read_error", message: String(e?.message ?? e) };
  }
  if (stat.size > MAX_FILE_SIZE) {
    return { path: filePath, error: "file_too_large", size: stat.size };
  }

  // 2. Read content
  let content;
  try {
    content = fs.readFileSync(filePath, "utf8");
  } catch (e) {
    return { path: filePath, error: "read_error", message: String(e?.message ?? e) };
  }

  // 3. Parse
  const scriptKind = getScriptKind(filePath);
  const sourceFile = ts.createSourceFile(
    filePath,
    content,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    scriptKind
  );

  // 4. Parse diagnostics ceiling
  const diagnostics = sourceFile.parseDiagnostics ?? [];
  if (diagnostics.length > MAX_PARSE_DIAGNOSTICS) {
    return {
      path: filePath,
      error: "too_many_parse_errors",
      count: diagnostics.length,
    };
  }

  // 5. Build the 7-tuple components (Python signature function combines them)
  const result = {
    path: filePath,
    content_first_200_bytes: content.slice(0, 200),
    top_level_node_kinds: [],
    default_export_kind: null,
    named_export_count: 0,
    import_specifiers: [],
    has_jsx: false,
    parse_diagnostics_count: diagnostics.length,
  };

  // Top-level node kinds (direct children of SourceFile)
  for (const stmt of sourceFile.statements) {
    result.top_level_node_kinds.push(ts.SyntaxKind[stmt.kind]);

    // Default export at top level
    const defKind = getDefaultExportKind(stmt);
    if (defKind && result.default_export_kind === null) {
      result.default_export_kind = defKind;
    }

    // Top-level named export
    if (isNamedExportTopLevel(stmt)) {
      // VariableStatement contains a list; count its declarations
      if (stmt.kind === ts.SyntaxKind.VariableStatement) {
        result.named_export_count += stmt.declarationList.declarations.length;
      } else {
        result.named_export_count += 1;
      }
    }

    // ExportDeclaration with named bindings (`export { a, b };`)
    if (
      stmt.kind === ts.SyntaxKind.ExportDeclaration &&
      stmt.exportClause &&
      stmt.exportClause.kind === ts.SyntaxKind.NamedExports
    ) {
      result.named_export_count += stmt.exportClause.elements.length;
    }

    // Top-level import (only at top level — `import` inside functions is rare)
    if (stmt.kind === ts.SyntaxKind.ImportDeclaration) {
      const moduleName =
        stmt.moduleSpecifier && typeof stmt.moduleSpecifier.text === "string"
          ? stmt.moduleSpecifier.text
          : null;
      if (moduleName) {
        result.import_specifiers.push([moduleName, importKindFor(stmt.importClause)]);
      }
    }
  }

  // Walk AST for JSX detection (recursive — JSX can be nested anywhere)
  let nodeCount = 0;
  let walkError = null;
  function visit(node) {
    nodeCount++;
    if (nodeCount > MAX_AST_NODES) {
      walkError = "ast_node_ceiling_exceeded";
      return;
    }

    if (
      node.kind === ts.SyntaxKind.JsxElement ||
      node.kind === ts.SyntaxKind.JsxSelfClosingElement ||
      node.kind === ts.SyntaxKind.JsxFragment
    ) {
      result.has_jsx = true;
    }

    ts.forEachChild(node, visit);
  }
  try {
    visit(sourceFile);
  } catch (e) {
    return {
      path: filePath,
      error: "walk_error",
      message: String(e?.message ?? e),
    };
  }
  if (walkError) {
    return { path: filePath, error: walkError };
  }

  return result;
}

// Main loop: read file paths from stdin, emit NDJSON results to stdout
const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
  const filePath = line.trim();
  if (!filePath) return;
  try {
    const result = extractFile(filePath);
    process.stdout.write(JSON.stringify(result) + "\n");
  } catch (e) {
    process.stdout.write(
      JSON.stringify({
        path: filePath,
        error: "extractor_crash",
        message: String(e?.message ?? e),
      }) + "\n"
    );
  }
});

rl.on("close", () => {
  process.exit(0);
});
