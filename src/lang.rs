//! Declarative language specs and the grammar registry.
//!
//! This is the module the whole "one engine, many languages" claim rests on.
//! Chameleon's equivalent layer is ~2,500 lines of hand-written Python across
//! three modules, most of it per-language *callables*; adding a fourth language
//! there means writing a fourth module and editing the core. Here a language is
//! a TOML file: node-kind sets, a kind-translation table, field names, and a few
//! flags. The walker reads the spec and never learns a language's name.
//!
//! The honest boundary: a spec expresses what can be said with node kinds and
//! field names. That covers top-level kinds, body shape, callables, classes,
//! calls, and imports -- the bulk of the contract. Where a language needs real
//! per-node logic (Ruby's `class << self`, TS's binding patterns), the spec has
//! flags for the cases that matter and the walker degrades gracefully rather
//! than guessing.

use std::collections::{BTreeMap, HashMap, HashSet};

use anyhow::{anyhow, Result};
use serde::{Deserialize, Deserializer};
use tree_sitter::Language;

/// A slot the walker reads off a node, written in the spec as one field name or
/// several tried in order.
///
/// Grammars name the same slot differently across the node kinds that carry it:
/// Lua puts a dotted access's property on `field` and a method call's on
/// `method`, and PHP puts an instance call's receiver on `object` and a static
/// call's on `scope`. One name per slot silently loses the other kind -- and
/// loses it as a WRONG row rather than a missing one, since `Foo::create()`
/// then records as a bare call to `create` and binds to any same-named
/// function.
pub type FieldNames = Vec<String>;

/// Accept either `field = "x"` or `field = ["x", "y"]`.
fn one_or_many<'de, D: Deserializer<'de>>(d: D) -> Result<FieldNames, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum OneOrMany {
        One(String),
        Many(Vec<String>),
    }
    Ok(match OneOrMany::deserialize(d)? {
        OneOrMany::One(s) => vec![s],
        OneOrMany::Many(v) => v,
    })
}

/// Field names the walker asks a node for. Every one is optional: a spec that
/// omits `name_field` simply yields unnamed declarations rather than failing.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Fields {
    /// Field holding a declaration's identifier.
    pub name: Option<String>,
    /// Field holding a CLASS/type's identifier, when it differs from the
    /// function one. C reads a function's name off `declarator` but a struct's
    /// off `name`; sharing one field made every C and C++ type shape vanish.
    pub class_name: Option<String>,
    /// Field holding a callable's parameter list.
    #[serde(deserialize_with = "one_or_many")]
    pub parameters: FieldNames,
    /// Field holding a declaration's body.
    #[serde(deserialize_with = "one_or_many")]
    pub body: FieldNames,
    /// Field holding a declared return type.
    pub return_type: Option<String>,
    /// Field holding the declaration an export statement wraps.
    #[serde(deserialize_with = "one_or_many")]
    pub declaration: FieldNames,
    /// Field holding a class's superclass/base list.
    #[serde(deserialize_with = "one_or_many")]
    pub superclass: FieldNames,
    /// Field holding a call's callee expression.
    #[serde(deserialize_with = "one_or_many")]
    pub call_function: FieldNames,
    /// On a member expression: the receiver, then the property.
    #[serde(deserialize_with = "one_or_many")]
    pub member_object: FieldNames,
    #[serde(deserialize_with = "one_or_many")]
    pub member_property: FieldNames,
    /// Field holding a parameter's type annotation.
    pub param_type: Option<String>,
    /// Node kinds to descend through when reading a declaration's name or
    /// parameter list. C nests both under a `function_declarator`, so reading
    /// the field once yields `f(int a)` as the name and no parameters at all.
    pub declarator_unwrap: Vec<String>,
}

/// How calls are recognized and classified.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CallSpec {
    /// Node kinds that are a call.
    pub nodes: Vec<String>,
    /// Node kinds to descend through when reading a member access's receiver or
    /// property. Swift nests the property one level deeper, in a
    /// `navigation_suffix`, so the property reads as `.g` rather than `g`.
    pub member_unwrap: Vec<String>,
    /// The receiver and the property are the member node's first two named
    /// children, with no fields naming them (kotlin-ng's
    /// `navigation_expression`). Every such call is otherwise dropped.
    pub member_positional: bool,
    /// Node kinds that are a member access (`a.b`), used to split receiver/name.
    pub member_nodes: Vec<String>,
    /// Node kinds to descend through when reading the CALLEE. Bash nests a
    /// command's name in a `command_name` wrapper, so the callee resolves to a
    /// node with named children and every Bash call is dropped -- a record that
    /// reads as a verified-empty script rather than an unsupported one.
    pub callee_unwrap: Vec<String>,
    /// Node kinds that are a constructor call (`new Foo()`).
    pub constructor_nodes: Vec<String>,
    /// Receiver texts that mean "this object" (`self`, `this`, `cls`).
    pub self_names: Vec<String>,
    /// Node kinds that denote a superclass call.
    pub super_nodes: Vec<String>,
    /// Some grammars put the receiver and the method name directly on the call
    /// node instead of nesting a member expression under it: Java's
    /// `method_invocation` carries `object` and `name`, and PHP's
    /// `member_call_expression` the same. Without these the receiver is lost and
    /// `obj.method()` records as a bare call to `method`, which is not merely
    /// less precise -- it is wrong, and it binds the edge to the wrong symbol.
    #[serde(deserialize_with = "one_or_many")]
    pub receiver_field: FieldNames,
    pub name_field: Option<String>,
    /// Receiver node kinds that denote a statically-resolvable constant, which
    /// the reference records with its own `constant` kind.
    pub constant_receiver_nodes: Vec<String>,
    /// Receiver node kinds that HAVE a name. When set, anything else -- a
    /// literal, a parenthesized expression -- yields no row at all rather than
    /// a receiver read off its source span.
    pub receiver_name_nodes: Vec<String>,
    /// Method node kinds the reference models as something other than a call
    /// (Ruby's `super` and `yield`), so no row exists for them.
    pub exclude_method_kinds: Vec<String>,
    /// Drop sends whose name is not an identifier. Operator sends (`+`, `<<`,
    /// `[]`) can never be index-resolved and would crowd real calls out of the
    /// per-file cap.
    pub identifier_names_only: bool,
    /// `recv.foo = x` calls the SETTER `foo=`; the grammar drops the `=` that
    /// is part of the method's real name.
    pub setter_suffix_on_assignment: bool,
    /// Promote a bare identifier that is NOT bound in scope to a receiverless
    /// send. Ruby has no syntax distinguishing `params` (a call) from `orders`
    /// (a local read); only the binding set tells them apart, so without this
    /// the majority of real Ruby call rows are missing.
    pub promote_unbound_identifiers: bool,
    /// Identifiers the language models as something other than a send whatever
    /// the scope says (`__FILE__`).
    pub pseudo_variables: Vec<String>,
    /// Parent node kinds in which an identifier is a name or a binding rather
    /// than a send.
    pub identifier_non_call_parents: Vec<String>,
    /// Node kinds whose target names bind a local.
    pub binding_assignment_nodes: Vec<String>,
    /// Fields on those nodes that hold the binding target. Defaults to `left`;
    /// Ruby's `for` binds on `pattern`, and binding the whole node instead makes
    /// the loop body's sends read as locals for the rest of the method.
    #[serde(deserialize_with = "one_or_many")]
    pub binding_target_fields: FieldNames,
    /// Node kinds that bind every identifier beneath them (parameter lists,
    /// exception variables).
    pub binding_scope_nodes: Vec<String>,
}

/// How import statements are read.
///
/// Node kinds and field names rather than tree-sitter queries: a query per
/// language would be more expressive, but import syntax is exactly where
/// grammars disagree most, and 23 hand-tuned queries is 23 things to get subtly
/// wrong. Node kinds degrade honestly -- a language whose spec names none
/// simply reports no imports instead of reporting wrong ones.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ImportSpec {
    /// Whole-module binds: `import x`, `require "x"`.
    pub module_nodes: Vec<String>,
    /// Named binds: `from m import a`, `import { a } from "m"`.
    pub from_nodes: Vec<String>,
    /// Field on those nodes holding the module path.
    pub module_field: Option<String>,
    /// Marker kinds that make an import (or one of its specifiers) TYPE-ONLY.
    /// `import type { T }` and `import { type T }` reference a type position,
    /// not a value binding, so removing the export does not break them -- and a
    /// symbol row for one seeds a call edge that can never fire.
    pub type_only_markers: Vec<String>,
    /// Node kinds that give a star export a NAME (`export * as ns from "m"`).
    /// Without them the bare `*` reads as an open export set, so the file claims
    /// it re-exports everything AND loses the one name it actually binds.
    pub star_export_alias_nodes: Vec<String>,
    /// Node kinds that bind the module under a single default name
    /// (`import Button from "./m"`). The reference records the specifier as
    /// `default` and collects NO named symbol for it, so treating the binding as
    /// a named import both mislabels the specifier and invents a symbol the
    /// module never exported.
    pub default_binding_nodes: Vec<String>,
    /// Node kinds wrapping an `as` alias.
    pub alias_nodes: Vec<String>,
    /// Field holding the local name an import binds, where the grammar puts it
    /// on the spec node instead of wrapping it. Go's `import_spec` carries the
    /// alias on `name`, so `import f "strings"` binds `f`, not `strings`.
    pub alias_field: Option<String>,
    /// Node kinds that open the export set (`export * from`, `from x import *`).
    pub wildcard_nodes: Vec<String>,
    /// Node kinds where a bare `*` CHILD means the export set is open. Plain
    /// `export * from "m"` produces no dedicated node -- just a `*` token on the
    /// export statement -- so a node-kind list alone cannot see it, and the
    /// index then positively asserts the barrel exports nothing.
    pub star_export_nodes: Vec<String>,
    /// Node kinds holding an explicit export list (`export { a, b }`). Their
    /// identifiers are the module's exported names.
    pub export_clause_nodes: Vec<String>,
    /// Node kinds whose module is implied by the syntax rather than written as a
    /// field. `from __future__ import annotations` parses as its own node kind
    /// with no module path to read, but the module is still `__future__`.
    pub implicit_module: BTreeMap<String, String>,
    /// Specifier kind per module-call name. The reference gives `require`,
    /// `require_relative` and `autoload` three different kinds, and a consumer
    /// reads that kind to tell a library dependency from a sibling file.
    pub module_call_kinds: BTreeMap<String, String>,
    /// Which ARGUMENT holds the module path, per call name. `autoload :Foo,
    /// "foo/bar"` names it second, and reading the whole argument list yields
    /// the literal text `:Foo, 'foo/bar'` as a module.
    pub module_call_arg: BTreeMap<String, usize>,
    /// Module-call names anchored at the importing FILE's directory rather than
    /// resolved by search path. `require_relative` carries no leading dot, so
    /// nothing else distinguishes it.
    pub relative_call_names: Vec<String>,
    /// When a language expresses imports as ordinary calls (Ruby's `require`),
    /// only these method names count. Without it every call in the file reads
    /// as an import -- `puts "x"` becomes a namespace import of `puts` and of
    /// `"x"`, and both land in the file's export set.
    pub module_call_names: Vec<String>,
    /// Node kinds to descend through when collecting imported names. TS nests
    /// specifiers two levels down (`import_statement > import_clause >
    /// named_imports > import_specifier`), so a direct-children scan captures
    /// the whole clause as a single symbol named `{ a, b }`.
    pub descend_nodes: Vec<String>,
}

/// How parameter nodes map to the emitted `kind`.
///
/// Kept as node-kind sets rather than walker code because the only genuinely
/// stateful rule -- "everything after the separator is keyword-only" -- is
/// shared by every language that has the concept, so the walker implements it
/// once and each spec just names its own node kinds.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ParamSpec {
    pub positional: Vec<String>,
    /// Has a default; emitted `optional: true`.
    pub optional: Vec<String>,
    /// `*args` / rest.
    pub rest: Vec<String>,
    /// `**kwargs`.
    pub keyword_rest: Vec<String>,
    /// Explicitly keyword parameters, where the language spells them (Ruby's
    /// `name:`). Distinct from the separator rule, which makes everything AFTER
    /// a marker keyword-only.
    pub keyword: Vec<String>,
    /// A bare `*` or equivalent: parameters after it are keyword-only.
    pub separator: Vec<String>,
    /// Object/array binding patterns, which occupy one positional slot.
    pub destructured: Vec<String>,
    /// Kinds that fill a parameter slot but emit no shape row -- Ruby's block
    /// parameter is counted and not described. Getting this wrong shifts
    /// param_count, which feeds the body-shape norms mining votes on.
    pub counted_only: Vec<String>,
    /// A parameter of this kind is optional only when it carries this field.
    /// Ruby's `kw:` is required; `kw: 2` is not.
    pub optional_when_field: Option<String>,
    /// Trailing punctuation to strip from a parameter's name (`kw:` -> `kw`).
    pub strip_name_suffix: Option<String>,
    /// Literal name a keyword-rest parameter is recorded under (`**`).
    pub keyword_rest_name: Option<String>,
    /// Node kinds that wrap the real parameter pattern to carry a type
    /// annotation. `*args: str` is a `typed_parameter` around a splat, so
    /// classifying on the wrapper alone would call a rest parameter positional.
    pub wrapper: Vec<String>,
    /// The declaration node IS the parameter list: the parameters are its own
    /// direct children rather than living in a container. Swift's
    /// `function_declaration` carries `parameter` children with no list node and
    /// no field, so without this every Swift function reports an arity of 0 into
    /// the body-shape norms mining votes on.
    pub inline: bool,
}

/// Direct-child fallbacks for slots a grammar carries no FIELD for.
///
/// A field lookup is exact and cheap, so it stays the first choice. But several
/// grammars express a slot purely by child position -- Kotlin's
/// `function_declaration` has only a `name` field and holds its parameters in a
/// `function_value_parameters` child, and TypeScript hangs a class's bases off a
/// `class_heritage` child. Without a fallback those languages parse cleanly and
/// report the slot as empty, which reads downstream as a positive claim: a class
/// with no base, a function with no parameters.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ChildKinds {
    pub parameters: Option<String>,
    /// The clause inside the heritage node naming declared interfaces.
    pub implements_item: Option<String>,
    /// The clause INSIDE the heritage node that names the base classes.
    /// TypeScript's `class_heritage` holds an `extends_clause` and an
    /// `implements_clause`, and reading the heritage node's children directly
    /// yields the literal text `extends B`.
    pub superclass_item: Option<String>,
    pub body: Option<String>,
    pub superclass: Option<String>,
    pub call_function: Option<String>,
    pub member_object: Option<String>,
    pub member_property: Option<String>,
}

/// Per-language switches for behavior that is a real language difference rather
/// than a naming difference.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Flags {
    /// Language has a syntactic default export (only TypeScript/JS do).
    pub has_default_export: bool,
    /// Exports are explicit: only a declaration marked with an export keyword
    /// is part of the module's public surface. Without this every top-level
    /// declaration and every imported local is reported as an export, which is
    /// not merely noisy -- it inverts the set, listing what a module keeps
    /// private and omitting what it actually exposes.
    pub explicit_exports: bool,
    /// Language has no export statement, so `default_export_kind` is repurposed
    /// as "the sole top-level definition, when unopposed": one class and no
    /// functions, or one function and no classes. A module with both reports
    /// nothing, because neither is the thing the file is *for*.
    pub sole_definition_default_export: bool,
    /// Language can contain JSX.
    pub supports_jsx: bool,
    /// Marker keyword TEXT that makes a callable `async` -- matched against the
    /// child's source text, not its node kind, because most grammars spell it
    /// as an anonymous token.
    pub async_markers: Vec<String>,
    /// Take an unnamed function's name from the variable it is assigned to.
    /// `export const getUsers = () => {}` is `getUsers`, not `<anonymous>`, and
    /// an anonymous name makes every call inside it uncorrelatable.
    pub name_from_declarator: bool,
    /// The `kind` string for a self/this-receiver call. TypeScript's reference
    /// extractor says `this`; Python's says `self`.
    pub self_kind: Option<String>,
    /// When the receiver is itself a call, name it by that call's method rather
    /// than dropping the site. `1.day.freeze` records `freeze` with receiver
    /// `day` in Ruby's reference extractor; dropping it loses the outer call
    /// entirely.
    pub receiver_from_method: bool,
    /// Node kinds carrying a decorator/annotation.
    pub decorator_nodes: Vec<String>,
    /// Node kinds that HOLD the decorators rather than being one. Java files
    /// its annotations inside a `modifiers` child, so they are a grandchild of
    /// the declaration and a direct-children scan finds none.
    pub decorator_container_nodes: Vec<String>,
    /// Statement wrappers to unwrap when reading top-level kinds. Python's
    /// grammar nests small statements one level deeper than the meaningful node.
    pub unwrap_nodes: Vec<String>,
    /// Node kinds whose assignment TARGETS are module exports, in a language
    /// with no export keyword. Every top-level binding is importable in Python,
    /// so a module's export set is not just its defs and classes.
    pub export_assignment_nodes: Vec<String>,
    /// Top-level compound statements to descend when collecting exports. A
    /// binding inside a `try/except` import fallback, an `if TYPE_CHECKING`, or
    /// a version gate is still module-level. A def or class body is a new scope
    /// and is never descended.
    pub export_descend_nodes: Vec<String>,
    /// Basenames whose module additionally re-exports its sibling submodules,
    /// because `from pkg import submodule` resolves on disk whatever the
    /// package module names.
    pub package_index_names: Vec<String>,
}

/// Which optional record fields this language emits.
///
/// The reference extractors genuinely differ: Ruby's class row carries `kind`
/// and no `bases`, TypeScript omits `decorators` when empty and has no
/// `is_async` at all. Emitting a field the reference never carried is a
/// mismatch even when the value is the obvious default, so this is data rather
/// than a uniform shape. Defaults reproduce Python, the parity target.
#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct EmitSpec {
    pub class_kind: bool,
    pub class_bases: bool,
    pub class_implements: bool,
    pub class_decorators: bool,
    pub class_attrs: bool,
    pub class_qualified: bool,
    pub signature_is_async: bool,
    /// `always` | `never` | `when_present`.
    pub signature_decorators: String,
}

impl Default for EmitSpec {
    fn default() -> Self {
        Self {
            class_kind: false,
            class_bases: true,
            class_implements: false,
            class_decorators: true,
            class_attrs: true,
            class_qualified: false,
            signature_is_async: true,
            signature_decorators: "always".into(),
        }
    }
}

/// One language's complete description.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LanguageSpec {
    pub name: String,
    /// The family a rule may name to cover this language. TSX is its own
    /// grammar but the same language for rule purposes; without this, a rule
    /// declaring `typescript` silently skips every `.tsx` file, which in a
    /// React repo exempts the whole component tree from every rule.
    #[serde(default)]
    pub family: Option<String>,
    pub extensions: Vec<String>,
    /// tree-sitter node kind -> the kind string chameleon's consumers read.
    /// A node mapped to the empty string is recorded as "seen but unnamed" and
    /// dropped from `top_level_node_kinds`, matching the dumpers' handling of
    /// comments.
    #[serde(default)]
    pub top_level_kinds: BTreeMap<String, String>,
    #[serde(default)]
    pub function_nodes: HashSet<String>,
    #[serde(default)]
    pub class_nodes: HashSet<String>,
    /// Nodes that add one to a function's branch count.
    #[serde(default)]
    pub branch_nodes: HashSet<String>,
    /// Nodes that open a nesting level.
    #[serde(default)]
    pub nesting_nodes: HashSet<String>,
    /// Clause kinds a recursive AST nests but this grammar lists flat.
    ///
    /// `if/elif/elif` is three nested `If` nodes in an abstract syntax tree but
    /// one `if_statement` with two sibling `elif_clause`s here. Each chained
    /// clause therefore adds a level that is only released when the enclosing
    /// statement ends, which is what reproduces the recursive depth.
    #[serde(default)]
    pub chained_nesting_nodes: HashSet<String>,
    /// Subtrees never descended into. Comments and string bodies belong here:
    /// descending them is pure cost and invites false call matches.
    #[serde(default)]
    pub skip_subtree_nodes: HashSet<String>,
    /// Second-pass refinement of a top-level kind, keyed by the wrapper node
    /// kind then by its first named child. Python's `expression_statement` is
    /// `Expr`, `Assign`, or `AnnAssign` depending entirely on what it contains,
    /// and the consumers distinguish all three.
    #[serde(default)]
    pub top_level_refine: BTreeMap<String, BTreeMap<String, String>>,
    /// Refinement by the PRESENCE of a field rather than by child kind. An
    /// `assignment` carrying a `type` field is an annotated assignment, which
    /// the consumers record as a distinct kind from a plain one.
    #[serde(default)]
    pub top_level_refine_field: BTreeMap<String, BTreeMap<String, String>>,
    /// Node kind of what `export default` wraps -> the emitted kind string, for
    /// the expression forms `top_level_kinds` does not cover. `export default
    /// foo` is an `Identifier` to the reference, and a bare identifier is not a
    /// top-level statement kind.
    #[serde(default)]
    pub default_export_kinds: BTreeMap<String, String>,
    #[serde(default)]
    pub params: ParamSpec,
    #[serde(default)]
    pub fields: Fields,
    #[serde(default)]
    pub child_kinds: ChildKinds,
    #[serde(default)]
    pub calls: CallSpec,
    #[serde(default)]
    pub imports: ImportSpec,
    #[serde(default)]
    pub flags: Flags,
    #[serde(default)]
    pub emit: EmitSpec,
}

impl LanguageSpec {
    /// Whether a rule naming `declared` applies to this language.
    pub fn matches_language(&self, declared: &str) -> bool {
        self.name == declared || self.family.as_deref() == Some(declared)
    }

    /// Map a tree-sitter node kind to the emitted top-level kind string.
    /// Returns `None` for a node with no mapping (not top-level-meaningful) and
    /// `Some("")` for one deliberately mapped to nothing.
    pub fn top_level_kind(&self, node_kind: &str) -> Option<&str> {
        self.top_level_kinds.get(node_kind).map(String::as_str)
    }

    pub fn is_function(&self, kind: &str) -> bool {
        self.function_nodes.contains(kind)
    }

    pub fn is_class(&self, kind: &str) -> bool {
        self.class_nodes.contains(kind)
    }
}

/// Every spec shipped in the binary, embedded at compile time.
///
/// Embedding rather than reading from disk is deliberate: the codex's design
/// constraint is a single offline binary, and a spec loaded from the filesystem
/// is one more thing that can be missing, stale, or tampered with at runtime.
const EMBEDDED_SPECS: &[(&str, &str)] = &[
    ("python", include_str!("../languages/python.toml")),
    ("typescript", include_str!("../languages/typescript.toml")),
    ("javascript", include_str!("../languages/javascript.toml")),
    ("ruby", include_str!("../languages/ruby.toml")),
    ("go", include_str!("../languages/go.toml")),
    ("rust", include_str!("../languages/rust.toml")),
    ("java", include_str!("../languages/java.toml")),
    ("c", include_str!("../languages/c.toml")),
    ("cpp", include_str!("../languages/cpp.toml")),
    ("csharp", include_str!("../languages/csharp.toml")),
    ("php", include_str!("../languages/php.toml")),
    ("bash", include_str!("../languages/bash.toml")),
    ("scala", include_str!("../languages/scala.toml")),
    ("swift", include_str!("../languages/swift.toml")),
    ("lua", include_str!("../languages/lua.toml")),
    ("elixir", include_str!("../languages/elixir.toml")),
    ("haskell", include_str!("../languages/haskell.toml")),
    ("kotlin", include_str!("../languages/kotlin.toml")),
    ("json", include_str!("../languages/json.toml")),
    ("yaml", include_str!("../languages/yaml.toml")),
    ("toml", include_str!("../languages/toml.toml")),
    ("css", include_str!("../languages/css.toml")),
    ("html", include_str!("../languages/html.toml")),
];

/// Resolve a spec name to its tree-sitter grammar.
///
/// This is the only place a language's name is written next to Rust code, and
/// it exists because a grammar is a linked C symbol, not data. Adding a language
/// is: one TOML file, one line in `EMBEDDED_SPECS`, one arm here, one Cargo
/// dependency. No walker change, no extraction logic, no core edit.
fn grammar_for(name: &str) -> Option<Language> {
    Some(match name {
        "python" => tree_sitter_python::LANGUAGE.into(),
        "typescript" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX.into(),
        "javascript" => tree_sitter_javascript::LANGUAGE.into(),
        "ruby" => tree_sitter_ruby::LANGUAGE.into(),
        "go" => tree_sitter_go::LANGUAGE.into(),
        "rust" => tree_sitter_rust::LANGUAGE.into(),
        "java" => tree_sitter_java::LANGUAGE.into(),
        "c" => tree_sitter_c::LANGUAGE.into(),
        "cpp" => tree_sitter_cpp::LANGUAGE.into(),
        "csharp" => tree_sitter_c_sharp::LANGUAGE.into(),
        "php" => tree_sitter_php::LANGUAGE_PHP.into(),
        "bash" => tree_sitter_bash::LANGUAGE.into(),
        "scala" => tree_sitter_scala::LANGUAGE.into(),
        "swift" => tree_sitter_swift::LANGUAGE.into(),
        "lua" => tree_sitter_lua::LANGUAGE.into(),
        "elixir" => tree_sitter_elixir::LANGUAGE.into(),
        "haskell" => tree_sitter_haskell::LANGUAGE.into(),
        "kotlin" => tree_sitter_kotlin_ng::LANGUAGE.into(),
        "json" => tree_sitter_json::LANGUAGE.into(),
        "yaml" => tree_sitter_yaml::LANGUAGE.into(),
        "toml" => tree_sitter_toml_ng::LANGUAGE.into(),
        "css" => tree_sitter_css::LANGUAGE.into(),
        "html" => tree_sitter_html::LANGUAGE.into(),
        _ => return None,
    })
}

/// A spec bound to its grammar, ready to parse.
pub struct BoundLanguage {
    pub spec: LanguageSpec,
    pub language: Language,
}

impl BoundLanguage {
    /// Every node kind and field name this spec names that its grammar does not
    /// have.
    ///
    /// A spec entry is matched by string against the tree, so a name the grammar
    /// spells differently is not an error -- it is a line that never fires. The
    /// feature it was written for is then silently absent, and the record looks
    /// like an honest "this file has none of those" rather than a bug: three
    /// such entries shipped for four rounds (`guard` for Haskell's `guards`,
    /// `for_each_statement` for C#'s `foreach_statement`,
    /// `anonymous_function_creation_expression` for PHP's `anonymous_function`)
    /// and were each found by hand, one at a time. Asking the grammar's own
    /// symbol table turns the whole class into a test failure.
    ///
    /// Only kinds and fields are checkable this way. A spec also carries TEXT
    /// (`self_names`, `async_markers`, `pseudo_variables`, `module_call_names`)
    /// which the grammar has no table for.
    pub fn spec_problems(&self) -> Vec<String> {
        let s = &self.spec;
        let mut out = Vec::new();

        // A SUPERTYPE is in the symbol table but is never any node's `.kind()`,
        // so a spec entry naming one is as dead as a misspelling while looking
        // present. kotlin-ng's `expression` is one: two spec lines matched it and
        // could never fire.
        let supertypes: HashSet<&str> = self
            .language
            .supertypes()
            .iter()
            .filter_map(|s| self.language.node_kind_for_id(*s))
            .collect();
        // Named OR anonymous: a spec legitimately names an anonymous token when
        // the slot it checks is reached by field rather than by the walk. C#
        // spells `base.g()`'s receiver as a bare `base` token, and matching it
        // is how that call is classified `super`.
        let mut kind = |label: &str, kinds: &mut dyn Iterator<Item = &str>| {
            for k in kinds {
                if supertypes.contains(k) {
                    out.push(format!("{label}: `{k}` is a supertype, never a node kind"));
                } else if self.language.id_for_node_kind(k, true) == 0
                    && self.language.id_for_node_kind(k, false) == 0
                {
                    out.push(format!("{label}: no node kind `{k}`"));
                }
            }
        };

        kind(
            "top_level_kinds",
            &mut s.top_level_kinds.keys().map(|k| &**k),
        );
        kind("function_nodes", &mut s.function_nodes.iter().map(|k| &**k));
        kind("class_nodes", &mut s.class_nodes.iter().map(|k| &**k));
        kind("branch_nodes", &mut s.branch_nodes.iter().map(|k| &**k));
        kind("nesting_nodes", &mut s.nesting_nodes.iter().map(|k| &**k));
        kind(
            "chained_nesting_nodes",
            &mut s.chained_nesting_nodes.iter().map(|k| &**k),
        );
        kind(
            "skip_subtree_nodes",
            &mut s.skip_subtree_nodes.iter().map(|k| &**k),
        );
        kind(
            "top_level_refine",
            &mut s
                .top_level_refine
                .iter()
                .flat_map(|(k, v)| std::iter::once(&**k).chain(v.keys().map(|c| &**c))),
        );
        kind(
            "top_level_refine_field",
            &mut s.top_level_refine_field.keys().map(|k| &**k),
        );
        kind(
            "default_export_kinds",
            &mut s.default_export_kinds.keys().map(|k| &**k),
        );
        let p = &s.params;
        kind(
            "params",
            &mut [
                &p.positional,
                &p.optional,
                &p.rest,
                &p.keyword_rest,
                &p.keyword,
                &p.separator,
                &p.destructured,
                &p.counted_only,
                &p.wrapper,
            ]
            .into_iter()
            .flatten()
            .map(|k| &**k),
        );
        kind(
            "fields.declarator_unwrap",
            &mut s.fields.declarator_unwrap.iter().map(|k| &**k),
        );
        let c = &s.calls;
        kind(
            "calls",
            &mut [
                &c.nodes,
                &c.member_nodes,
                &c.constructor_nodes,
                &c.callee_unwrap,
                &c.member_unwrap,
                &c.super_nodes,
                &c.constant_receiver_nodes,
                &c.receiver_name_nodes,
                &c.exclude_method_kinds,
                &c.identifier_non_call_parents,
                &c.binding_assignment_nodes,
                &c.binding_scope_nodes,
            ]
            .into_iter()
            .flatten()
            .map(|k| &**k),
        );
        let i = &s.imports;
        kind(
            "imports",
            &mut [
                &i.module_nodes,
                &i.from_nodes,
                &i.alias_nodes,
                &i.wildcard_nodes,
                &i.star_export_nodes,
                &i.export_clause_nodes,
                &i.descend_nodes,
                &i.default_binding_nodes,
                &i.type_only_markers,
                &i.star_export_alias_nodes,
            ]
            .into_iter()
            .flatten()
            .map(|k| &**k)
            .chain(i.implicit_module.keys().map(|k| &**k)),
        );
        kind(
            "flags",
            &mut s
                .flags
                .decorator_nodes
                .iter()
                .chain(&s.flags.unwrap_nodes)
                .chain(&s.flags.decorator_container_nodes)
                .chain(&s.flags.export_assignment_nodes)
                .chain(&s.flags.export_descend_nodes)
                .map(|k| &**k),
        );

        let ck = &s.child_kinds;
        kind(
            "child_kinds",
            &mut [
                &ck.parameters,
                &ck.body,
                &ck.superclass,
                &ck.superclass_item,
                &ck.implements_item,
                &ck.call_function,
                &ck.member_object,
                &ck.member_property,
            ]
            .into_iter()
            .flatten()
            .map(|k| &**k),
        );

        let f = &s.fields;
        let mut field = |label: &str, names: &mut dyn Iterator<Item = &str>| {
            for n in names {
                if self.language.field_id_for_name(n).is_none() {
                    out.push(format!("{label}: no field `{n}`"));
                }
            }
        };
        for (label, name) in [
            ("fields.name", &f.name),
            ("fields.class_name", &f.class_name),
            ("fields.return_type", &f.return_type),
            ("fields.param_type", &f.param_type),
            ("calls.name_field", &c.name_field),
            ("imports.module_field", &i.module_field),
            ("imports.alias_field", &i.alias_field),
            ("params.optional_when_field", &p.optional_when_field),
        ] {
            field(label, &mut name.iter().map(|n| &**n));
        }
        for (label, names) in [
            ("fields.declaration", &f.declaration),
            ("fields.parameters", &f.parameters),
            ("fields.body", &f.body),
            ("fields.superclass", &f.superclass),
            ("fields.call_function", &f.call_function),
            ("fields.member_object", &f.member_object),
            ("fields.member_property", &f.member_property),
            ("calls.receiver_field", &c.receiver_field),
            ("calls.binding_target_fields", &c.binding_target_fields),
        ] {
            field(label, &mut names.iter().map(|n| &**n));
        }
        for (node, by_field) in &s.top_level_refine_field {
            let label = format!("top_level_refine_field[{node}]");
            field(&label, &mut by_field.keys().map(|n| &**n));
        }

        out.sort();
        out.dedup();
        out
    }
}

/// Every language the binary can parse, indexed by name and by extension.
pub struct Registry {
    by_name: HashMap<String, BoundLanguage>,
    by_extension: HashMap<String, String>,
}

impl Registry {
    /// Load every embedded spec and bind its grammar.
    ///
    /// A spec whose grammar is missing is a build-time mistake, so this returns
    /// an error rather than skipping it quietly: a language that silently
    /// vanishes from the registry would show up much later as an inexplicable
    /// `unsupported_language` on a file the user expects to work.
    pub fn load() -> Result<Self> {
        let mut by_name = HashMap::new();
        let mut by_extension = HashMap::new();

        for (name, toml_src) in EMBEDDED_SPECS {
            let spec: LanguageSpec = toml::from_str(toml_src)
                .map_err(|e| anyhow!("language spec {name} is malformed: {e}"))?;
            let language = grammar_for(&spec.name)
                .ok_or_else(|| anyhow!("no grammar linked for language {}", spec.name))?;

            for ext in &spec.extensions {
                by_extension.insert(ext.to_ascii_lowercase(), spec.name.clone());
            }
            by_name.insert(spec.name.clone(), BoundLanguage { spec, language });
        }

        // TSX shares the TypeScript spec but needs the TSX grammar: the two
        // grammars differ in how they resolve `<T>` (type assertion vs element),
        // so parsing .tsx with the .ts grammar produces a wrong tree, not a
        // slightly-off one.
        if let Some(ts) = by_name.get("typescript") {
            let mut tsx_spec = ts.spec.clone();
            tsx_spec.name = "tsx".into();
            tsx_spec.family = Some("typescript".into());
            tsx_spec.extensions = vec![".tsx".into()];
            tsx_spec.flags.supports_jsx = true;
            let language = grammar_for("tsx").ok_or_else(|| anyhow!("no tsx grammar"))?;
            by_extension.insert(".tsx".into(), "tsx".into());
            by_name.insert(
                "tsx".into(),
                BoundLanguage {
                    spec: tsx_spec,
                    language,
                },
            );
        }

        Ok(Self {
            by_name,
            by_extension,
        })
    }

    pub fn by_name(&self, name: &str) -> Option<&BoundLanguage> {
        self.by_name.get(name)
    }

    /// Resolve a path to a language by its extension.
    ///
    /// Extension-only, deliberately: content sniffing is a guess, and a wrong
    /// guess silently produces a plausible-looking tree in the wrong grammar.
    pub fn for_path(&self, path: &str) -> Option<&BoundLanguage> {
        let lower = path.to_ascii_lowercase();
        // Longest extension first so `.d.ts` beats `.ts` where a spec declares both.
        let mut best: Option<(&String, usize)> = None;
        for (ext, name) in &self.by_extension {
            if lower.ends_with(ext) && best.is_none_or(|(_, len)| ext.len() > len) {
                best = Some((name, ext.len()));
            }
        }
        best.and_then(|(name, _)| self.by_name.get(name))
    }

    /// Every extension the engine can parse, lower-cased and dot-prefixed.
    pub fn extensions(&self) -> Vec<&str> {
        let mut out: Vec<&str> = self.by_extension.keys().map(String::as_str).collect();
        out.sort_unstable();
        out
    }

    pub fn names(&self) -> Vec<&str> {
        let mut out: Vec<&str> = self.by_name.keys().map(String::as_str).collect();
        out.sort_unstable();
        out
    }

    pub fn len(&self) -> usize {
        self.by_name.len()
    }

    pub fn is_empty(&self) -> bool {
        self.by_name.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tree_sitter::Parser;

    #[test]
    fn every_shipped_spec_parses_and_binds_a_grammar() {
        let reg = Registry::load().expect("registry loads");
        assert!(
            reg.len() >= 23,
            "expected the full language set, got {}",
            reg.len()
        );
    }

    /// The ABI-compatibility guarantee, asserted rather than assumed. The core
    /// accepts 13..=15; the shipped grammars span 14 and 15. A future grammar
    /// bump that lands outside the window fails here instead of at runtime.
    #[test]
    fn every_shipped_grammar_loads_and_parses() {
        let reg = Registry::load().unwrap();
        for name in reg.names() {
            let bound = reg.by_name(name).unwrap();
            let abi = bound.language.abi_version();
            assert!(
                (13..=15).contains(&abi),
                "{name}: ABI {abi} is outside the range tree-sitter 0.26 accepts"
            );
            let mut parser = Parser::new();
            parser
                .set_language(&bound.language)
                .unwrap_or_else(|e| panic!("{name}: set_language failed: {e}"));
            assert!(
                parser.parse("", None).is_some(),
                "{name}: failed to parse empty input"
            );
        }
    }

    /// Every spec name checked against its own grammar's symbol table. A spec
    /// entry the grammar does not have is a feature that silently does nothing,
    /// and it reads downstream as an honest absence rather than a bug.
    #[test]
    fn no_spec_names_a_kind_or_field_its_grammar_lacks() {
        let reg = Registry::load().unwrap();
        let mut bad = Vec::new();
        for name in reg.names() {
            for p in reg.by_name(name).unwrap().spec_problems() {
                bad.push(format!("{name}: {p}"));
            }
        }
        assert!(bad.is_empty(), "dead spec entries:\n  {}", bad.join("\n  "));
    }

    #[test]
    fn extension_lookup_resolves_the_expected_language() {
        let reg = Registry::load().unwrap();
        for (path, expected) in [
            ("/a/b/c.py", "python"),
            ("/a/b/c.rb", "ruby"),
            ("/a/b/c.ts", "typescript"),
            ("/a/b/c.tsx", "tsx"),
            ("/a/b/c.go", "go"),
            ("/a/b/c.rs", "rust"),
            ("/a/b/c.kt", "kotlin"),
        ] {
            let got = reg
                .for_path(path)
                .unwrap_or_else(|| panic!("{path} did not resolve"));
            assert_eq!(got.spec.name, expected, "for {path}");
        }
        assert!(reg.for_path("/a/b/c.unknownext").is_none());
    }

    #[test]
    fn tsx_and_typescript_are_distinct_grammars() {
        let reg = Registry::load().unwrap();
        let ts = reg.by_name("typescript").unwrap();
        let tsx = reg.by_name("tsx").unwrap();
        // The TSX grammar parses a JSX element; the TS grammar reads `<div/>`
        // as a type assertion and errors on it.
        let mut p = Parser::new();
        p.set_language(&tsx.language).unwrap();
        let tree = p.parse("const C = () => <div/>;", None).unwrap();
        assert!(
            !tree.root_node().has_error(),
            "tsx grammar should accept JSX"
        );

        let mut p2 = Parser::new();
        p2.set_language(&ts.language).unwrap();
        let tree2 = p2.parse("const C = () => <div/>;", None).unwrap();
        assert!(
            tree2.root_node().has_error(),
            "ts grammar should reject JSX"
        );
    }
}
