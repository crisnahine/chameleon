//! The single tree walk that fills every extracted field.
//!
//! One walk, explicitly stacked, never recursive. That is a safety property
//! rather than a style choice: tree-sitter happily parses 50,000 nested
//! parentheses into a 50,003-deep tree, and a recursive walk over it overflows
//! the stack. The dumper processes chameleon replaced could recurse safely
//! because a blown stack only killed a subprocess; an engine meant to be
//! embedded cannot make that trade.
//!
//! Everything language-specific arrives as data on the `LanguageSpec`. Nothing
//! here knows what Python or Go is, which is what keeps a new language a TOML
//! file rather than a new module.

use std::collections::BTreeSet;

use tree_sitter::{Node, Parser, Tree};

use crate::core::*;
use crate::lang::{BoundLanguage, LanguageSpec};

/// A function frame, live while the walk is inside that function's subtree.
struct FuncFrame {
    name: String,
    start_line: usize,
    end_line: usize,
    param_count: usize,
    branch_count: usize,
    /// Nesting depth right now, and the high-water mark to report.
    cur_depth: usize,
    max_depth: usize,
    /// Levels opened by chained clauses, keyed by the statement that will
    /// release them. See `chained_nesting_nodes`.
    chained_held: std::collections::HashMap<usize, usize>,
    /// The node this frame belongs to, so the matching leave event pops it.
    node_id: usize,
}

/// A class frame, live while the walk is inside a class body.
struct ClassFrame {
    name: String,
    base: Option<String>,
    node_id: usize,
}

/// Parse one file's bytes into a `ParsedFile`, or explain why not.
///
/// `path` is echoed into the record verbatim; the caller owns path policy
/// (containment, symlinks) because the engine may be driven over a stream of
/// paths it did not choose.
pub fn extract(
    path: &str,
    source: &[u8],
    bound: &BoundLanguage,
    limits: &Limits,
) -> Result<ParsedFile, ParseError> {
    if source.len() as u64 > limits.max_file_bytes {
        return Err(ParseError::new(path, "file_too_large").with_size(source.len() as u64));
    }

    let mut parser = Parser::new();
    parser
        .set_language(&bound.language)
        .map_err(|e| ParseError::new(path, "walk_error").with_message(e.to_string()))?;

    let tree: Tree = parser.parse(source, None).ok_or_else(|| {
        ParseError::new(path, "parse_error").with_message("parser returned no tree")
    })?;

    // Node budget before the walk, not during: counting descendants is cheap and
    // refusing early means a pathological file costs one traversal, not two.
    let node_count = tree.root_node().descendant_count();
    if node_count > limits.max_ast_nodes {
        return Err(ParseError::new(path, "ast_node_ceiling_exceeded"));
    }

    Ok(walk(path, source, &tree, &bound.spec, limits))
}

/// Text of a node, lossily decoded. Lossy rather than fatal: one stray byte in
/// a comment must not cost the whole file's record.
fn text<'a>(node: Node, source: &'a [u8]) -> &'a str {
    node.utf8_text(source).unwrap_or("")
}

fn line_of(node: Node) -> usize {
    node.start_position().row + 1
}

/// A module specifier's text, without the string delimiters.
///
/// In most grammars the specifier IS a string literal, and its node text spans
/// the quotes. Left in, `"react"` never matches a corpus path and the whole
/// import graph for that language silently resolves to nothing.
fn module_text(node: Node, source: &[u8]) -> String {
    text(node, source)
        .trim()
        .trim_matches(|c| c == '"' || c == '\'' || c == '`' || c == '<' || c == '>')
        .to_string()
}

/// Named child on `field`, when the spec named a field at all.
fn field_node<'t>(node: Node<'t>, field: &Option<String>) -> Option<Node<'t>> {
    node.child_by_field_name(field.as_deref()?)
}

fn field_text(node: Node, field: &Option<String>, source: &[u8]) -> Option<String> {
    field_node(node, field).map(|n| text(n, source).to_string())
}

/// Render a base list the way the dumpers do: the first base, with a count of
/// the rest, so a consumer reading one string is never told a class has a single
/// parent when it has four.
fn extends_display(bases: &[String]) -> Option<String> {
    match bases.len() {
        0 => None,
        1 => Some(bases[0].clone()),
        n => Some(format!("{} (+{} more)", bases[0], n - 1)),
    }
}

/// Decorators attached to a declaration.
///
/// A decorated declaration is usually wrapped by the grammar (Python's
/// `decorated_definition`), so the decorators are siblings under the parent
/// rather than children of the declaration itself; both layouts are checked.
fn decorators_of(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    if spec.flags.decorator_nodes.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut collect = |parent: Node| {
        let mut c = parent.walk();
        for child in parent.children(&mut c) {
            if spec.flags.decorator_nodes.iter().any(|k| k == child.kind()) {
                let raw = text(child, source).trim_start_matches('@').trim();
                // `@app.route("/x")` records as `app.route`: the call arguments
                // are data, and keying on them would make every route a
                // distinct decorator.
                let name = raw.split(['(', '\n']).next().unwrap_or(raw).trim();
                if !name.is_empty() {
                    out.push(name.to_string());
                }
            }
        }
    };
    if let Some(parent) = node.parent() {
        collect(parent);
    }
    collect(node);
    out.dedup();
    out
}

/// Classify one parameter node against the spec's kind sets.
fn param_of(node: Node, spec: &LanguageSpec, source: &[u8], keyword_only: bool) -> Option<Param> {
    let p = &spec.params;
    // See through a type-carrying wrapper to the pattern underneath, but keep
    // reading the annotation off the wrapper.
    let inner = if p.wrapper.iter().any(|k| k == node.kind()) {
        node.named_child(0).unwrap_or(node)
    } else {
        node
    };
    let kind_str = inner.kind();

    let (mut kind, optional) = if p.rest.iter().any(|k| k == kind_str) {
        // `*args` can always be omitted, so it is optional in the same sense a
        // defaulted parameter is.
        ("rest", true)
    } else if p.keyword_rest.iter().any(|k| k == kind_str) {
        ("keyword_rest", true)
    } else if p.optional.iter().any(|k| k == kind_str) {
        // A defaulted parameter is its own kind, not a positional that happens
        // to be optional: callers distinguish "may be omitted" from "may be
        // passed by position".
        ("optional", true)
    } else if p.destructured.iter().any(|k| k == kind_str) {
        ("destructured", false)
    } else if p.positional.iter().any(|k| k == kind_str) {
        ("positional", false)
    } else {
        return None;
    };

    // A parameter after the `*` separator is keyword-only regardless of which
    // node kind it is; rest/keyword_rest keep their own identity.
    if keyword_only && matches!(kind, "positional" | "optional") {
        kind = "keyword";
    }

    let raw = match field_text(inner, &spec.fields.name, source) {
        Some(n) => n,
        None => {
            // Destructuring patterns have no name; the dumpers record a
            // placeholder so the positional slot is still counted.
            if kind == "destructured" {
                "{}".to_string()
            } else {
                text(inner, source)
                    .split([':', '='])
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_string()
            }
        }
    };
    // `*args` / `**kwargs` bind `args` and `kwargs`; the sigil is carried by
    // `kind`, so repeating it in the name would make the binding unmatchable
    // against a call site.
    let name = raw.trim_start_matches('*').trim().to_string();
    if name.is_empty() {
        return None;
    }

    Some(Param {
        name,
        optional,
        kind: kind.to_string(),
        type_annotation: field_text(node, &spec.fields.param_type, source),
    })
}

/// Every parameter of a callable, in source order.
fn params_of(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<Param> {
    let Some(list) = field_node(node, &spec.fields.parameters) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut keyword_only = false;
    let mut cursor = list.walk();
    for child in list.named_children(&mut cursor) {
        if spec.params.separator.iter().any(|k| k == child.kind()) {
            keyword_only = true;
            continue;
        }
        if let Some(p) = param_of(child, spec, source, keyword_only) {
            // A rest parameter also opens the keyword-only region, and it may
            // arrive wrapped, so this reads the classified kind rather than the
            // node kind.
            let opens_keyword_region = p.kind == "rest";
            out.push(p);
            if opens_keyword_region {
                keyword_only = true;
            }
        }
    }
    out
}

/// The receiver text, when the receiver is a single name rather than a chain.
///
/// Only a simple identifier receiver is ever recorded: `a.b.c()` must yield no
/// edge rather than a guessed one. A leaf node is the common case, but some
/// grammars wrap a plain variable in one more node (PHP's `$obj` is a
/// `variable_name` around a `name`), so a single leaf child whose text carries
/// no access operator counts as simple too. Anything else is a chain.
fn simple_receiver<'a>(node: Node, source: &'a [u8]) -> Option<&'a str> {
    let t = text(node, source);
    match node.named_child_count() {
        0 => Some(t),
        1 => {
            let only = node.named_child(0)?;
            let unwrapped = only.named_child_count() == 0
                && !t.contains('.')
                && !t.contains("->")
                && !t.contains("::")
                && !t.contains('(')
                && !t.contains('[');
            unwrapped.then_some(t)
        }
        _ => None,
    }
}

/// Split a call node into (name, receiver, kind), or `None` to record nothing.
///
/// Returning `None` is the common and correct outcome for anything the engine
/// cannot resolve deterministically -- a multi-hop chain like `a.b.c()` records
/// no edge rather than a guessed one, matching chameleon's precision-first
/// posture. A wrong edge is worse than a missing one: it makes a blast radius
/// lie.
fn call_of(
    node: Node,
    spec: &LanguageSpec,
    source: &[u8],
) -> Option<(String, Option<String>, String)> {
    if spec
        .calls
        .constructor_nodes
        .iter()
        .any(|k| k == node.kind())
    {
        let callee =
            field_node(node, &spec.fields.call_function).or_else(|| node.named_child(0))?;
        return Some((text(callee, source).to_string(), None, "new".to_string()));
    }

    // Grammars that carry the receiver and name on the call node itself.
    if let Some(name_field) = &spec.calls.name_field {
        if let Some(name_node) = node.child_by_field_name(name_field.as_str()) {
            let name = text(name_node, source).to_string();
            if name.is_empty() {
                return None;
            }
            let recv = spec
                .calls
                .receiver_field
                .as_deref()
                .and_then(|f| node.child_by_field_name(f));
            return Some(match recv {
                Some(r) => {
                    let rt = simple_receiver(r, source)?.to_string();
                    if spec.calls.self_names.iter().any(|s| s == &rt) {
                        (name, Some(rt), "self".to_string())
                    } else {
                        (name, Some(rt), "member".to_string())
                    }
                }
                None => (name, None, "bare".to_string()),
            });
        }
    }

    let callee = field_node(node, &spec.fields.call_function).or_else(|| node.named_child(0))?;

    if spec.calls.member_nodes.iter().any(|k| k == callee.kind()) {
        let obj = field_node(callee, &spec.fields.member_object)?;
        let prop = field_node(callee, &spec.fields.member_property)?;
        let name = text(prop, source).to_string();
        let recv_text = text(obj, source).to_string();

        // `svc.api.deep.sync()` has a member expression as its receiver and is
        // dropped rather than guessed at.
        let is_self = spec.calls.self_names.iter().any(|s| s == &recv_text);
        if !is_self && simple_receiver(obj, source).is_none() {
            return None;
        }
        if is_self {
            return Some((name, Some(recv_text), "self".to_string()));
        }
        if spec.calls.super_nodes.iter().any(|k| k == obj.kind()) {
            return Some((name, Some(recv_text), "super".to_string()));
        }
        return Some((name, Some(recv_text), "member".to_string()));
    }

    // A bare call: the callee must be a plain identifier to be recordable.
    if callee.named_child_count() == 0 {
        let name = text(callee, source).to_string();
        if name.is_empty() {
            return None;
        }
        return Some((name, None, "bare".to_string()));
    }
    None
}

/// The main walk. Enter/leave events over one explicit cursor.
fn walk(
    path: &str,
    source: &[u8],
    tree: &Tree,
    spec: &LanguageSpec,
    limits: &Limits,
) -> ParsedFile {
    let root = tree.root_node();

    let mut top_level_node_kinds = Vec::new();
    let mut function_scopes: Vec<FunctionScope> = Vec::new();
    let mut callable_signatures: Vec<CallableSignature> = Vec::new();
    let mut class_shapes: Vec<ClassShape> = Vec::new();
    let mut call_sites: Vec<CallSite> = Vec::new();
    let mut import_specifiers: Vec<(String, String)> = Vec::new();
    let mut import_symbols: Vec<ImportSymbol> = Vec::new();
    let mut namespace_imports: Vec<NamespaceImport> = Vec::new();
    let mut export_names: BTreeSet<String> = BTreeSet::new();
    let mut export_set_open = false;
    let mut has_jsx = false;
    let mut named_export_count = 0usize;
    let mut call_sites_total = 0usize;
    let mut top_level_funcs = 0usize;
    let mut top_level_classes = 0usize;
    let mut top_level_func_kind: Option<String> = None;
    let mut top_level_class_kind: Option<String> = None;

    let mut func_stack: Vec<FuncFrame> = Vec::new();
    let mut class_stack: Vec<ClassFrame> = Vec::new();

    // Top-level kinds first: they are the root's own children, so reading them
    // in a separate pass is cheaper and clearer than threading depth through the
    // main walk.
    {
        let mut c = root.walk();
        for child in root.named_children(&mut c) {
            let mut node = child;
            // Unwrap grammar wrappers so the recorded kind is the meaningful
            // inner statement. The wrapped declaration is the wrapper's LAST
            // named child; the earlier ones are its decorators (Python) or the
            // `export` keyword (TypeScript).
            //
            // Whether the wrapper was an export marker also decides membership
            // of the module's public surface. Without that, a TS file reports
            // its PRIVATE declarations as exports and omits the ones it
            // actually exposes -- the set is not noisy, it is inverted.
            let was_wrapped = spec.flags.unwrap_nodes.iter().any(|k| k == node.kind());
            if was_wrapped {
                let last = (node.named_child_count() as u32).saturating_sub(1);
                if let Some(inner) = node.named_child(last) {
                    node = inner;
                }
            }
            let is_exported = !spec.flags.explicit_exports || was_wrapped;
            let Some(mapped) = spec.top_level_kind(node.kind()) else {
                continue;
            };
            if mapped.is_empty() {
                continue;
            }
            // Refine a wrapper whose meaning depends on its content: first by
            // the inner node's kind, then by which fields that inner node
            // carries (an assignment with a `type` is an annotated assignment).
            let inner = node.named_child(0);
            let by_kind = spec
                .top_level_refine
                .get(node.kind())
                .and_then(|table| inner.and_then(|i| table.get(i.kind()).cloned()));
            let by_field = inner.and_then(|i| {
                spec.top_level_refine_field.get(i.kind()).and_then(|table| {
                    table
                        .iter()
                        .find(|(field, _)| i.child_by_field_name(field.as_str()).is_some())
                        .map(|(_, emitted)| emitted.clone())
                })
            });
            let refined_kind = by_field.or(by_kind).unwrap_or_else(|| mapped.to_string());
            top_level_node_kinds.push(refined_kind.clone());

            if (spec.is_function(node.kind()) || spec.is_class(node.kind())) && is_exported {
                named_export_count += 1;
                if spec.is_class(node.kind()) {
                    top_level_classes += 1;
                    top_level_class_kind = Some(refined_kind.clone());
                } else {
                    top_level_funcs += 1;
                    top_level_func_kind = Some(refined_kind.clone());
                }
                if is_exported {
                    if let Some(name) = field_text(node, &spec.fields.name, source) {
                        export_names.insert(name);
                    }
                }
            }
        }
    }

    let mut cursor = tree.walk();
    let mut budget = limits.max_ast_nodes;

    'walk: loop {
        let node = cursor.node();
        budget = budget.saturating_sub(1);
        if budget == 0 {
            break;
        }
        let kind = node.kind();

        // A decorated declaration opens its frame at the WRAPPER node, not at
        // the inner `def`. In a recursive AST the decorator is a child of the
        // function it decorates, so a call inside `@wraps(fn)` is attributed to
        // that function; here the decorator is an earlier sibling, so opening
        // the frame at the wrapper is what reproduces the attribution.
        let wrapped = if spec.flags.unwrap_nodes.iter().any(|k| k == kind) {
            let last = (node.named_child_count() as u32).saturating_sub(1);
            node.named_child(last)
        } else {
            None
        };
        let decl = wrapped.unwrap_or(node);
        // ...and the inner declaration must then not open a second frame.
        //
        // Gated on the node actually being a declaration: `Node::parent()` walks
        // down from the root in tree-sitter, so calling it once per node is
        // O(nodes x depth) and made a 20k-deep file take 20 seconds. Declarations
        // are a tiny fraction of nodes, so asking only for them is free.
        let opened_by_wrapper = (spec.is_function(kind) || spec.is_class(kind))
            && !spec.flags.unwrap_nodes.is_empty()
            && node.parent().is_some_and(|p| {
                spec.flags.unwrap_nodes.iter().any(|k| k == p.kind())
                    && p.named_child((p.named_child_count() as u32).saturating_sub(1)) == Some(node)
            });

        let skip = spec.skip_subtree_nodes.iter().any(|k| k == kind);

        if !skip {
            // ---- enter ----
            if spec.flags.supports_jsx && kind.starts_with("jsx_") {
                has_jsx = true;
            }

            if spec.is_class(decl.kind()) && !opened_by_wrapper {
                let name = field_text(decl, &spec.fields.name, source).unwrap_or_default();
                let bases = base_list(decl, spec, source);
                if !name.is_empty() {
                    class_shapes.push(ClassShape {
                        name: name.clone(),
                        start_line: line_of(decl),
                        extends: extends_display(&bases),
                        bases: bases.clone(),
                        decorators: decorators_of(decl, spec, source),
                        class_attrs: class_attrs_of(decl, spec, source),
                    });
                }
                class_stack.push(ClassFrame {
                    name,
                    base: bases.first().cloned(),
                    node_id: node.id(),
                });
            }

            if spec.is_function(decl.kind()) && !opened_by_wrapper {
                let name = field_text(decl, &spec.fields.name, source)
                    .unwrap_or_else(|| "<anonymous>".to_string());
                let params = params_of(decl, spec, source);
                let decorators = decorators_of(decl, spec, source);
                let enclosing = class_stack.last();

                if callable_signatures.len() < limits.max_callable_signatures {
                    callable_signatures.push(CallableSignature {
                        kind: callable_kind(&decorators, enclosing.is_some()),
                        name: name.clone(),
                        params: params.clone(),
                        is_default_export: false,
                        is_async: is_async(decl, spec, source),
                        enclosing_class: enclosing.map(|c| c.name.clone()),
                        // Nesting-qualified: `Outer.Inner` for a nested class,
                        // the bare name for a top-level one.
                        enclosing_class_path: enclosing.map(|_| {
                            class_stack
                                .iter()
                                .map(|f| f.name.as_str())
                                .collect::<Vec<_>>()
                                .join(".")
                        }),
                        base_class: enclosing.and_then(|c| c.base.clone()),
                        decorators,
                        start_line: line_of(decl),
                        end_line: decl.end_position().row + 1,
                        return_type: field_text(decl, &spec.fields.return_type, source),
                    });
                }

                func_stack.push(FuncFrame {
                    name,
                    start_line: line_of(decl),
                    end_line: decl.end_position().row + 1,
                    param_count: params.len(),
                    branch_count: 0,
                    cur_depth: 0,
                    max_depth: 0,
                    chained_held: std::collections::HashMap::new(),
                    node_id: node.id(),
                });
            }

            if let Some(frame) = func_stack.last_mut() {
                if spec.branch_nodes.contains(kind) {
                    frame.branch_count += 1;
                }
                if spec.nesting_nodes.contains(kind) {
                    frame.cur_depth += 1;
                    frame.max_depth = frame.max_depth.max(frame.cur_depth);
                }
                // A chained clause deepens the frame too, but its level is held
                // until the enclosing statement closes rather than released at
                // its own leave -- that is what makes `if/elif/elif` measure 3
                // deep here as it would in a recursive tree.
                if spec.chained_nesting_nodes.contains(kind) {
                    frame.cur_depth += 1;
                    frame.max_depth = frame.max_depth.max(frame.cur_depth);
                    if let Some(parent) = node.parent() {
                        *frame.chained_held.entry(parent.id()).or_insert(0) += 1;
                    }
                }
            }

            if spec.calls.nodes.iter().any(|k| k == kind) {
                // Counted only when the call actually resolves to a recordable
                // row. A dropped multi-hop chain is not a truncated site, and
                // counting it as one would make `call_sites_truncated` lie about
                // a file that was fully recorded.
                if let Some((name, receiver, ckind)) = call_of(node, spec, source) {
                    call_sites_total += 1;
                    if call_sites.len() < limits.max_call_sites {
                        call_sites.push(CallSite {
                            name,
                            receiver,
                            kind: ckind,
                            line: line_of(node),
                            caller: func_stack
                                .last()
                                .map(|f| f.name.clone())
                                .unwrap_or_else(|| "<module>".to_string()),
                            nesting: None,
                        });
                    }
                }
            }

            collect_imports(
                node,
                spec,
                source,
                &mut import_specifiers,
                &mut import_symbols,
                &mut namespace_imports,
                &mut export_names,
                &mut export_set_open,
            );

            if cursor.goto_first_child() {
                continue 'walk;
            }
        }

        // ---- leave (this node had no children, or was skipped) ----
        leave(
            node,
            &mut func_stack,
            &mut class_stack,
            &mut function_scopes,
            spec,
        );

        loop {
            if cursor.goto_next_sibling() {
                continue 'walk;
            }
            if !cursor.goto_parent() {
                break 'walk;
            }
            leave(
                cursor.node(),
                &mut func_stack,
                &mut class_stack,
                &mut function_scopes,
                spec,
            );
        }
    }

    // Frames still open when the walk ended (a truncated budget) are still real
    // functions; reporting them beats silently losing their shape.
    while let Some(frame) = func_stack.pop() {
        function_scopes.push(scope_of(&frame));
    }
    // Deliberately NOT sorted. Frames are appended as they are popped, so a
    // nested closure lands before its enclosing function -- which is the order
    // the reference dumpers emit, and the order downstream consumers compare
    // against.

    let content_first_200_bytes =
        String::from_utf8_lossy(&source[..source.len().min(200)]).to_string();

    // Unopposed sole definition; a module holding both a class and a function
    // reports nothing, since neither is what the file is for.
    let default_export_kind = if spec.flags.sole_definition_default_export {
        match (top_level_classes, top_level_funcs) {
            (1, 0) => top_level_class_kind,
            (0, 1) => top_level_func_kind,
            _ => None,
        }
    } else {
        None
    };

    ParsedFile {
        path: path.to_string(),
        content_first_200_bytes,
        top_level_node_kinds,
        default_export_kind,
        named_export_count,
        named_export_names: export_names.into_iter().collect(),
        export_set_open,
        import_specifiers,
        import_symbols,
        namespace_imports,
        has_jsx,
        parse_diagnostics_count: usize::from(root.has_error()),
        function_scopes,
        callable_signatures,
        class_shapes,
        call_sites_truncated: call_sites_total > call_sites.len(),
        call_sites_total,
        call_sites,
    }
}

fn scope_of(frame: &FuncFrame) -> FunctionScope {
    FunctionScope {
        start_line: frame.start_line,
        end_line: frame.end_line,
        line_span: Some(frame.end_line.saturating_sub(frame.start_line) + 1),
        max_depth: frame.max_depth,
        branch_count: frame.branch_count,
        param_count: frame.param_count,
    }
}

/// Pop whatever frames this node owned, and undo its nesting contribution.
fn leave(
    node: Node,
    func_stack: &mut Vec<FuncFrame>,
    class_stack: &mut Vec<ClassFrame>,
    scopes: &mut Vec<FunctionScope>,
    spec: &LanguageSpec,
) {
    if let Some(frame) = func_stack.last_mut() {
        let mut release = usize::from(spec.nesting_nodes.contains(node.kind()));
        release += frame.chained_held.remove(&node.id()).unwrap_or(0);
        frame.cur_depth = frame.cur_depth.saturating_sub(release);
    }
    if func_stack.last().is_some_and(|f| f.node_id == node.id()) {
        let frame = func_stack.pop().expect("checked above");
        scopes.push(scope_of(&frame));
    }
    if class_stack.last().is_some_and(|c| c.node_id == node.id()) {
        class_stack.pop();
    }
}

/// A method decorated `@staticmethod` / `@classmethod` reports that kind, since
/// the class-contract derivation treats the three differently.
fn callable_kind(decorators: &[String], in_class: bool) -> String {
    for d in decorators {
        match d.as_str() {
            "staticmethod" => return "staticmethod".into(),
            "classmethod" => return "classmethod".into(),
            _ => {}
        }
    }
    if in_class {
        "method".into()
    } else {
        "function".into()
    }
}

fn is_async(node: Node, spec: &LanguageSpec, source: &[u8]) -> bool {
    if spec.flags.async_markers.is_empty() {
        return false;
    }
    let mut c = node.walk();
    let found = node.children(&mut c).any(|child| {
        spec.flags
            .async_markers
            .iter()
            .any(|m| m == text(child, source))
    });
    found
}

/// Base classes of a class node, in source order.
fn base_list(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    let Some(sup) = field_node(node, &spec.fields.superclass) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut c = sup.walk();
    let named: Vec<Node> = sup.named_children(&mut c).collect();
    if named.is_empty() {
        let t = text(sup, source).trim_start_matches('<').trim().to_string();
        if !t.is_empty() {
            out.push(t);
        }
    } else {
        for child in named {
            // Keyword arguments in a Python base list (`metaclass=X`) are not bases.
            if child.kind().contains("keyword") {
                continue;
            }
            let t = text(child, source).trim().to_string();
            if !t.is_empty() {
                out.push(t);
            }
        }
    }
    out
}

/// Names (never values) of direct class-body assignments.
///
/// Values are deliberately not captured: `permission_classes` being present is a
/// role signal, what it is set to is application data.
fn class_attrs_of(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    const MAX_CLASS_ATTRS: usize = 50;
    let Some(body) = field_node(node, &spec.fields.body) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut c = body.walk();
    for stmt in body.named_children(&mut c) {
        if out.len() >= MAX_CLASS_ATTRS {
            break;
        }
        let mut inner = stmt;
        if inner.kind() == "expression_statement" {
            if let Some(first) = inner.named_child(0) {
                inner = first;
            }
        }
        if inner.kind() == "assignment" || inner.kind() == "field_declaration" {
            if let Some(left) = inner
                .child_by_field_name("left")
                .or_else(|| inner.named_child(0))
            {
                if left.named_child_count() == 0 {
                    let name = text(left, source).trim().to_string();
                    if !name.is_empty() {
                        out.push(name);
                    }
                }
            }
        }
    }
    out
}

/// Read one node's import contribution, if it is an import at all.
#[allow(clippy::too_many_arguments)]
fn collect_imports(
    node: Node,
    spec: &LanguageSpec,
    source: &[u8],
    specifiers: &mut Vec<(String, String)>,
    symbols: &mut Vec<ImportSymbol>,
    namespaces: &mut Vec<NamespaceImport>,
    export_names: &mut BTreeSet<String>,
    export_set_open: &mut bool,
) {
    let kind = node.kind();
    let line = line_of(node);

    if spec.imports.wildcard_nodes.iter().any(|k| k == kind) {
        *export_set_open = true;
    }

    // Named form: `from m import a`, `import { a } from "m"`.
    if spec.imports.from_nodes.iter().any(|k| k == kind) {
        let module_node = field_node(node, &spec.imports.module_field);
        // The implicit map first: a node kind whose module is fixed by the
        // syntax has no field to read, and falling back to the `name` field
        // would record the imported symbol as the module -- which is how
        // `from __future__ import annotations` became module "annotations".
        let module = spec
            .imports
            .implicit_module
            .get(kind)
            .cloned()
            .or_else(|| module_node.map(|m| module_text(m, source)))
            .unwrap_or_default();
        if module.is_empty() {
            return;
        }
        let mut any_named = false;

        // Collect the real specifier nodes, descending through the wrappers a
        // grammar puts between the statement and its bindings.
        let mut children: Vec<Node> = Vec::new();
        let mut queue: Vec<Node> = {
            let mut c = node.walk();
            node.named_children(&mut c).collect()
        };
        while let Some(n) = queue.pop() {
            if spec.imports.descend_nodes.iter().any(|k| k == n.kind()) {
                let mut c = n.walk();
                queue.extend(n.named_children(&mut c));
            } else {
                children.push(n);
            }
        }
        children.sort_by_key(|n| n.start_byte());

        for child in children {
            // The module path itself is not an imported name, and neither is a
            // trailing `# noqa` comment, which the grammar files as a named
            // child of the import statement.
            if Some(child) == module_node || spec.skip_subtree_nodes.contains(child.kind()) {
                continue;
            }
            if spec
                .imports
                .wildcard_nodes
                .iter()
                .any(|k| k == child.kind())
            {
                *export_set_open = true;
                specifiers.push((module.clone(), "namespace".into()));
                return;
            }
            // `import * as ns from "m"` binds a whole module under one name, so
            // it belongs in namespace_imports. Left in the named list it shows
            // up as a symbol literally called `* as ns`, which matches nothing.
            if child.kind() == "namespace_import" {
                let alias = child
                    .named_child(0)
                    .map(|n| text(n, source).to_string())
                    .unwrap_or_default();
                if !alias.is_empty() {
                    if !spec.flags.explicit_exports {
                        export_names.insert(alias.clone());
                    }
                    namespaces.push(NamespaceImport {
                        alias,
                        module: module.clone(),
                        line,
                    });
                    specifiers.push((module.clone(), "namespace".into()));
                    continue;
                }
            }
            let (name, local) = if spec.imports.alias_nodes.iter().any(|k| k == child.kind()) {
                let n = child
                    .named_child(0)
                    .map(|x| text(x, source).to_string())
                    .unwrap_or_default();
                let a = child
                    .named_child(1)
                    .map(|x| text(x, source).to_string())
                    .unwrap_or_else(|| n.clone());
                (n, a)
            } else {
                let n = text(child, source).to_string();
                (n.clone(), n)
            };
            if name.is_empty() {
                continue;
            }
            any_named = true;
            if !spec.flags.explicit_exports {
                // In Python an imported name really is a module attribute; in
                // TypeScript it is not exported unless re-exported explicitly.
                export_names.insert(local.clone());
            }
            symbols.push(ImportSymbol {
                name,
                local,
                module: module.clone(),
                line,
            });
        }
        if any_named {
            specifiers.push((module, "named".into()));
        }
        return;
    }

    // Whole-module form: `import x`, `import x as y`.
    if spec.imports.module_nodes.iter().any(|k| k == kind) {
        // When a language spells imports as ordinary calls, only the named ones
        // count. Without this gate every call is an import: `puts "x"` records
        // namespace imports of `puts` AND of `"x"`, and both poison the file's
        // export set.
        if !spec.imports.module_call_names.is_empty() {
            let callee =
                field_node(node, &spec.fields.call_function).or_else(|| node.named_child(0));
            let called = callee
                .map(|c| text(c, source).to_string())
                .unwrap_or_default();
            if !spec.imports.module_call_names.iter().any(|n| n == &called) {
                return;
            }
        }
        let children: Vec<Node> = {
            let mut c = node.walk();
            node.named_children(&mut c).collect()
        };
        for child in children {
            if spec.skip_subtree_nodes.contains(child.kind()) {
                continue;
            }
            // The callee of a require IS the require, not a module.
            if !spec.imports.module_call_names.is_empty()
                && Some(child) == field_node(node, &spec.fields.call_function)
            {
                continue;
            }
            let (module, alias) = if spec.imports.alias_nodes.iter().any(|k| k == child.kind()) {
                let m = child
                    .named_child(0)
                    .map(|x| module_text(x, source))
                    .unwrap_or_default();
                let a = child
                    .named_child(1)
                    .map(|x| text(x, source).to_string())
                    .unwrap_or_else(|| m.clone());
                (m, a)
            } else {
                let m = module_text(child, source);
                // `import a.b.c` binds the FIRST segment: `a`, not `c`. Taking
                // the last one silently rebinds every dotted import.
                let a = m.split('.').next().unwrap_or(&m).to_string();
                (m, a)
            };
            if module.is_empty() {
                continue;
            }
            if !spec.flags.explicit_exports {
                export_names.insert(alias.clone());
            }
            namespaces.push(NamespaceImport {
                alias,
                module: module.clone(),
                line,
            });
            specifiers.push((module, "namespace".into()));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::Registry;

    fn parse(lang: &str, src: &str) -> ParsedFile {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name(lang).unwrap();
        extract("/t", src.as_bytes(), bound, &Limits::default()).unwrap()
    }

    #[test]
    fn python_top_level_kinds_use_libcst_vocabulary() {
        let pf = parse(
            "python",
            "import os\nfrom a import b\n\ndef f():\n    pass\n\nclass K:\n    pass\n",
        );
        assert_eq!(
            pf.top_level_node_kinds,
            vec!["Import", "ImportFrom", "FunctionDef", "ClassDef"]
        );
    }

    #[test]
    fn python_assignment_refines_away_from_expr() {
        let pf = parse("python", "x = 1\nfoo()\n");
        assert_eq!(pf.top_level_node_kinds, vec!["Assign", "Expr"]);
    }

    #[test]
    fn body_shape_counts_branches_and_nesting_per_frame() {
        let pf = parse(
            "python",
            "def f(a, b):\n    if a:\n        for x in b:\n            if x:\n                return x\n    return b\n",
        );
        assert_eq!(pf.function_scopes.len(), 1);
        let s = &pf.function_scopes[0];
        assert_eq!(s.param_count, 2);
        assert_eq!(s.branch_count, 3, "two ifs and a for");
        assert_eq!(s.max_depth, 3, "if > for > if");
    }

    #[test]
    fn nested_closures_are_their_own_frame() {
        let pf = parse(
            "python",
            "def outer(a):\n    def inner(b, c):\n        return b\n    return inner\n",
        );
        assert_eq!(pf.function_scopes.len(), 2);
        let inner = pf
            .function_scopes
            .iter()
            .find(|s| s.param_count == 2)
            .unwrap();
        assert_eq!(inner.param_count, 2);
    }

    #[test]
    fn keyword_only_params_are_classified_after_the_separator() {
        let pf = parse("python", "def f(a, *, b, c=1, **kw):\n    pass\n");
        let params = &pf.callable_signatures[0].params;
        let by = |n: &str| params.iter().find(|p| p.name == n).unwrap();
        assert_eq!(by("a").kind, "positional");
        assert_eq!(by("b").kind, "keyword");
        assert_eq!(by("c").kind, "keyword");
        assert!(by("c").optional);
        assert_eq!(by("kw").kind, "keyword_rest");
    }

    #[test]
    fn multi_hop_call_chains_record_no_edge() {
        // `a.b.c()` must NOT produce a row: the receiver is not a simple name,
        // so any edge would be a guess.
        let pf = parse("python", "def f():\n    a.b.c()\n    d.e()\n    g()\n");
        let names: Vec<&str> = pf.call_sites.iter().map(|c| c.name.as_str()).collect();
        assert!(names.contains(&"e"), "single-hop member call is recorded");
        assert!(names.contains(&"g"), "bare call is recorded");
        assert!(
            !names.contains(&"c"),
            "multi-hop chain must be dropped, got {names:?}"
        );
    }

    #[test]
    fn self_calls_are_classified_as_self_not_member() {
        let pf = parse("python", "class K:\n    def f(self):\n        self.g()\n");
        let site = &pf.call_sites[0];
        assert_eq!(site.kind, "self");
        assert_eq!(site.caller, "f");
    }

    #[test]
    fn caller_at_module_level_is_the_module_sentinel() {
        let pf = parse("python", "setup()\n");
        assert_eq!(pf.call_sites[0].caller, "<module>");
    }

    #[test]
    fn class_shape_records_bases_and_attrs_but_not_values() {
        let pf = parse(
            "python",
            "class V(APIView, Mixin):\n    permission_classes = [IsAuthenticated]\n    queryset = None\n",
        );
        let c = &pf.class_shapes[0];
        assert_eq!(c.bases, vec!["APIView", "Mixin"]);
        assert_eq!(c.extends.as_deref(), Some("APIView (+1 more)"));
        assert_eq!(c.class_attrs, vec!["permission_classes", "queryset"]);
    }

    #[test]
    fn decorated_methods_report_their_decorator_kind() {
        let pf = parse(
            "python",
            "class K:\n    @classmethod\n    def a(cls):\n        pass\n    @staticmethod\n    def b():\n        pass\n",
        );
        let a = pf
            .callable_signatures
            .iter()
            .find(|s| s.name == "a")
            .unwrap();
        let b = pf
            .callable_signatures
            .iter()
            .find(|s| s.name == "b")
            .unwrap();
        assert_eq!(a.kind, "classmethod");
        assert_eq!(b.kind, "staticmethod");
        assert_eq!(a.enclosing_class.as_deref(), Some("K"));
    }

    #[test]
    fn wildcard_import_opens_the_export_set() {
        let clean = parse("python", "from a import b\n");
        assert!(!clean.export_set_open);
        let open = parse("python", "from a import *\n");
        assert!(open.export_set_open, "star import must open the set");
    }

    #[test]
    fn imports_record_module_kind_pairs() {
        let pf = parse(
            "python",
            "import os\nimport a.b as ab\nfrom m import x, y\n",
        );
        assert!(pf
            .import_specifiers
            .contains(&("os".into(), "namespace".into())));
        assert!(pf.import_specifiers.contains(&("m".into(), "named".into())));
        assert!(pf
            .namespace_imports
            .iter()
            .any(|n| n.alias == "ab" && n.module == "a.b"));
        assert_eq!(pf.import_symbols.len(), 2);
    }

    #[test]
    fn comments_and_string_bodies_are_never_descended() {
        // A docstring naming a function must not become a call edge.
        let pf = parse(
            "python",
            "def f():\n    \"\"\"calls helper() somewhere\"\"\"\n    return 1\n",
        );
        assert!(pf.call_sites.is_empty(), "got {:?}", pf.call_sites);
    }

    #[test]
    fn oversize_file_is_refused_with_its_size() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let limits = Limits {
            max_file_bytes: 10,
            ..Default::default()
        };
        let err = extract("/t", b"x = 1234567890123", bound, &limits).unwrap_err();
        assert_eq!(err.error, "file_too_large");
        assert_eq!(err.size, Some(17));
    }

    #[test]
    fn node_ceiling_refuses_rather_than_half_reporting() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let limits = Limits {
            max_ast_nodes: 5,
            ..Default::default()
        };
        let err = extract("/t", b"def f(a,b,c):\n    return a+b+c\n", bound, &limits).unwrap_err();
        assert_eq!(err.error, "ast_node_ceiling_exceeded");
    }

    /// The property the iterative walk exists for. A recursive walk over this
    /// input overflows the stack; this must simply return.
    #[test]
    fn pathological_nesting_is_neither_a_stack_overflow_nor_quadratic() {
        let depth = 20_000;
        let src = format!("x = {}1{}\n", "(".repeat(depth), ")".repeat(depth));
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let limits = Limits {
            max_ast_nodes: usize::MAX,
            ..Default::default()
        };

        // The timing bound is a real regression guard, not decoration: asking
        // tree-sitter for `Node::parent()` once per node is O(nodes x depth),
        // which took this exact input from 40ms to 20 seconds. Loose enough not
        // to flake on a loaded machine, tight enough to catch that.
        let started = std::time::Instant::now();
        let out = extract("/t", src.as_bytes(), bound, &limits);
        let elapsed = started.elapsed();

        assert!(out.is_ok(), "deep nesting must not panic or overflow");
        assert!(
            elapsed.as_secs() < 5,
            "walk went quadratic: took {elapsed:?}"
        );
    }

    /// A decorator's own calls belong to the function it decorates, not to the
    /// enclosing scope -- that is where a recursive AST puts them, and the
    /// grammar's sibling layout would otherwise attribute them one level out.
    #[test]
    fn a_decorator_call_is_attributed_to_the_function_it_decorates() {
        let pf = parse(
            "python",
            "def outer(fn):\n    @functools.wraps(fn)\n    def inner():\n        pass\n    return inner\n",
        );
        let site = pf
            .call_sites
            .iter()
            .find(|c| c.name == "wraps")
            .expect("the decorator call is recorded");
        assert_eq!(site.caller, "inner", "not the enclosing `outer`");
    }

    /// `*args: str` is a splat wrapped in a type carrier. Classifying on the
    /// wrapper alone calls it positional and, worse, loses the keyword-only
    /// boundary it opens for every parameter after it.
    #[test]
    fn a_typed_rest_param_stays_rest_and_opens_the_keyword_region() {
        let pf = parse(
            "python",
            "def f(a: int, *args: str, timeout: float = 1.0):\n    pass\n",
        );
        let params = &pf.callable_signatures[0].params;
        let by = |n: &str| params.iter().find(|p| p.name == n).unwrap();
        assert_eq!(by("a").kind, "positional");
        assert_eq!(by("args").kind, "rest");
        assert!(by("args").optional, "a rest param can always be omitted");
        assert_eq!(
            by("timeout").kind,
            "keyword",
            "after *args, parameters are keyword-only"
        );
    }

    #[test]
    fn a_defaulted_param_is_optional_not_positional() {
        let pf = parse("python", "def f(a, b=1):\n    pass\n");
        let params = &pf.callable_signatures[0].params;
        assert_eq!(params[0].kind, "positional");
        assert_eq!(params[1].kind, "optional");
        assert!(params[1].optional);
    }

    /// `if/elif/elif` is one flat statement in this grammar but three nested
    /// `If`s in a recursive AST, and body-shape norms compare against the latter.
    #[test]
    fn chained_elif_deepens_nesting_cumulatively() {
        let flat = parse(
            "python",
            "def f(x):\n    if x == 1:\n        pass\n    elif x == 2:\n        pass\n    elif x == 3:\n        pass\n",
        );
        assert_eq!(
            flat.function_scopes[0].max_depth, 3,
            "an if plus two elifs nests three deep"
        );

        let single = parse("python", "def f(x):\n    if x:\n        pass\n");
        assert_eq!(single.function_scopes[0].max_depth, 1);
    }

    /// Frames are emitted as they are popped, so an inner closure precedes its
    /// parent. Sorting by line would look tidier and break the comparison.
    #[test]
    fn scopes_are_emitted_in_pop_order_inner_first() {
        let pf = parse(
            "python",
            "def outer(a):\n    def inner(b):\n        return b\n    return inner\n",
        );
        assert_eq!(
            pf.function_scopes[0].start_line, 2,
            "inner is emitted first"
        );
        assert_eq!(pf.function_scopes[1].start_line, 1, "outer second");
    }

    /// The parenthesized form is the one that actually breaks.
    ///
    /// On a single-line `from a import b  # noqa`, the comment is a sibling at
    /// module level and never reaches the import node -- a test written that way
    /// passes whether or not the filter exists, which is exactly how the first
    /// version of this test slipped through as a vacuous guard. Inside the
    /// parentheses the comment really is a named child of the import statement,
    /// and unfiltered it is recorded as a symbol named `# noqa: F401`.
    #[test]
    fn a_comment_inside_a_parenthesized_import_is_not_an_imported_name() {
        let pf = parse(
            "python",
            "from a.b import (  # noqa: F401\n    thing,\n    other,\n)\n",
        );
        let names: Vec<&str> = pf.import_symbols.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, vec!["thing", "other"], "got {names:?}");
        assert!(pf.import_symbols.iter().all(|s| s.module == "a.b"));
    }

    #[test]
    fn a_trailing_comment_on_a_single_line_import_is_harmless() {
        let pf = parse("python", "from a.b import thing  # noqa: F401\n");
        assert_eq!(pf.import_symbols.len(), 1);
        assert_eq!(pf.import_symbols[0].name, "thing");
        assert_eq!(pf.import_symbols[0].module, "a.b");
    }

    #[test]
    fn future_imports_carry_their_implicit_module() {
        let pf = parse("python", "from __future__ import annotations\n");
        assert_eq!(
            pf.import_specifiers,
            vec![("__future__".to_string(), "named".to_string())]
        );
        assert_eq!(pf.import_symbols[0].module, "__future__");
    }

    /// Python has no export statement, so the field is repurposed as "the sole
    /// top-level definition, when unopposed". A module holding both a class and
    /// a function reports nothing, because neither is what the file is for.
    #[test]
    fn a_sole_top_level_definition_becomes_the_default_export_kind() {
        let one_fn = parse("python", "import os\n\ndef only():\n    pass\n");
        assert_eq!(one_fn.default_export_kind.as_deref(), Some("FunctionDef"));

        let one_cls = parse("python", "class Only:\n    pass\n");
        assert_eq!(one_cls.default_export_kind.as_deref(), Some("ClassDef"));

        let both = parse("python", "class K:\n    pass\n\ndef f():\n    pass\n");
        assert_eq!(both.default_export_kind, None, "opposed -> nothing");

        let two_fns = parse("python", "def a():\n    pass\n\ndef b():\n    pass\n");
        assert_eq!(two_fns.default_export_kind, None, "not sole -> nothing");

        // A language with a real default export must not get the heuristic.
        let go = parse("go", "package m\nfunc F() {}\n");
        assert_eq!(go.default_export_kind, None);
    }

    #[test]
    fn an_annotated_assignment_is_distinguished_from_a_plain_one() {
        let pf = parse("python", "x = 1\ny: int = 2\n");
        assert_eq!(pf.top_level_node_kinds, vec!["Assign", "AnnAssign"]);
    }

    #[test]
    fn call_site_cap_reports_an_honest_total() {
        let mut src = String::from("def f():\n");
        for _ in 0..50 {
            src.push_str("    g()\n");
        }
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let limits = Limits {
            max_call_sites: 10,
            ..Default::default()
        };
        let pf = extract("/t", src.as_bytes(), bound, &limits).unwrap();
        assert_eq!(pf.call_sites.len(), 10);
        assert_eq!(pf.call_sites_total, 50, "the true count survives the cap");
        assert!(pf.call_sites_truncated);
    }

    /// Grammars that put `object`/`name` on the call node itself must still
    /// yield a receiver. Losing it is not merely less precise -- `obj.method()`
    /// records as a bare call to `method` and binds the edge to the wrong
    /// symbol, which is how a blast radius comes to lie.
    #[test]
    fn receiver_on_the_call_node_is_read_not_lost() {
        let java = parse(
            "java",
            "class A { void f() { helper(); obj.method(); this.own(); a.b.c(); } }",
        );
        let got: Vec<(&str, Option<&str>, &str)> = java
            .call_sites
            .iter()
            .map(|c| (c.kind.as_str(), c.receiver.as_deref(), c.name.as_str()))
            .collect();
        assert_eq!(
            got,
            vec![
                ("bare", None, "helper"),
                ("member", Some("obj"), "method"),
                ("self", Some("this"), "own"),
            ],
            "the a.b.c() chain must be dropped, not guessed"
        );
    }

    /// PHP wraps a plain `$obj` in a `variable_name` node, so a strict
    /// leaf-only receiver test drops a perfectly simple receiver.
    #[test]
    fn a_singly_wrapped_receiver_still_counts_as_simple() {
        let php = parse(
            "php",
            "<?php function f() { helper(); $obj->method(); $a->b->c(); }",
        );
        let got: Vec<(&str, Option<&str>)> = php
            .call_sites
            .iter()
            .map(|c| (c.name.as_str(), c.receiver.as_deref()))
            .collect();
        assert_eq!(got, vec![("helper", None), ("method", Some("$obj"))]);
    }

    /// Ruby spells imports as ordinary method calls, so only the require family
    /// counts. Without the gate every call is an import: `puts "x"` records
    /// namespace imports of `puts` AND of `"x"`, and both land in the file's
    /// export set, which then lets unrelated calls earn cross-file edges.
    #[test]
    fn only_require_family_calls_count_as_ruby_imports() {
        let pf = parse(
            "ruby",
            "require \"app/models/user\"\nrequire_relative \"helper\"\n\nclass Foo\n  def bar(a)\n    baz(a)\n    puts \"x\"\n  end\nend\n",
        );
        let modules: Vec<&str> = pf
            .import_specifiers
            .iter()
            .map(|(m, _)| m.as_str())
            .collect();
        assert_eq!(
            modules,
            vec!["app/models/user", "helper"],
            "baz and puts are calls, not imports; got {modules:?}"
        );
        for noise in ["baz", "puts", "\"x\"", "(a)"] {
            assert!(
                !pf.named_export_names.iter().any(|n| n == noise),
                "{noise} must not reach the export set: {:?}",
                pf.named_export_names
            );
        }
    }

    #[test]
    fn other_languages_extract_structurally() {
        let go = parse("go", "package m\nfunc Add(a int, b int) int {\n\tif a > b {\n\t\treturn a\n\t}\n\treturn b\n}\n");
        assert_eq!(go.callable_signatures[0].name, "Add");
        assert_eq!(go.function_scopes[0].branch_count, 1);

        let rs = parse(
            "rust",
            "fn main() { let x = compute(); }\nfn compute() -> i32 { 1 }\n",
        );
        assert_eq!(rs.callable_signatures.len(), 2);
        assert!(rs.call_sites.iter().any(|c| c.name == "compute"));

        let java = parse(
            "java",
            "class A {\n  int f(int a) { if (a > 0) { return a; } return 0; }\n}\n",
        );
        assert_eq!(java.class_shapes[0].name, "A");
        assert_eq!(
            java.callable_signatures[0].enclosing_class.as_deref(),
            Some("A")
        );

        let rb = parse("ruby", "class Foo\n  def bar(a)\n    baz(a)\n  end\nend\n");
        assert_eq!(rb.class_shapes[0].name, "Foo");
        assert!(rb.callable_signatures.iter().any(|s| s.name == "bar"));
    }
}
