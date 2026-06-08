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
// A real source file declares a few dozen callables; a generated mega-module
// can declare thousands. Cap the recorded headers so one outlier file cannot
// bloat the dump record (the consensus only needs a representative sample).
const MAX_CALLABLE_SIGNATURES = 200;

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
    node.kind === ts.SyntaxKind.TypeAliasDeclaration ||
    node.kind === ts.SyntaxKind.ModuleDeclaration
  );
}

// Pull the exported binding names off a single top-level statement and push
// them into `names`. This drives the phantom-symbol check: an import of a name
// absent from the resolved module's export set is a hallucinated binding. The
// set must be complete or the check self-defeats, so it covers direct
// `export const|function|class|...`, `export { a as b }` clauses (the EXPORTED
// name, not the local one), and `export { x } from './m'` re-exports.
//
// `export * from './m'` cannot be enumerated statically without resolving the
// re-export chain, so any statement of that shape flips `state.exportSetOpen`
// and the caller marks the whole file's export set as non-authoritative. The
// phantom-symbol check then skips imports from such a file, mirroring the
// conservative skip-on-ambiguity stance the path check already takes.
// Collect every identifier bound by a declaration target. A simple binding is
// an Identifier (`.text`); a destructuring export (`export const { a, b: c,
// ...rest } = f()`) binds through an Object/ArrayBindingPattern whose node
// carries no `.text`, so the names live one level down in `.elements`. Left
// unwalked those names vanish from the export set while the file is still
// marked authoritative (`open: false`), and the phantom-symbol check then
// flags the (real) imports of them as hallucinated. Recurse so each bound name
// is recorded; array holes (`[, a]`) are OmittedExpression with no `.name`.
function collectBindingNames(name, names) {
  if (!name) return;
  if (typeof name.text === "string") {
    names.add(name.text);
    return;
  }
  if (
    name.kind === ts.SyntaxKind.ObjectBindingPattern ||
    name.kind === ts.SyntaxKind.ArrayBindingPattern
  ) {
    for (const el of name.elements) {
      if (el.name) collectBindingNames(el.name, names);
    }
  }
}

function collectExportNames(node, names, state) {
  // export const|let|var|function|class|interface|type|enum|namespace foo
  if (isNamedExportTopLevel(node)) {
    if (node.kind === ts.SyntaxKind.VariableStatement) {
      for (const decl of node.declarationList.declarations) {
        collectBindingNames(decl.name, names);
      }
    } else if (node.name && typeof node.name.text === "string") {
      names.add(node.name.text);
    }
    return;
  }

  if (node.kind === ts.SyntaxKind.ExportDeclaration) {
    // `export * from './m'` (no exportClause) re-exports an unknown set; mark
    // the file's set as open so the symbol check skips imports from it.
    if (!node.exportClause) {
      state.exportSetOpen = true;
      return;
    }
    // `export * as ns from './m'` binds a single namespace name.
    if (node.exportClause.kind === ts.SyntaxKind.NamespaceExport) {
      if (node.exportClause.name && typeof node.exportClause.name.text === "string") {
        names.add(node.exportClause.name.text);
      }
      return;
    }
    // `export { a, b as c }` / `export { x } from './m'`: each element's
    // `name` is the EXPORTED identifier (the alias when `as` is present), which
    // is what an importer references.
    if (node.exportClause.kind === ts.SyntaxKind.NamedExports) {
      for (const el of node.exportClause.elements) {
        if (el.name && typeof el.name.text === "string") {
          names.add(el.name.text);
        }
      }
    }
  }
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

// Pull the IMPORTED binding names off a single `import { a, b as c } from './m'`
// statement and push `{ name, module, line }` rows into `out`. The recorded
// `name` is the name as the SOURCE module exports it (the left side of `as`),
// which is what the reverse index keys on: who-imports-`editPrice` does not care
// what the importer locally calls it. Type-only imports (`import type { T }`
// and inline `import { type T }`) are skipped -- they reference a type position,
// not a value binding, so removing the export does not break them at runtime.
// Default and namespace imports carry no named binding here and are ignored.
function collectImportSymbols(node, out, sourceFile) {
  if (node.kind !== ts.SyntaxKind.ImportDeclaration) return;
  const moduleName =
    node.moduleSpecifier && typeof node.moduleSpecifier.text === "string"
      ? node.moduleSpecifier.text
      : null;
  if (!moduleName) return;
  const clause = node.importClause;
  if (!clause || clause.isTypeOnly) return;
  const bindings = clause.namedBindings;
  if (!bindings || bindings.kind !== ts.SyntaxKind.NamedImports) return;
  let line = null;
  try {
    line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
  } catch (e) {
    line = null;
  }
  for (const el of bindings.elements) {
    if (el.isTypeOnly) continue; // `import { type Foo }` -> type position
    // `propertyName` is the source-exported name when an `as` alias is present;
    // otherwise `name` is both the imported and local name.
    const exported = el.propertyName && typeof el.propertyName.text === "string"
      ? el.propertyName.text
      : el.name && typeof el.name.text === "string"
        ? el.name.text
        : null;
    if (!exported || exported === "default") continue;
    out.push({ name: exported, module: moduleName, line });
  }
}

// Pull a readable callable name off a declaration. Covers the cases a
// signature contract cares about: a named function/method, an accessor, a
// constructor, and a top-level `const foo = (...) => {}` whose name lives on
// the binding rather than the arrow itself. Anonymous callables (inline
// callbacks, default-exported arrows) return null and are skipped: there is
// no stable name to key a contract on.
function callableNameOf(node) {
  if (node.kind === ts.SyntaxKind.Constructor) return "constructor";
  if (node.name && typeof node.name.text === "string") return node.name.text;
  // `export const foo = () => {}` / `const foo = function () {}`: the arrow or
  // function expression sits inside a VariableDeclaration that carries the name.
  const parent = node.parent;
  if (
    parent &&
    parent.kind === ts.SyntaxKind.VariableDeclaration &&
    parent.name &&
    typeof parent.name.text === "string"
  ) {
    return parent.name.text;
  }
  return null;
}

// A short, stable kind label for the callable. The body-shape walk already
// distinguishes function-like nodes; this maps them onto the handful of kinds
// a reader reasons about when comparing an override against the contract.
function callableKindOf(node) {
  switch (node.kind) {
    case ts.SyntaxKind.FunctionDeclaration:
    case ts.SyntaxKind.FunctionExpression:
    case ts.SyntaxKind.ArrowFunction:
      return "function";
    case ts.SyntaxKind.MethodDeclaration:
      return "method";
    case ts.SyntaxKind.Constructor:
      return "constructor";
    case ts.SyntaxKind.GetAccessor:
      return "getter";
    case ts.SyntaxKind.SetAccessor:
      return "setter";
    default:
      return "function";
  }
}

// Structured parameter shape for one callable: each entry carries the binding
// name (or a placeholder for a destructured/rest binding), whether it is
// optional (a `?` marker or a default value makes it droppable), and its kind.
// This is the unit a contract comparison needs: positional count plus which
// names are required, without dragging in the parameter's type or body.
function paramShapesOf(node) {
  if (!Array.isArray(node.parameters)) return [];
  return node.parameters.map((p) => {
    const isRest = !!p.dotDotDotToken;
    let name;
    let kind = "positional";
    if (p.name && typeof p.name.text === "string") {
      name = p.name.text;
    } else if (
      p.name &&
      (p.name.kind === ts.SyntaxKind.ObjectBindingPattern ||
        p.name.kind === ts.SyntaxKind.ArrayBindingPattern)
    ) {
      // A destructured parameter has no single name; the contract still cares
      // that the positional slot exists, so record it with a stable marker.
      name = "{}";
      kind = "destructured";
    } else {
      name = "_";
    }
    if (isRest) kind = "rest";
    const optional = !!p.questionToken || p.initializer !== undefined || isRest;
    return { name, optional, kind };
  });
}

function isDefaultExportNode(node) {
  return (
    Array.isArray(node.modifiers) &&
    node.modifiers.some((m) => m.kind === ts.SyntaxKind.DefaultKeyword)
  );
}

// Function-like nodes that open a new body-shape frame. Getters/setters and
// constructors count too: they carry the same overlong/deeply-nested risk as
// a plain method.
function isFunctionLike(node) {
  return (
    node.kind === ts.SyntaxKind.FunctionDeclaration ||
    node.kind === ts.SyntaxKind.MethodDeclaration ||
    node.kind === ts.SyntaxKind.FunctionExpression ||
    node.kind === ts.SyntaxKind.ArrowFunction ||
    node.kind === ts.SyntaxKind.Constructor ||
    node.kind === ts.SyntaxKind.GetAccessor ||
    node.kind === ts.SyntaxKind.SetAccessor
  );
}

// Control-flow nodes that add a nesting level and contribute a decision point.
// Switch is counted once at the SwitchStatement; each CaseClause adds a branch
// so a long flat dispatch reads as branchy rather than deep. The set is the
// cyclomatic-complexity decision points minus boolean operators (those would
// require token-level walking and add noise without changing the verdict).
function isBranchNode(node) {
  switch (node.kind) {
    case ts.SyntaxKind.IfStatement:
    case ts.SyntaxKind.ForStatement:
    case ts.SyntaxKind.ForInStatement:
    case ts.SyntaxKind.ForOfStatement:
    case ts.SyntaxKind.WhileStatement:
    case ts.SyntaxKind.DoStatement:
    case ts.SyntaxKind.CaseClause:
    case ts.SyntaxKind.CatchClause:
    case ts.SyntaxKind.ConditionalExpression:
      return true;
    default:
      return false;
  }
}

// Branch nodes that also open a nested block (so they raise max_depth). A
// CaseClause and a ConditionalExpression add a decision point but not a
// structural indent level, so they count toward branch_count only.
function isNestingNode(node) {
  switch (node.kind) {
    case ts.SyntaxKind.IfStatement:
    case ts.SyntaxKind.ForStatement:
    case ts.SyntaxKind.ForInStatement:
    case ts.SyntaxKind.ForOfStatement:
    case ts.SyntaxKind.WhileStatement:
    case ts.SyntaxKind.DoStatement:
    case ts.SyntaxKind.SwitchStatement:
    case ts.SyntaxKind.TryStatement:
    case ts.SyntaxKind.CatchClause:
      return true;
    default:
      return false;
  }
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
  // setParentNodes (5th arg) is on so a `const foo = () => {}` arrow can read
  // its binding name off the enclosing VariableDeclaration when recording the
  // callable signature. The body-shape walk does not need parents, but the
  // signature extraction does and it is cheaper than a second pass.
  const sourceFile = ts.createSourceFile(
    filePath,
    content,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
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
    named_export_names: [],
    export_set_open: false,
    import_specifiers: [],
    import_symbols: [],
    has_jsx: false,
    parse_diagnostics_count: diagnostics.length,
    function_scopes: [],
    callable_signatures: [],
  };

  const exportNameSet = new Set();
  const exportState = { exportSetOpen: false };

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

    collectExportNames(stmt, exportNameSet, exportState);

    if (stmt.kind === ts.SyntaxKind.ImportDeclaration) {
      const moduleName =
        stmt.moduleSpecifier && typeof stmt.moduleSpecifier.text === "string"
          ? stmt.moduleSpecifier.text
          : null;
      if (moduleName) {
        result.import_specifiers.push([moduleName, importKindFor(stmt.importClause)]);
      }
      collectImportSymbols(stmt, result.import_symbols, sourceFile);
    }
  }

  result.export_set_open = exportState.exportSetOpen;
  // Sorted for a stable, reproducible record (the index is committed and
  // hashed into the trust SHA). The name set is small even for a barrel, so the
  // sort cost is negligible.
  result.named_export_names = Array.from(exportNameSet).sort();

  let nodeCount = 0;
  let walkError = null;
  // Active body-shape frames, innermost last. Each frame tracks its own max
  // nesting depth and branch count so a helper closure nested inside a method
  // is measured independently of its enclosing function.
  const frameStack = [];

  function startLineOf(node) {
    try {
      return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
    } catch (e) {
      return null;
    }
  }
  function endLineOf(node) {
    try {
      return sourceFile.getLineAndCharacterOfPosition(node.getEnd()).line + 1;
    } catch (e) {
      return null;
    }
  }

  function paramCountOf(node) {
    return Array.isArray(node.parameters) ? node.parameters.length : 0;
  }

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

    const isFn = isFunctionLike(node);
    if (isFn) {
      const start = startLineOf(node);
      const end = endLineOf(node);
      frameStack.push({
        start_line: start,
        end_line: end,
        line_span: start !== null && end !== null ? end - start + 1 : null,
        param_count: paramCountOf(node),
        max_depth: 0,
        branch_count: 0,
        depth: 0,
      });
      // Record the declaration header (name + param shape) for named callables
      // only. Anonymous inline callbacks have no stable name to anchor a
      // signature contract, so they are skipped rather than recorded as noise.
      const callableName = callableNameOf(node);
      if (callableName !== null && result.callable_signatures.length < MAX_CALLABLE_SIGNATURES) {
        result.callable_signatures.push({
          name: callableName,
          kind: callableKindOf(node),
          params: paramShapesOf(node),
          is_default_export: isDefaultExportNode(node),
          // Body span for the duplication catalog's body-hash fallback: a
          // body-exact clone whose name shares no tokens with the original
          // can only be paired by body identity.
          start_line: start,
          end_line: end,
        });
      }
    } else if (frameStack.length > 0) {
      const frame = frameStack[frameStack.length - 1];
      if (isBranchNode(node)) {
        frame.branch_count++;
      }
      if (isNestingNode(node)) {
        frame.depth++;
        if (frame.depth > frame.max_depth) {
          frame.max_depth = frame.depth;
        }
      }
    }

    ts.forEachChild(node, visit);

    if (isFn) {
      const frame = frameStack.pop();
      result.function_scopes.push({
        start_line: frame.start_line,
        end_line: frame.end_line,
        line_span: frame.line_span,
        max_depth: frame.max_depth,
        branch_count: frame.branch_count,
        param_count: frame.param_count,
      });
    } else if (frameStack.length > 0 && isNestingNode(node)) {
      frameStack[frameStack.length - 1].depth--;
    }
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
