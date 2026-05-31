#!/usr/bin/env node

import * as readline from "node:readline";
import * as fs from "node:fs";
import * as path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Resolve `typescript` from the chameleon-provisioned node_modules. The
// installer (extractors/typescript.py) sets CHAMELEON_NODE_MODULES to the
// per-user, version-scoped install dir; fall back to the legacy
// <plugin>/mcp/node_modules for dev / older installs.
const nodeModules = process.env.CHAMELEON_NODE_MODULES;
const requireBase = nodeModules
  ? path.join(nodeModules, "..", "package.json")
  : path.resolve(__dirname, "..", "mcp", "package.json");
const require = createRequire(requireBase);
const ts = require("typescript");

const MAX_AST_NODES = 50_000;
const MAX_PARSE_DIAGNOSTICS = 20;
const MAX_FILE_SIZE = 1_000_000;

function getScriptKind(filePath) {
  if (filePath.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (filePath.endsWith(".ts")) return ts.ScriptKind.TS;
  if (filePath.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (filePath.endsWith(".mjs") || filePath.endsWith(".cjs") || filePath.endsWith(".js"))
    return ts.ScriptKind.JS;
  return ts.ScriptKind.JS;
}

function getDefaultExportKind(node) {
  if (
    node.kind === ts.SyntaxKind.ExportAssignment &&
    node.isExportEquals !== true
  ) {
    return ts.SyntaxKind[node.expression.kind];
  }
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
  if (!importClause) return "namespace";
  if (importClause.name) return "default";
  if (importClause.namedBindings) {
    if (importClause.namedBindings.kind === ts.SyntaxKind.NamedImports) return "named";
    return "namespace";
  }
  return "namespace";
}

function extractFile(filePath) {
  let stat;
  try {
    stat = fs.lstatSync(filePath);
  } catch (e) {
    return { path: filePath, error: "read_error", message: String(e?.message ?? e) };
  }
  if (stat.isSymbolicLink()) {
    return { path: filePath, error: "symlink_refused" };
  }
  if (stat.size > MAX_FILE_SIZE) {
    return { path: filePath, error: "file_too_large", size: stat.size };
  }

  let content;
  try {
    content = fs.readFileSync(filePath, "utf8");
  } catch (e) {
    return { path: filePath, error: "read_error", message: String(e?.message ?? e) };
  }

  const scriptKind = getScriptKind(filePath);
  const sourceFile = ts.createSourceFile(
    filePath,
    content,
    ts.ScriptTarget.Latest,
    scriptKind
  );

  const diagnostics = sourceFile.parseDiagnostics ?? [];
  if (diagnostics.length > MAX_PARSE_DIAGNOSTICS) {
    return {
      path: filePath,
      error: "too_many_parse_errors",
      count: diagnostics.length,
    };
  }

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

  for (const stmt of sourceFile.statements) {
    result.top_level_node_kinds.push(ts.SyntaxKind[stmt.kind]);

    const defKind = getDefaultExportKind(stmt);
    if (defKind && result.default_export_kind === null) {
      result.default_export_kind = defKind;
    }

    if (isNamedExportTopLevel(stmt)) {
      if (stmt.kind === ts.SyntaxKind.VariableStatement) {
        result.named_export_count += stmt.declarationList.declarations.length;
      } else {
        result.named_export_count += 1;
      }
    }

    if (
      stmt.kind === ts.SyntaxKind.ExportDeclaration &&
      stmt.exportClause &&
      stmt.exportClause.kind === ts.SyntaxKind.NamedExports
    ) {
      result.named_export_count += stmt.exportClause.elements.length;
    }

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
