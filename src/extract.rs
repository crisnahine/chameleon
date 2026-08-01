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
    /// Names bound in this frame. A method is not a closure, so each frame
    /// starts fresh rather than inheriting.
    locals: BTreeSet<String>,
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

/// The first of several candidate fields the node actually carries.
///
/// Grammars spell one slot differently across the node kinds that carry it, so
/// the spec lists every name and the first hit wins.
fn any_field<'t>(node: Node<'t>, fields: &[String]) -> Option<Node<'t>> {
    fields.iter().find_map(|f| node.child_by_field_name(f))
}

/// Descend through pure wrapper kinds to the node that carries the real name.
///
/// Bash wraps a command's name in `command_name`; Swift nests a member access's
/// property inside a `navigation_suffix`, where reading it directly yields `.g`
/// rather than `g`. Bounded for the same reason the declarator chain is: a
/// malformed tree need not be finite.
fn unwrap_kinds<'t>(node: Node<'t>, kinds: &[String]) -> Node<'t> {
    let mut n = node;
    for _ in 0..4 {
        if !kinds.iter().any(|k| k == n.kind()) {
            return n;
        }
        match n.named_child(0) {
            Some(inner) if inner.id() != n.id() => n = inner,
            _ => return n,
        }
    }
    n
}

/// A slot resolved by field, or failing that by direct child kind.
fn slot<'t>(node: Node<'t>, fields: &[String], child: &Option<String>) -> Option<Node<'t>> {
    if let Some(n) = any_field(node, fields) {
        return Some(n);
    }
    let want = child.as_deref()?;
    let mut c = node.walk();
    let found = node.named_children(&mut c).find(|n| n.kind() == want);
    found
}

/// The first node in a declarator chain that carries a parameter list.
fn parameter_holder<'t>(node: Node<'t>, spec: &LanguageSpec) -> Option<Node<'t>> {
    let mut n = field_node(node, &spec.fields.name)?;
    for _ in 0..8 {
        if any_field(n, &spec.fields.parameters).is_some() {
            return Some(n);
        }
        if !spec.fields.declarator_unwrap.iter().any(|k| k == n.kind()) {
            return None;
        }
        match field_node(n, &spec.fields.name).or_else(|| n.named_child(0)) {
            Some(inner) if inner.id() != n.id() => n = inner,
            _ => return None,
        }
    }
    None
}

/// The node a declaration's name or parameter list really lives on.
///
/// C wraps both in a `function_declarator`, so reading the field once gives
/// `f(int a)` for the name and nothing for the parameters. The loop is bounded
/// because a declarator chain (pointer-to-function-returning-pointer) is finite
/// but a malformed tree need not be.
fn declarator_node<'t>(node: Node<'t>, spec: &LanguageSpec) -> Node<'t> {
    let mut n = node;
    for _ in 0..8 {
        if !spec.fields.declarator_unwrap.iter().any(|k| k == n.kind()) {
            break;
        }
        match field_node(n, &spec.fields.name).or_else(|| n.named_child(0)) {
            Some(inner) if inner.id() != n.id() => n = inner,
            _ => break,
        }
    }
    n
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
/// A decorator is either a child of the declaration (Java's `modifiers`) or an
/// immediately PRECEDING sibling of it -- Python nests both inside a
/// `decorated_definition` wrapper, Rust and Scala leave them loose in the
/// enclosing block. Only the contiguous run of decorator siblings directly
/// before the declaration counts. Scanning the whole parent block instead
/// attributed a sibling's attribute to every declaration around it: with
/// `#[test] fn a() {} fn helper() {}`, `helper` came back decorated `test`, and
/// a file-level `#[derive(Debug)]` decorated everything in the file. Decorators
/// are a primary archetype signal, so that is fabricated cluster evidence.
fn decorators_of(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    if spec.flags.decorator_nodes.is_empty() {
        return Vec::new();
    }
    let is_decorator = |n: &Node| spec.flags.decorator_nodes.iter().any(|k| k == n.kind());
    let name_of = |n: Node| {
        // `@app.route("/x")` records as `app.route` and `#[derive(Debug)]` as
        // `derive`: the arguments are data, and keying on them would make every
        // route and every derive list a distinct decorator.
        let raw = text(n, source)
            .trim()
            .trim_start_matches("#[")
            .trim_start_matches('@')
            .trim_start_matches('[')
            .trim();
        raw.split(['(', '\n', '[', ']'])
            .next()
            .unwrap_or(raw)
            .trim()
            .to_string()
    };

    let mut out = Vec::new();
    let mut prev = node.prev_named_sibling();
    while let Some(p) = prev.filter(is_decorator) {
        out.push(name_of(p));
        prev = p.prev_named_sibling();
    }
    out.reverse();

    let mut c = node.walk();
    out.extend(node.children(&mut c).filter(is_decorator).map(name_of));

    out.retain(|n| !n.is_empty());
    out.dedup();
    out
}

/// Classify one parameter node against the spec's kind sets.
fn param_of(node: Node, spec: &LanguageSpec, source: &[u8], keyword_only: bool) -> Option<Param> {
    let p = &spec.params;
    if p.counted_only.iter().any(|k| k == node.kind()) {
        return None;
    }
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
    } else if p.keyword.iter().any(|k| k == kind_str) {
        // Required unless it carries a default.
        let has_default = p
            .optional_when_field
            .as_deref()
            .is_some_and(|f| inner.child_by_field_name(f).is_some());
        ("keyword", has_default)
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
    let mut name = raw.trim_start_matches('*').trim().to_string();
    if let Some(suffix) = p.strip_name_suffix.as_deref() {
        name = name.trim_end_matches(suffix).to_string();
    }
    if kind == "keyword_rest" {
        if let Some(placeholder) = p.keyword_rest_name.as_deref() {
            name = placeholder.to_string();
        }
    }
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

/// The target field an assignment binds through, when a spec names none.
static LEFT_FIELD: std::sync::LazyLock<String> = std::sync::LazyLock::new(|| "left".to_string());

/// Every identifier name bound beneath a node.
///
/// Explicitly stacked rather than recursive, for the same reason the main walk
/// is: the input is arbitrary repo source, and a deeply nested destructuring
/// pattern (`a, (a, (a, ...)) = x`) is small enough in bytes and node count to
/// clear both the file-size and node-ceiling guards while being thousands of
/// levels deep. A stack overflow aborts the process rather than unwinding, so
/// one such file would kill the run for every remaining path in the corpus.
fn collect_bound_names(node: Node, out: &mut Vec<String>, source: &[u8]) {
    let mut queue = vec![node];
    while let Some(n) = queue.pop() {
        // `self.attr = v` and `cache[key] = 1` bind NOTHING: the left side is a
        // setter send and an index write. Harvesting their identifiers records
        // phantom locals, and every later send of that name is then silently
        // dropped -- the standard Rails `before_validation` shape.
        if matches!(
            n.kind(),
            "call"
                | "element_reference"
                | "scope_resolution"
                | "instance_variable"
                | "class_variable"
        ) {
            continue;
        }
        if n.kind() == "identifier" {
            let t = text(n, source).trim().to_string();
            if !t.is_empty() {
                out.push(t);
            }
            continue;
        }
        let mut c = n.walk();
        queue.extend(n.named_children(&mut c));
    }
}

/// The name of a bare identifier that is a send rather than a variable read.
///
/// Conservative in the safe direction, matching the reference: an identifier is
/// promoted only when nothing bound it AND the grammar has not already given it
/// another meaning in this position (a method name, a parameter, a binding).
fn promotable_send(
    node: Node,
    spec: &LanguageSpec,
    source: &[u8],
    func_stack: &[FuncFrame],
    file_locals: &BTreeSet<String>,
) -> Option<String> {
    let name = text(node, source).trim().to_string();
    if name.is_empty() || spec.calls.pseudo_variables.iter().any(|p| p == &name) {
        return None;
    }
    // A method is not a closure: only this frame's bindings are visible.
    let bound = match func_stack.last() {
        Some(f) => f.locals.contains(&name),
        None => file_locals.contains(&name),
    };
    if bound {
        return None;
    }
    let parent = node.parent()?;
    let pk = parent.kind();
    if spec
        .calls
        .identifier_non_call_parents
        .iter()
        .any(|k| k == pk)
    {
        // A call's RECEIVER is a send in its own right; its METHOD name is
        // already reported by the call row itself.
        if pk == "call" {
            let is_receiver = parent
                .child_by_field_name("receiver")
                .is_some_and(|r| r.id() == node.id());
            return is_receiver.then_some(name);
        }
        return None;
    }
    // A parameter's NAME is a binding; its DEFAULT VALUE is an ordinary
    // expression.
    if matches!(pk, "optional_parameter" | "keyword_parameter") {
        let is_name = parent
            .child_by_field_name("name")
            .is_some_and(|n| n.id() == node.id());
        return (!is_name).then_some(name);
    }
    Some(name)
}

/// Parameters that occupy a slot without emitting a shape row.
fn counted_only_params(node: Node, spec: &LanguageSpec) -> usize {
    if spec.params.counted_only.is_empty() {
        return 0;
    }
    let Some(list) = any_field(node, &spec.fields.parameters) else {
        return 0;
    };
    let mut c = list.walk();
    list.named_children(&mut c)
        .filter(|n| spec.params.counted_only.iter().any(|k| k == n.kind()))
        .count()
}

/// Every parameter of a callable, in source order.
fn params_of(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<Param> {
    // The parameter list may sit on a nested declarator rather than the
    // declaration itself.
    // Walk the declarator chain to the first node that actually carries the
    // parameter list. Descending all the way (as the NAME resolution does)
    // lands on the bare identifier, which has no parameters -- which is why
    // every pointer-returning C function reported an arity of 0.
    let list =
        match slot(node, &spec.fields.parameters, &spec.child_kinds.parameters).or_else(|| {
            parameter_holder(node, spec)
                .and_then(|h| slot(h, &spec.fields.parameters, &spec.child_kinds.parameters))
        }) {
            Some(l) => l,
            // Swift hangs `parameter` nodes straight off the declaration with no
            // list node and no field, so the declaration itself is the list. Only
            // kinds the spec classifies as parameters are picked up, and the
            // declaration's other children (modifiers, type parameters, the body)
            // classify as nothing.
            None if spec.params.inline => node,
            None => return Vec::new(),
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
        let callee = slot(
            node,
            &spec.fields.call_function,
            &spec.child_kinds.call_function,
        )
        .or_else(|| node.named_child(0))?;
        return Some((text(callee, source).to_string(), None, "new".to_string()));
    }

    // Grammars that carry the receiver and name on the call node itself.
    if let Some(name_field) = &spec.calls.name_field {
        if let Some(name_node) = node.child_by_field_name(name_field.as_str()) {
            // Modelled as something other than a call by the reference
            // (Ruby's `super` / `yield` are their own node types there).
            if spec
                .calls
                .exclude_method_kinds
                .iter()
                .any(|k| k == name_node.kind())
            {
                return None;
            }
            let mut name = text(name_node, source).to_string();
            if name.is_empty() {
                return None;
            }
            // Operator sends can never be index-resolved, and would crowd real
            // calls out of the per-file cap.
            if spec.calls.identifier_names_only
                && !name.starts_with(|c: char| c.is_alphabetic() || c == '_')
            {
                return None;
            }
            // `recv.foo = x` calls the SETTER `foo=`; the grammar drops the `=`
            // that is part of the method's real name. `recv.foo += x` is a
            // different node type to the reference and contributes no row.
            if spec.calls.setter_suffix_on_assignment {
                if let Some(parent) = node.parent() {
                    let is_target = parent
                        .child_by_field_name("left")
                        .is_some_and(|l| l.id() == node.id());
                    if is_target {
                        match parent.kind() {
                            "operator_assignment" => return None,
                            "assignment" => name.push('='),
                            _ => {}
                        }
                    }
                }
            }
            let recv = any_field(node, &spec.calls.receiver_field);
            return Some(match recv {
                Some(r)
                    if spec
                        .calls
                        .constant_receiver_nodes
                        .iter()
                        .any(|k| k == r.kind()) =>
                {
                    // A statically-resolvable constant path.
                    (
                        name,
                        Some(text(r, source).trim().to_string()),
                        "constant".to_string(),
                    )
                }
                Some(r) => {
                    // A receiver with no name -- a literal, a parenthesized
                    // expression -- yields no row rather than one read off its
                    // source span.
                    if !spec.calls.receiver_name_nodes.is_empty()
                        && !spec.calls.receiver_name_nodes.iter().any(|k| k == r.kind())
                    {
                        return None;
                    }
                    let rt = match simple_receiver(r, source) {
                        Some(s) => s.to_string(),
                        None if spec.flags.receiver_from_method => {
                            // A chained call: `1.day.freeze` names its receiver
                            // by the inner call's method.
                            let m = spec
                                .calls
                                .name_field
                                .as_deref()
                                .and_then(|f| r.child_by_field_name(f))
                                .map(|n| text(n, source).to_string())
                                .unwrap_or_default();
                            if m.is_empty() {
                                return None;
                            }
                            m
                        }
                        None => return None,
                    };
                    if spec.calls.self_names.iter().any(|s| s == &rt) {
                        (name, Some(rt), self_kind(spec))
                    } else {
                        (name, Some(rt), "member".to_string())
                    }
                }
                None => (name, None, "bare".to_string()),
            });
        }
    }

    let mut callee = slot(
        node,
        &spec.fields.call_function,
        &spec.child_kinds.call_function,
    )
    .or_else(|| node.named_child(0))?;
    callee = unwrap_kinds(callee, &spec.calls.callee_unwrap);

    if spec.calls.member_nodes.iter().any(|k| k == callee.kind()) {
        // Fields first; a grammar that names neither (kotlin-ng) puts the
        // receiver and the property in the first two named-child slots.
        let (obj, prop) = if spec.calls.member_positional {
            (callee.named_child(0)?, callee.named_child(1)?)
        } else {
            (
                slot(
                    callee,
                    &spec.fields.member_object,
                    &spec.child_kinds.member_object,
                )?,
                slot(
                    callee,
                    &spec.fields.member_property,
                    &spec.child_kinds.member_property,
                )?,
            )
        };
        let obj = unwrap_kinds(obj, &spec.calls.member_unwrap);
        let prop = unwrap_kinds(prop, &spec.calls.member_unwrap);
        let name = text(prop, source).to_string();
        let recv_text = text(obj, source).to_string();

        // `svc.api.deep.sync()` has a member expression as its receiver and is
        // dropped rather than guessed at.
        let is_self = spec.calls.self_names.iter().any(|s| s == &recv_text);
        if !is_self && simple_receiver(obj, source).is_none() {
            return None;
        }
        if is_self {
            return Some((name, Some(recv_text), self_kind(spec)));
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
    let mut default_export_kind: Option<String> = None;
    let mut top_level_funcs = 0usize;
    let mut top_level_classes = 0usize;
    let mut top_level_func_kind: Option<String> = None;
    let mut top_level_class_kind: Option<String> = None;

    let mut func_stack: Vec<FuncFrame> = Vec::new();
    let mut class_stack: Vec<ClassFrame> = Vec::new();
    let mut file_locals: BTreeSet<String> = BTreeSet::new();

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
            // `export default ...` is the module's default export, NOT one of
            // its named ones. Both halves matter: the record's
            // `default_export_kind` is what a consumer resolves a default import
            // against, and counting the declaration among the named exports puts
            // a name in the export set that no `import { X }` can ever bind.
            let is_default_export = spec.flags.has_default_export && {
                let mut dc = node.walk();
                let has = node.children(&mut dc).any(|ch| ch.kind() == "default");
                has
            };
            if is_default_export && default_export_kind.is_none() {
                let inner = any_field(node, &spec.fields.declaration)
                    .or_else(|| node.named_child(node.named_child_count() as u32 - 1));
                default_export_kind = inner.and_then(|i| {
                    spec.default_export_kinds
                        .get(i.kind())
                        .cloned()
                        .or_else(|| spec.top_level_kind(i.kind()).map(str::to_string))
                        .filter(|k| !k.is_empty())
                });
            }
            if was_wrapped {
                let last = (node.named_child_count() as u32).saturating_sub(1);
                if let Some(inner) = node.named_child(last) {
                    // Unwrap only when the inner node is itself a meaningful
                    // top-level kind. `export { x }`, `export * from` and
                    // `export default x` wrap a clause or a bare identifier, and
                    // unwrapping those dropped the statement from the record
                    // entirely rather than recording it as an export.
                    if spec.top_level_kind(inner.kind()).is_some() {
                        node = inner;
                    }
                }
            }
            let is_exported = (!spec.flags.explicit_exports || was_wrapped) && !is_default_export;
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

            // A value export counts toward the module's public surface as much
            // as a function does: `export const X = 1` is one exported name.
            if spec.flags.explicit_exports
                && is_exported
                && !spec.is_function(node.kind())
                && !spec.is_class(node.kind())
            {
                for name in declarator_names(node, spec, source) {
                    named_export_count += 1;
                    export_names.insert(name);
                }
            }

            // `export * from "m"` -- a bare `*` token on the statement.
            if spec
                .imports
                .star_export_nodes
                .iter()
                .any(|k| k == node.kind())
            {
                let mut c = node.walk();
                if node.children(&mut c).any(|ch| ch.kind() == "*") {
                    export_set_open = true;
                }
            }
            // `export { a, b }` -- the clause names the exports.
            for name in export_clause_names(node, spec, source) {
                named_export_count += 1;
                export_names.insert(name);
            }

            if (spec.is_function(node.kind()) || spec.is_class(node.kind())) && is_exported {
                named_export_count += 1;
                if spec.is_class(node.kind()) {
                    top_level_classes += 1;
                    top_level_class_kind = Some(refined_kind.clone());
                } else {
                    top_level_funcs += 1;
                    top_level_func_kind = Some(refined_kind.clone());
                }
                // `is_exported` is already required to enter this block; the
                // gate is not repeated here. The declarator chain is descended
                // for the same reason the signature does it: C's name field is
                // a `function_declarator`, so the raw text is
                // `*make(int a, char *b)` rather than `make`, and an export set
                // keyed on that matches no importer.
                if let Some(n) = field_node(node, &spec.fields.name) {
                    let name = text(declarator_node(n, spec), source).trim().to_string();
                    if !name.is_empty() {
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
        // Classification applies to NAMED nodes only. A grammar's keyword token
        // often carries the same kind string as the construct it introduces --
        // tree-sitter-ruby's `if` node contains an anonymous `if` token -- so an
        // unguarded set lookup counts every Ruby branch and nesting level twice.
        // Python is immune by accident (`if_statement` vs `if`), which is
        // exactly why a Python-only parity check could not see this.
        let named = node.is_named();

        if !skip && named {
            // ---- enter ----
            if spec.flags.supports_jsx && kind.starts_with("jsx_") {
                has_jsx = true;
            }

            if spec.is_class(decl.kind()) && !opened_by_wrapper {
                let name = field_text(decl, &spec.fields.class_name, source)
                    .or_else(|| field_text(decl, &spec.fields.name, source))
                    .unwrap_or_default();
                let bases = base_list(decl, spec, source);
                // Capped like callable_signatures: one generated megafile must
                // not bloat the record.
                if !name.is_empty() && class_shapes.len() < limits.max_callable_signatures {
                    let e = &spec.emit;
                    class_shapes.push(ClassShape {
                        name: name.clone(),
                        kind: e.class_kind.then(|| class_keyword(decl, spec, source)),
                        start_line: line_of(decl),
                        extends: extends_display(&bases),
                        bases: e.class_bases.then(|| bases.clone()),
                        implements: e
                            .class_implements
                            .then(|| implements_list(decl, spec, source)),
                        decorators: e
                            .class_decorators
                            .then(|| decorators_of(decl, spec, source)),
                        class_attrs: e.class_attrs.then(|| class_attrs_of(decl, spec, source)),
                        qualified: e
                            .class_qualified
                            .then(|| qualified_name(&class_stack, &name))
                            .flatten(),
                    });
                }
                // Ruby's `class << self` and Rust's `impl` carry no name, and a
                // frame with an empty one puts an empty `enclosing_class` on
                // every method inside it and an empty segment in the class path
                // -- both always-serialized, so they are wrong values rather
                // than absent ones.
                if !name.is_empty() {
                    class_stack.push(ClassFrame {
                        name,
                        base: bases.first().cloned(),
                        node_id: node.id(),
                    });
                }
            }

            if spec.is_function(decl.kind()) && !opened_by_wrapper {
                let name = field_node(decl, &spec.fields.name)
                    .map(|n| text(declarator_node(n, spec), source).to_string())
                    .filter(|n| !n.is_empty())
                    .or_else(|| {
                        // `const getUsers = () => {}` is named `getUsers`. An
                        // anonymous name makes every call inside it
                        // uncorrelatable with the function that contains them.
                        spec.flags
                            .name_from_declarator
                            .then(|| assigned_name(decl, source))
                            .flatten()
                    })
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
                        is_async: spec
                            .emit
                            .signature_is_async
                            .then(|| is_async(decl, spec, source)),
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
                        decorators: match spec.emit.signature_decorators.as_str() {
                            "never" => None,
                            // TypeScript's reference omits the key when empty; an
                            // always-present [] is a mismatch, not a tidier default.
                            "when_present" if decorators.is_empty() => None,
                            _ => Some(decorators),
                        },
                        start_line: line_of(decl),
                        end_line: decl.end_position().row + 1,
                        return_type: field_text(decl, &spec.fields.return_type, source)
                            .map(|r| r.trim_start_matches(':').trim().to_string())
                            .filter(|r| !r.is_empty()),
                    });
                }

                func_stack.push(FuncFrame {
                    name,
                    start_line: line_of(decl),
                    end_line: decl.end_position().row + 1,
                    param_count: params.len() + counted_only_params(decl, spec),
                    branch_count: 0,
                    cur_depth: 0,
                    max_depth: 0,
                    chained_held: std::collections::HashMap::new(),
                    locals: params.iter().map(|p| p.name.clone()).collect(),
                    node_id: node.id(),
                });
            }

            if let Some(frame) = func_stack.last_mut() {
                // A node listed as BOTH a callable and a nesting level (Elixir's
                // `anonymous_function`) would otherwise deepen the frame it just
                // opened, so every closure starts its own body one level down
                // and a one-line `fn` reports a depth of 1.
                let opened_here = frame.node_id == node.id();
                if spec.branch_nodes.contains(kind) {
                    frame.branch_count += 1;
                }
                if spec.nesting_nodes.contains(kind) && !opened_here {
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

            // Binding tracking, before the identifier is examined: `x = 1` binds
            // `x` for everything that follows it in the frame.
            if spec.calls.promote_unbound_identifiers {
                let mut bound: Vec<String> = Vec::new();
                if spec
                    .calls
                    .binding_assignment_nodes
                    .iter()
                    .any(|k| k == kind)
                {
                    let targets: &[String] = if spec.calls.binding_target_fields.is_empty() {
                        std::slice::from_ref(&LEFT_FIELD)
                    } else {
                        &spec.calls.binding_target_fields
                    };
                    if let Some(target) = any_field(node, targets) {
                        collect_bound_names(target, &mut bound, source);
                    }
                }
                if spec.calls.binding_scope_nodes.iter().any(|k| k == kind) {
                    collect_bound_names(node, &mut bound, source);
                }
                if !bound.is_empty() {
                    let sink = match func_stack.last_mut() {
                        Some(f) => &mut f.locals,
                        None => &mut file_locals,
                    };
                    sink.extend(bound);
                }
            }

            // A bare identifier that nothing bound is a receiverless send.
            if spec.calls.promote_unbound_identifiers && kind == "identifier" {
                if let Some(name) = promotable_send(node, spec, source, &func_stack, &file_locals) {
                    call_sites_total += 1;
                    if call_sites.len() < limits.max_call_sites {
                        call_sites.push(CallSite {
                            name,
                            receiver: None,
                            kind: "bare".to_string(),
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
        //
        // Gated on exactly the same condition as enter. A construct whose
        // keyword token shares its kind string -- tree-sitter-ruby's `if` node
        // and its anonymous `if` token -- otherwise has the level opened at
        // enter released by that token before the body is even walked, flooring
        // every nesting depth at 1. That is a worse error than the double-count
        // it replaced, and it is invisible to a Python-only parity check because
        // Python's kinds are all `*_statement`.
        if !skip && named {
            leave(
                node,
                &mut func_stack,
                &mut class_stack,
                &mut function_scopes,
                spec,
            );
        }

        loop {
            if cursor.goto_next_sibling() {
                continue 'walk;
            }
            if !cursor.goto_parent() {
                break 'walk;
            }
            let parent = cursor.node();
            if parent.is_named() && !spec.skip_subtree_nodes.contains(parent.kind()) {
                leave(
                    parent,
                    &mut func_stack,
                    &mut class_stack,
                    &mut function_scopes,
                    spec,
                );
            }
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

    // A language with no export statement repurposes the field as "the sole
    // top-level definition, when unopposed"; a module holding both a class and a
    // function reports nothing, since neither is what the file is for.
    let default_export_kind = if spec.flags.sole_definition_default_export {
        match (top_level_classes, top_level_funcs) {
            (1, 0) => top_level_class_kind,
            (0, 1) => top_level_func_kind,
            _ => None,
        }
    } else {
        default_export_kind
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
        // Gated on the same condition as the enter-side increment: a node that
        // opened this frame never deepened it, so it releases nothing.
        let opened_here = frame.node_id == node.id();
        let mut release = usize::from(spec.nesting_nodes.contains(node.kind()) && !opened_here);
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
    heritage(node, spec, source, &spec.child_kinds.superclass_item)
}

/// Interfaces a class declares, where the grammar separates them from bases.
fn implements_list(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    match &spec.child_kinds.implements_item {
        Some(_) => heritage(node, spec, source, &spec.child_kinds.implements_item),
        None => Vec::new(),
    }
}

fn heritage(node: Node, spec: &LanguageSpec, source: &[u8], item: &Option<String>) -> Vec<String> {
    let Some(sup) = slot(node, &spec.fields.superclass, &spec.child_kinds.superclass) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut c = sup.walk();
    let mut named: Vec<Node> = sup.named_children(&mut c).collect();
    // The heritage node holds CLAUSES, not bases: reading its children directly
    // yields the literal text `extends B`, and it would count an
    // `implements_clause` as a base class.
    let clause_scoped = item.is_some();
    if let Some(item) = item.as_deref() {
        named = named
            .iter()
            .filter(|n| n.kind() == item)
            .flat_map(|n| {
                let mut ic = n.walk();
                n.named_children(&mut ic).collect::<Vec<_>>()
            })
            .collect();
    }
    // A clause-scoped read that matched nothing means the class declares none of
    // that relation. Falling back to the heritage node's raw text would answer
    // "what does it implement?" with `extends A`.
    if named.is_empty() && clause_scoped {
        return out;
    }
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

/// Names bound by a variable declaration statement.
///
/// `export const A = 1, B = 2` exports two names, so the declarators are read
/// individually rather than the statement being counted once.
fn declarator_names(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    // Only a variable declaration binds names this way. Without the gate this
    // walks an enum body and reports its MEMBERS as the module's exports, so
    // `export enum E { A }` exported `A` and not `E`.
    if !matches!(
        node.kind(),
        "lexical_declaration" | "variable_declaration" | "variable_statement"
    ) {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut c = node.walk();
    for child in node.named_children(&mut c) {
        if let Some(name) = field_node(child, &spec.fields.name).map(|n| text(n, source)) {
            if !name.is_empty()
                && name
                    .chars()
                    .all(|ch| ch.is_alphanumeric() || ch == '_' || ch == '$')
            {
                out.push(name.to_string());
            }
        }
    }
    out
}

/// Names listed in an explicit export clause (`export { a, b as c }`).
///
/// The exported name is what the importer binds, so `b as c` exports `c`.
fn export_clause_names(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    if spec.imports.export_clause_nodes.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut queue = vec![node];
    while let Some(n) = queue.pop() {
        let mut c = n.walk();
        for child in n.named_children(&mut c) {
            if spec
                .imports
                .export_clause_nodes
                .iter()
                .any(|k| k == child.kind())
            {
                queue.push(child);
            } else if child.kind() == "export_specifier" {
                // `alias` field when present, else the name itself.
                let exported = child
                    .child_by_field_name("alias")
                    .or_else(|| child.child_by_field_name("name"))
                    .map(|x| text(x, source).to_string());
                if let Some(name) = exported {
                    if !name.is_empty() {
                        out.push(name);
                    }
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out
}

/// The `kind` string for a self/this-receiver call. TypeScript's reference
/// extractor says `this`; Python's says `self`.
fn self_kind(spec: &LanguageSpec) -> String {
    spec.flags
        .self_kind
        .clone()
        .unwrap_or_else(|| "self".to_string())
}

/// The variable an unnamed function expression is bound to.
///
/// Bounded to the immediate binding forms -- a declarator or a plain assignment
/// -- so a callback passed as an argument stays anonymous rather than borrowing
/// an unrelated name from further up the tree.
fn assigned_name(node: Node, source: &[u8]) -> Option<String> {
    let parent = node.parent()?;
    let holder = match parent.kind() {
        "variable_declarator" | "assignment_expression" | "public_field_definition" => parent,
        // `export const f = () => {}` nests one more level.
        _ => return None,
    };
    let name = holder
        .child_by_field_name("name")
        .or_else(|| holder.child_by_field_name("left"))?;
    let text = text(name, source).trim().to_string();
    (!text.is_empty() && !text.contains(['.', '(', '{'])).then_some(text)
}

/// The `class`/`module` keyword a declaration was introduced with.
fn class_keyword(node: Node, _spec: &LanguageSpec, _source: &[u8]) -> String {
    if node.kind().contains("module") {
        "module".to_string()
    } else {
        "class".to_string()
    }
}

/// Nesting-qualified name, when it differs from the bare one.
fn qualified_name(stack: &[ClassFrame], name: &str) -> Option<String> {
    if stack.is_empty() {
        return None;
    }
    let mut parts: Vec<&str> = stack.iter().map(|f| f.name.as_str()).collect();
    parts.push(name);
    let joined = parts.join("::");
    (joined != name).then_some(joined)
}

/// Names (never values) of direct class-body assignments.
///
/// Values are deliberately not captured: `permission_classes` being present is a
/// role signal, what it is set to is application data.
fn class_attrs_of(node: Node, spec: &LanguageSpec, source: &[u8]) -> Vec<String> {
    const MAX_CLASS_ATTRS: usize = 50;
    let Some(body) = slot(node, &spec.fields.body, &spec.child_kinds.body) else {
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
            // `import Button from "./m"` binds the module's DEFAULT export. The
            // reference records the specifier as `default` and collects no named
            // symbol, so treating the binding as a named import both mislabels
            // the specifier and invents a symbol `./m` never exported -- which a
            // phantom-import check then reports as a hallucinated binding on
            // every default import in the corpus.
            if spec
                .imports
                .default_binding_nodes
                .iter()
                .any(|k| k == child.kind())
            {
                let local = text(child, source).to_string();
                if !local.is_empty() {
                    if !spec.flags.explicit_exports {
                        export_names.insert(local);
                    }
                    specifiers.push((module.clone(), "default".into()));
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
        } else if specifiers.last().is_none_or(|(m, _)| m != &module) {
            // A side-effect import (`import "./polyfill"`) has no clause at all.
            // Emitting nothing loses the edge entirely, and a polyfill or a CSS
            // side-effect import is exactly the dependency a blast radius has to
            // see, since nothing else in the file references it.
            specifiers.push((module, "namespace".into()));
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
            let callee = slot(
                node,
                &spec.fields.call_function,
                &spec.child_kinds.call_function,
            )
            .or_else(|| node.named_child(0));
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
                && Some(child)
                    == slot(
                        node,
                        &spec.fields.call_function,
                        &spec.child_kinds.call_function,
                    )
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
        assert_eq!(
            c.bases.as_deref(),
            Some(&["APIView".to_string(), "Mixin".to_string()][..])
        );
        assert_eq!(c.extends.as_deref(), Some("APIView (+1 more)"));
        assert_eq!(
            c.class_attrs.as_deref(),
            Some(&["permission_classes".to_string(), "queryset".to_string()][..])
        );
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

    /// `export { x }`, `export * from` and `export default x` wrap a clause or
    /// a bare identifier, not a declaration. Unwrapping them unconditionally
    /// dropped the statement from the record entirely.
    #[test]
    fn every_export_form_is_recorded() {
        let pf = parse(
            "typescript",
            "function helper() { return 1; }\nexport { helper };\nexport * from \"./other\";\nexport default helper;\n",
        );
        let kinds = &pf.top_level_node_kinds;
        assert_eq!(
            kinds.iter().filter(|k| *k == "ExportDeclaration").count(),
            3,
            "all three export forms must survive: {kinds:?}"
        );
        assert!(
            pf.named_export_names.iter().any(|n| n == "helper"),
            "`export {{ helper }}` exports helper: {:?}",
            pf.named_export_names
        );
        assert!(pf.export_set_open, "`export * from` opens the set");
    }

    /// declarator_names walked an enum body and reported its MEMBERS as the
    /// module's exports, so `export enum E {{ A }}` exported `A`, not `E`.
    #[test]
    fn an_enum_member_is_not_a_module_export() {
        let pf = parse("typescript", "export enum E { A, B }\n");
        assert!(
            !pf.named_export_names.iter().any(|n| n == "A" || n == "B"),
            "enum members must not be exports: {:?}",
            pf.named_export_names
        );
    }

    #[test]
    fn javascript_import_aliases_are_split() {
        let pf = parse("javascript", "import { a as b } from \"m\";\n");
        assert_eq!(pf.import_symbols.len(), 1);
        assert_eq!(pf.import_symbols[0].name, "a", "the exported name");
        assert_eq!(pf.import_symbols[0].local, "b", "the local binding");
    }

    /// C and C++ read a function's name off `declarator` but a struct's off
    /// `name`; sharing one field made every C/C++ type shape vanish, and Go's
    /// type name sits on the inner `type_spec` rather than the declaration.
    /// Measured on 20k real files, all three reported zero classes.
    #[test]
    fn c_family_and_go_extract_type_shapes() {
        let c = parse(
            "c",
            "struct Point { int x; };\nenum Color { RED };\nint f(int a, char *b) { return a; }\n",
        );
        assert_eq!(
            c.class_shapes
                .iter()
                .map(|s| s.name.as_str())
                .collect::<Vec<_>>(),
            vec!["Point", "Color"]
        );
        // The name must not carry the parameter list, or no call site can ever
        // match the definition.
        assert_eq!(c.callable_signatures[0].name, "f");
        assert_eq!(c.callable_signatures[0].params.len(), 2);

        let go = parse(
            "go",
            "package m\ntype User struct { Name string }\ntype Repo interface { Get() error }\n",
        );
        assert_eq!(
            go.class_shapes
                .iter()
                .map(|s| s.name.as_str())
                .collect::<Vec<_>>(),
            vec!["User", "Repo"]
        );

        let cpp = parse(
            "cpp",
            "class Widget { public: int draw(); };\nstruct Box { int w; };\n",
        );
        assert_eq!(
            cpp.class_shapes
                .iter()
                .map(|s| s.name.as_str())
                .collect::<Vec<_>>(),
            vec!["Widget", "Box"]
        );
    }

    /// The bug a Python-only parity check could never see: tree-sitter-ruby's
    /// named `if` node contains an ANONYMOUS `if` keyword token of the same
    /// kind. Counting it doubles every branch and nesting level; releasing it
    /// on leave floors every depth at 1. Python is immune by accident
    /// (`if_statement` vs `if`), and a single-level case passes under BOTH
    /// bugs -- only a multi-level one separates them.
    #[test]
    fn a_keyword_token_is_neither_counted_nor_released_as_its_own_construct() {
        let one = parse(
            "ruby",
            "class A\n  def f(x)\n    if x\n      1\n    end\n  end\nend\n",
        );
        assert_eq!(one.function_scopes[0].branch_count, 1, "not two");
        assert_eq!(one.function_scopes[0].max_depth, 1, "not two");

        // Three levels: the double-count reports 6/6, the premature release
        // reports 3/1, and only a correct walk reports 3/3.
        let deep = parse(
            "ruby",
            "class A\n  def f(x)\n    if x\n      if x > 1\n        if x > 2\n          1\n        end\n      end\n    end\n  end\nend\n",
        );
        assert_eq!(deep.function_scopes[0].branch_count, 3);
        assert_eq!(
            deep.function_scopes[0].max_depth, 3,
            "depth must not floor at 1"
        );

        let py = parse(
            "python",
            "def f(x):\n    if x:\n        if x > 1:\n            if x > 2:\n                return 1\n",
        );
        assert_eq!(py.function_scopes[0].max_depth, 3);
    }

    /// `self.attr = v` and `cache[key] = 1` bind nothing: the left side is a
    /// setter send and an index write. Harvesting their identifiers as locals
    /// silently drops every later send of those names -- the standard Rails
    /// `before_validation` shape.
    #[test]
    fn a_setter_or_index_target_binds_no_local() {
        let rb = parse(
            "ruby",
            "class A\n  def f\n    self.name = \"x\"\n    name\n    cache[key] = 1\n    cache\n  end\nend\n",
        );
        let names: Vec<&str> = rb.call_sites.iter().map(|c| c.name.as_str()).collect();
        assert!(names.contains(&"name="), "the setter send: {names:?}");
        assert!(names.contains(&"name"), "the later read is still a send");
        assert!(names.contains(&"cache"), "an index target binds nothing");
        assert!(names.contains(&"key"));
    }

    /// C nests the parameter list under a declarator chain. Descending all the
    /// way lands on the bare identifier, which has none, so every
    /// pointer-returning function reported an arity of 0 into the body-shape
    /// norms mining votes on.
    #[test]
    fn a_pointer_returning_c_function_keeps_its_parameters() {
        let c = parse(
            "c",
            "int *make(int a, char *b) { return 0; }\nint plain(int a, char *b) { return a; }\n",
        );
        for sig in &c.callable_signatures {
            assert_eq!(sig.params.len(), 2, "{} lost its params", sig.name);
        }
        assert_eq!(
            c.named_export_names,
            vec!["make".to_string(), "plain".to_string()],
            "an export set keyed on raw declarator text matches no importer"
        );
    }

    /// The reference records a default import as a `default` specifier and
    /// collects NO named symbol for it. Calling it a named import also invents
    /// a symbol the module never exported, which a phantom-import check then
    /// reports as a hallucinated binding on every default import in the corpus.
    #[test]
    fn a_default_import_is_not_a_named_one() {
        let ts = parse(
            "typescript",
            "import Button from \"./Button\";\nimport \"./polyfill\";\nimport { a, b as c } from \"./m\";\n",
        );
        assert_eq!(
            ts.import_specifiers,
            vec![
                ("./Button".to_string(), "default".to_string()),
                ("./polyfill".to_string(), "namespace".to_string()),
                ("./m".to_string(), "named".to_string()),
            ],
            "a side-effect import is an edge too"
        );
        let names: Vec<&str> = ts.import_symbols.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, vec!["a", "b"], "no phantom `Button`");
    }

    /// No TS/JS class node has a `superclass` FIELD -- the bases hang off a
    /// `class_heritage` child -- so reading the field left every class in the
    /// corpus claiming, positively, to have no base.
    #[test]
    fn a_typescript_class_reports_its_base_and_its_interfaces_apart() {
        let ts = parse(
            "typescript",
            "export default class U extends B implements I {}\nexport class O extends A {}\n",
        );
        let u = &ts.class_shapes[0];
        assert_eq!(u.extends.as_deref(), Some("B"));
        assert_eq!(u.implements.as_deref(), Some(&["I".to_string()][..]));
        assert_eq!(u.bases, None, "the reference emits no `bases` for TS");
        // A clause-scoped read that matches nothing must not fall back to the
        // heritage node's raw text, which would answer "implements?" with
        // `extends A`.
        assert_eq!(ts.class_shapes[1].implements.as_deref(), Some(&[][..]));

        // `export default` is the default export, NOT a named one.
        assert_eq!(ts.default_export_kind.as_deref(), Some("ClassDeclaration"));
        assert_eq!(ts.named_export_names, vec!["O".to_string()]);
    }

    /// An attribute is a standalone sibling in Rust and Scala, so scanning the
    /// enclosing block attributed it to every declaration around it. Decorators
    /// are a primary archetype signal, so that is fabricated cluster evidence.
    #[test]
    fn a_decorator_belongs_only_to_the_declaration_it_precedes() {
        let rs = parse(
            "rust",
            "#[derive(Debug)]\nstruct S;\nmod m {\n    #[test]\n    fn a() {}\n    fn helper() {}\n}\n",
        );
        let by_name = |n: &str| {
            rs.callable_signatures
                .iter()
                .find(|s| s.name == n)
                .unwrap_or_else(|| panic!("{n} missing"))
                .decorators
                .clone()
                .unwrap_or_default()
        };
        assert_eq!(by_name("a"), vec!["test".to_string()]);
        assert!(
            by_name("helper").is_empty(),
            "a sibling's attribute is not its own"
        );
        assert_eq!(
            rs.class_shapes[0].decorators.as_deref(),
            Some(&["derive".to_string()][..]),
            "the arguments are data; `derive(Debug)` keys as `derive`"
        );

        // Python nests both in a wrapper, where the decorator is still the
        // declaration's own preceding sibling.
        let py = parse("python", "@app.route(\"/x\")\ndef f():\n    pass\n");
        assert_eq!(
            py.callable_signatures[0].decorators.as_deref(),
            Some(&["app.route".to_string()][..])
        );
    }

    /// `for i in items` binds `i`. Binding the whole node harvested the iterable
    /// and the entire loop body, so every send inside the loop -- and every send
    /// of those names for the rest of the method -- was silently dropped.
    #[test]
    fn a_ruby_for_loop_binds_only_its_loop_variable() {
        let rb = parse(
            "ruby",
            "class Foo\n  def f\n    for i in items\n      helper\n    end\n    helper\n  end\nend\n",
        );
        let names: Vec<&str> = rb.call_sites.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, vec!["items", "helper", "helper"]);
        assert!(!names.contains(&"i"), "the loop variable is bound");
    }

    /// A class node with no resolvable name opened a frame anyway, putting an
    /// empty `enclosing_class` on every method inside it and an empty segment in
    /// the class path -- both always-serialized, so wrong values not absent ones.
    #[test]
    fn an_unnamed_class_node_opens_no_frame() {
        let rb = parse(
            "ruby",
            "class Foo\n  class << self\n    def bar; end\n  end\nend\n",
        );
        let bar = rb
            .callable_signatures
            .iter()
            .find(|s| s.name == "bar")
            .expect("bar");
        assert_eq!(bar.enclosing_class.as_deref(), Some("Foo"));
        assert_eq!(bar.enclosing_class_path.as_deref(), Some("Foo"));
    }

    /// Every one of these languages emitted an empty slot that read downstream
    /// as a verified absence: no calls at all, or an arity of 0.
    #[test]
    fn the_positional_grammars_still_yield_calls_and_parameters() {
        let sw = parse(
            "swift",
            "class A: B { func f(x: Int, y: String) { super.g(); self.h(); obj.k() } }",
        );
        assert_eq!(sw.callable_signatures[0].params.len(), 2, "swift arity");
        assert_eq!(
            sw.call_sites
                .iter()
                .map(|c| (c.name.as_str(), c.kind.as_str()))
                .collect::<Vec<_>>(),
            vec![("g", "super"), ("h", "self"), ("k", "member")]
        );

        let kt = parse(
            "kotlin",
            "class A : B() { fun f(x: Int, y: String) { super.g(); this.h(); obj.k() } }",
        );
        assert_eq!(kt.callable_signatures[0].params.len(), 2, "kotlin arity");
        assert_eq!(
            kt.call_sites
                .iter()
                .map(|c| (c.name.as_str(), c.kind.as_str()))
                .collect::<Vec<_>>(),
            vec![("g", "super"), ("h", "self"), ("k", "member")]
        );

        // Bash wraps the callee in `command_name`, so every call was dropped and
        // the record read as a verified-empty script.
        let sh = parse("bash", "build() { make all; }\nbuild\n");
        assert_eq!(
            sh.call_sites
                .iter()
                .map(|c| c.name.as_str())
                .collect::<Vec<_>>(),
            vec!["make", "build"]
        );
    }

    /// A receiver read off the wrong field is worse than a missing one: the row
    /// still exists, classified `bare`, and binds to any same-named function.
    #[test]
    fn a_static_or_method_receiver_is_never_silently_dropped() {
        let php = parse(
            "php",
            "<?php\nclass A { function f() { Foo::create(); $this->g(); $obj->h(); } }\n",
        );
        assert_eq!(
            php.call_sites
                .iter()
                .map(|c| (c.name.as_str(), c.receiver.as_deref()))
                .collect::<Vec<_>>(),
            vec![
                ("create", Some("Foo")),
                ("g", Some("$this")),
                ("h", Some("$obj"))
            ]
        );

        // Lua names a dotted access's property `field` and a method call's
        // `method`; one name per slot loses the other kind entirely.
        let lua = parse("lua", "function f() obj:m(); tbl.n() end\n");
        assert_eq!(
            lua.call_sites
                .iter()
                .map(|c| (c.name.as_str(), c.receiver.as_deref()))
                .collect::<Vec<_>>(),
            vec![("m", Some("obj")), ("n", Some("tbl"))]
        );
    }

    /// A node listed as both a callable and a nesting level deepened the frame
    /// it had just opened, so a one-line closure reported a depth of 1.
    #[test]
    fn a_closure_does_not_deepen_the_frame_it_opens() {
        let ex = parse("elixir", "f = fn y -> y end\n");
        assert_eq!(ex.function_scopes[0].max_depth, 0);
    }

    /// A spec kind no grammar node carries is silently inert. These three were
    /// misspellings against their own grammars.
    #[test]
    fn previously_dead_spec_kinds_are_live() {
        let hs = parse(
            "haskell",
            "classify :: Int -> Int\nclassify x\n  | x > 10 = 1\n  | x > 0 = 2\n  | otherwise = 3\n",
        );
        assert!(
            hs.function_scopes.iter().any(|s| s.branch_count > 0),
            "Haskell guards are branches"
        );

        let cs = parse(
            "csharp",
            "class A { void f(int[] xs) { foreach (var x in xs) { if (x>0) {} } } }\n",
        );
        assert!(
            cs.function_scopes[0].max_depth >= 2,
            "foreach opens a level"
        );

        let php = parse("php", "<?php $f = function ($x) { return $x; };\n");
        assert_eq!(php.callable_signatures.len(), 1, "a closure is a callable");
    }

    /// Ruby has no syntax separating a receiverless send from a local read;
    /// only the binding set tells them apart, so without promotion most real
    /// Ruby call rows are missing -- and with careless promotion, locals become
    /// phantom calls.
    #[test]
    fn ruby_promotes_unbound_identifiers_but_not_locals() {
        let rb = parse(
            "ruby",
            "class A\n  def f(x)\n    helper\n    y = 1\n    y\n    x\n    config.load_defaults\n  end\nend\n",
        );
        let names: Vec<&str> = rb.call_sites.iter().map(|c| c.name.as_str()).collect();
        assert!(names.contains(&"helper"), "unbound -> a send: {names:?}");
        assert!(names.contains(&"config"), "a call receiver is a send too");
        assert!(!names.contains(&"y"), "an assigned local is not a send");
        assert!(!names.contains(&"x"), "a parameter is not a send");
    }

    /// `recv.foo = v` calls the setter `foo=`; `recv.foo += v` is a different
    /// node to the reference and contributes no row at all.
    #[test]
    fn ruby_setter_assignment_names_the_setter() {
        let rb = parse(
            "ruby",
            "class A\n  def bot=(v)\n    self.actor_type = v\n    self.count += 1\n  end\nend\n",
        );
        let names: Vec<&str> = rb.call_sites.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, vec!["actor_type="], "got {names:?}");
    }

    /// A chained call names its receiver by the inner call's METHOD, not by the
    /// receiver's source span -- reading the span instead is the single largest
    /// fidelity error available here.
    #[test]
    fn ruby_chained_calls_name_the_receiver_by_its_method() {
        let rb = parse(
            "ruby",
            "class A\n  def f\n    Order.recent.limit(5)\n  end\nend\n",
        );
        let rows: Vec<(&str, Option<&str>, &str)> = rb
            .call_sites
            .iter()
            .map(|c| (c.kind.as_str(), c.receiver.as_deref(), c.name.as_str()))
            .collect();
        assert!(
            rows.contains(&("member", Some("recent"), "limit")),
            "got {rows:?}"
        );
        assert!(
            rows.contains(&("constant", Some("Order"), "recent")),
            "got {rows:?}"
        );
    }

    /// Ruby's class row carries `kind` and no `bases`/`decorators`/`class_attrs`;
    /// emitting a field the reference never carried is a mismatch even when the
    /// value is the obvious default.
    #[test]
    fn ruby_class_rows_carry_only_the_fields_the_reference_emits() {
        let rb = parse("ruby", "class Foo < Bar\n  def x; end\nend\n");
        let c = &rb.class_shapes[0];
        assert_eq!(c.kind.as_deref(), Some("class"));
        assert_eq!(c.extends.as_deref(), Some("Bar"));
        assert!(c.bases.is_none(), "Ruby carries no bases list");
        assert!(c.decorators.is_none());
        assert!(c.class_attrs.is_none());
        // Python still carries all three.
        let py = parse("python", "class K(Base):\n    x = 1\n");
        assert!(py.class_shapes[0].bases.is_some());
        assert!(py.class_shapes[0].class_attrs.is_some());
        assert!(py.class_shapes[0].kind.is_none());
    }

    /// `export const getUsers = () => {}` is named `getUsers`. An anonymous name
    /// makes every call inside it uncorrelatable with its container.
    #[test]
    fn an_arrow_function_takes_its_name_from_its_binding() {
        let ts = parse(
            "typescript",
            "export const getUsers = async () => { return fetchAll(); };\n",
        );
        assert_eq!(ts.callable_signatures[0].name, "getUsers");
        assert_eq!(ts.call_sites[0].caller, "getUsers");
        // A callback argument stays anonymous rather than borrowing a name.
        let cb = parse("typescript", "run(() => { helper(); });\n");
        assert!(cb
            .callable_signatures
            .iter()
            .all(|s| s.name == "<anonymous>"));
    }

    /// The reference vocabulary differs: TypeScript says `this`, Python `self`.
    #[test]
    fn the_self_call_kind_follows_the_language() {
        let ts = parse("typescript", "class A { f() { this.g(); } }\n");
        assert_eq!(ts.call_sites[0].kind, "this");
        let py = parse("python", "class A:\n    def f(self):\n        self.g()\n");
        assert_eq!(py.call_sites[0].kind, "self");
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
