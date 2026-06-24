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
// One file's recorded call sites are capped so a generated megafile cannot
// bloat the dump; the true total is preserved for honest truncation.
const MAX_CALL_SITES = 2000;

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
// statement and push `{ name, local, module, line }` rows into `out`. The
// recorded `name` is the name as the SOURCE module exports it (the left side of
// `as`), which is what the reverse index keys on: who-imports-`editPrice` does
// not care what the importer locally calls it. `local` is the binding the
// importer's own code uses at call sites (the right side of `as`; identical to
// `name` when no alias is present), which is what the calls index matches
// identifiers against. Type-only imports (`import type { T }`
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
    const local = el.name && typeof el.name.text === "string" ? el.name.text : null;
    const exported = el.propertyName && typeof el.propertyName.text === "string"
      ? el.propertyName.text
      : local;
    if (!exported || exported === "default") continue;
    out.push({ name: exported, local: local ?? exported, module: moduleName, line });
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

// Classify a call/new expression's callee into the dump's call-site shape.
// Returns null for callees the index can never resolve deterministically:
// computed member access obj[k](), immediately-invoked expressions, and any
// PropertyAccess whose direct receiver is not an Identifier/this/super (a
// multi-hop chain like api.utils.helper() or new api.utils.Klass() is
// statically unresolvable — the callee is a property of a property, not a
// direct export of any named module — so the site is dropped, the same stance
// as computed access and operator sends).
function callSiteOf(node) {
  const isNew = node.kind === ts.SyntaxKind.NewExpression;
  const callee = node.expression;
  if (!callee) return null;
  if (callee.kind === ts.SyntaxKind.Identifier) {
    return { name: callee.text, receiver: null, kind: isNew ? "new" : "bare" };
  }
  if (callee.kind === ts.SyntaxKind.PropertyAccessExpression) {
    const name = callee.name && callee.name.text;
    if (!name) return null;
    const recv = callee.expression;
    if (recv.kind === ts.SyntaxKind.ThisKeyword) {
      return { name, receiver: "this", kind: "this" };
    }
    if (recv.kind === ts.SyntaxKind.SuperKeyword) {
      return { name, receiver: "super", kind: "super" };
    }
    // Only depth-1 chains (svc.sync(), new api.Klass()) carry a resolvable
    // receiver identifier. A deeper chain (api.utils.helper()) dispatches
    // through a property of the receiver; the receiver's export set proves
    // nothing about the callee, so the site is dropped rather than emitted
    // with receiver=null (which is byte-identical to a true receiver-less site
    // and lets the builder fabricate import edges).
    if (recv.kind !== ts.SyntaxKind.Identifier) return null;
    return { name, receiver: recv.text, kind: isNew ? "new" : "member" };
  }
  return null;
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
    const shape = { name, optional, kind };
    // Best-effort DECLARED type annotation text (definition-hydration only). This
    // is a pure-parse getText() of the annotation as written -- no type checker,
    // so an untyped param has none. JSON.stringify omits the undefined key.
    if (p.type) {
      const t = p.type.getText();
      if (t) shape.type = t;
    }
    return shape;
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
    namespace_imports: [],
    has_jsx: false,
    parse_diagnostics_count: diagnostics.length,
    function_scopes: [],
    callable_signatures: [],
    class_shapes: [],
    call_sites: [],
    call_sites_total: 0,
    call_sites_truncated: false,
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
      // Capture `import * as alias from 'module'` for namespace-call resolution.
      // Type-only namespace imports (`import type * as T from '...'`) have no
      // runtime value and must not seed call-edge resolution.
      const clause = stmt.importClause;
      if (
        clause &&
        !clause.isTypeOnly &&
        clause.namedBindings &&
        clause.namedBindings.kind === ts.SyntaxKind.NamespaceImport &&
        clause.namedBindings.name &&
        typeof clause.namedBindings.name.text === "string" &&
        moduleName
      ) {
        let line = null;
        try {
          line = sourceFile.getLineAndCharacterOfPosition(stmt.getStart(sourceFile)).line + 1;
        } catch (e) {
          line = null;
        }
        result.namespace_imports.push({
          alias: clause.namedBindings.name.text,
          module: moduleName,
          line,
        });
      }
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
  // Enclosing class names, innermost last. Method signatures read this to record
  // which class they belong to without a second parse.
  const classStack = [];
  // Base class (extends) parallel to classStack, innermost last; null for an
  // unextended or anonymous class. Methods read the top to record base_class.
  const classBaseStack = [];
  // Enclosing namespace/module names, innermost last. Joined with the named
  // classStack entries to form a method's qualified enclosing_class_path so a
  // short class name does not collide across namespaces (matches the Python /
  // Ruby dumps' qualified path).
  const namespaceStack = [];
  // Enclosing callable names, innermost last. Call sites read this to record
  // which function they were invoked from.
  const callerStack = [];

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

  // Trailing identifier of an expression used as a type/decorator/heritage
  // reference: `Injectable`, `core.Injectable`, `Base<T>` -> "Injectable"/"Base".
  function nameFromExpr(expr) {
    if (!expr) return null;
    if (expr.kind === ts.SyntaxKind.Identifier) return expr.text;
    if (expr.kind === ts.SyntaxKind.PropertyAccessExpression && expr.name) {
      return expr.name.text;
    }
    if (expr.expression) return nameFromExpr(expr.expression);
    return null;
  }

  function decoratorName(dec) {
    let expr = dec.expression;
    // `@Injectable()` is a CallExpression; `@Injectable` is the bare identifier.
    if (expr && expr.kind === ts.SyntaxKind.CallExpression) expr = expr.expression;
    return nameFromExpr(expr);
  }

  // Decorators across TS versions: getDecorators (4.8+), the modifiers array,
  // and the legacy node.decorators property. Return identifier names only.
  function decoratorsOf(node) {
    let decs = [];
    try {
      if (typeof ts.getDecorators === "function" && ts.canHaveDecorators?.(node)) {
        decs = ts.getDecorators(node) ?? [];
      }
    } catch {
      decs = [];
    }
    if ((!decs || decs.length === 0) && Array.isArray(node.modifiers)) {
      decs = node.modifiers.filter((m) => m.kind === ts.SyntaxKind.Decorator);
    }
    if ((!decs || decs.length === 0) && Array.isArray(node.decorators)) {
      decs = node.decorators;
    }
    return decs.map((d) => decoratorName(d)).filter(Boolean);
  }

  function heritageOf(node) {
    let ext = null;
    const impl = [];
    for (const clause of node.heritageClauses ?? []) {
      if (clause.token === ts.SyntaxKind.ExtendsKeyword) {
        const t = clause.types?.[0];
        if (t) ext = nameFromExpr(t.expression);
      } else if (clause.token === ts.SyntaxKind.ImplementsKeyword) {
        for (const t of clause.types ?? []) {
          const n = nameFromExpr(t.expression);
          if (n) impl.push(n);
        }
      }
    }
    return { ext, impl };
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

    // Track enclosing class so method signatures can record which class they
    // belong to. ClassExpression covers `const C = class Foo {}` patterns.
    // Unnamed ClassDeclaration/ClassExpression and ObjectLiteralExpression push
    // a null sentinel so methods defined inside them do not inherit the
    // lexically-enclosing named class -- they have no named class of their own.
    const isNamedClass =
      (node.kind === ts.SyntaxKind.ClassDeclaration ||
        node.kind === ts.SyntaxKind.ClassExpression ||
        node.kind === ts.SyntaxKind.InterfaceDeclaration) &&
      node.name &&
      typeof node.name.text === "string";
    const isClassSentinel =
      !isNamedClass &&
      (node.kind === ts.SyntaxKind.ClassDeclaration ||
        node.kind === ts.SyntaxKind.ClassExpression ||
        node.kind === ts.SyntaxKind.ObjectLiteralExpression);
    const isClass = isNamedClass || isClassSentinel;
    // Namespace/module nesting contributes to the qualified path but is not a
    // class frame. A string-literal module name (`declare module "x"`) has no
    // useful path segment, so push null and filter it out when joining.
    const isNamespace = node.kind === ts.SyntaxKind.ModuleDeclaration;
    if (isNamespace) {
      namespaceStack.push(node.name && typeof node.name.text === "string" ? node.name.text : null);
    }
    if (isNamedClass) {
      classStack.push(node.name.text);
      // Base only for real classes (interfaces carry no runtime base here).
      classBaseStack.push(
        node.kind === ts.SyntaxKind.ClassDeclaration ||
          node.kind === ts.SyntaxKind.ClassExpression
          ? heritageOf(node).ext
          : null,
      );
    } else if (isClassSentinel) {
      classStack.push(null);
      classBaseStack.push(null);
    }

    // The class's decorator + heritage shape: the contract a decorator/base
    // implies (NestJS @Injectable, TypeORM @Entity, a shared base) that the
    // signature index never records. Interfaces carry no runtime contract here.
    if (
      isNamedClass &&
      (node.kind === ts.SyntaxKind.ClassDeclaration ||
        node.kind === ts.SyntaxKind.ClassExpression) &&
      result.class_shapes.length < MAX_CALLABLE_SIGNATURES
    ) {
      const { ext, impl } = heritageOf(node);
      result.class_shapes.push({
        name: node.name.text,
        decorators: decoratorsOf(node),
        extends: ext,
        implements: impl,
      });
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
      callerStack.push(callableName ?? "<anonymous>");
      if (callableName !== null && result.callable_signatures.length < MAX_CALLABLE_SIGNATURES) {
        // Methods, constructors, and accessors belong to the enclosing class;
        // plain functions do not and carry null so callers can distinguish them.
        // callableKindOf returns "function" for FunctionDeclaration/Expression/Arrow
        // and a distinct kind for every class-member shape, so != "function" covers all.
        const isMethod = callableKindOf(node) !== "function";
        const innermostClass = classStack.length ? classStack[classStack.length - 1] : null;
        let enclosingClassPath; // omitted unless the method has a NAMED class
        let baseClass; // omitted unless the named class extends something
        let methodDecorators;
        if (isMethod && innermostClass) {
          // Qualified path: namespace segments then every named class frame
          // (anonymous/object-literal sentinels are null and dropped).
          const parts = [...namespaceStack, ...classStack].filter(Boolean);
          enclosingClassPath = parts.join(".");
          const innerBase = classBaseStack[classBaseStack.length - 1];
          if (innerBase) baseClass = innerBase;
        }
        if (isMethod) {
          const decs = decoratorsOf(node);
          if (decs.length > 0) methodDecorators = decs;
        }
        result.callable_signatures.push({
          name: callableName,
          kind: callableKindOf(node),
          params: paramShapesOf(node),
          // Best-effort DECLARED return-type annotation text (definition
          // hydration). Pure-parse getText(); JSON.stringify omits it when the
          // function has no return annotation (an inferred return is invisible).
          return_type: node.type ? node.type.getText() : undefined,
          is_default_export: isDefaultExportNode(node),
          // Body span for the duplication catalog's body-hash fallback: a
          // body-exact clone whose name shares no tokens with the original
          // can only be paired by body identity.
          start_line: start,
          end_line: end,
          enclosing_class: isMethod ? innermostClass ?? null : null,
          // Qualified class path + base + per-method decorators bring TS to
          // parity with the Python/Ruby dumps. JSON.stringify omits the
          // undefined ones (plain functions, unextended classes, no decorators).
          enclosing_class_path: enclosingClassPath,
          base_class: baseClass,
          decorators: methodDecorators,
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

    if (
      node.kind === ts.SyntaxKind.CallExpression ||
      node.kind === ts.SyntaxKind.NewExpression
    ) {
      const site = callSiteOf(node);
      if (site !== null) {
        result.call_sites_total++;
        if (result.call_sites.length < MAX_CALL_SITES) {
          result.call_sites.push({
            ...site,
            line: startLineOf(node),
            caller: callerStack.length > 0 ? callerStack[callerStack.length - 1] : "<module>",
          });
        } else {
          result.call_sites_truncated = true;
        }
      }
    }

    ts.forEachChild(node, visit);

    if (isFn) {
      callerStack.pop();
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

    if (isClass) {
      classStack.pop();
      classBaseStack.pop();
    }
    if (isNamespace) {
      namespaceStack.pop();
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
  // Drain stdout before exiting: writes past the kernel pipe buffer sit in the
  // process-side write queue, and process.exit() discards that queue. The empty
  // write's callback fires only after the queue drains, so large records (e.g.
  // files with thousands of call sites) are not silently truncated.
  process.stdout.write("", () => {
    process.exit(0);
  });
});
