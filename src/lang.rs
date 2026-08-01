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
use serde::Deserialize;
use tree_sitter::Language;

/// Field names the walker asks a node for. Every one is optional: a spec that
/// omits `name_field` simply yields unnamed declarations rather than failing.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Fields {
    /// Field holding a declaration's identifier.
    pub name: Option<String>,
    /// Field holding a callable's parameter list.
    pub parameters: Option<String>,
    /// Field holding a declaration's body.
    pub body: Option<String>,
    /// Field holding a declared return type.
    pub return_type: Option<String>,
    /// Field holding a class's superclass/base list.
    pub superclass: Option<String>,
    /// Field holding a call's callee expression.
    pub call_function: Option<String>,
    /// On a member expression: the receiver, then the property.
    pub member_object: Option<String>,
    pub member_property: Option<String>,
    /// Field holding a parameter's type annotation.
    pub param_type: Option<String>,
}

/// How calls are recognized and classified.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CallSpec {
    /// Node kinds that are a call.
    pub nodes: Vec<String>,
    /// Node kinds that are a member access (`a.b`), used to split receiver/name.
    pub member_nodes: Vec<String>,
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
    pub receiver_field: Option<String>,
    pub name_field: Option<String>,
    /// True when call rows should carry a `nesting` list (Ruby constant dispatch).
    pub carries_nesting: bool,
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
    /// Field holding the imported name.
    pub name_field: Option<String>,
    /// Node kinds wrapping an `as` alias.
    pub alias_nodes: Vec<String>,
    /// Node kinds that open the export set (`export * from`, `from x import *`).
    pub wildcard_nodes: Vec<String>,
    /// Node kinds whose module is implied by the syntax rather than written as a
    /// field. `from __future__ import annotations` parses as its own node kind
    /// with no module path to read, but the module is still `__future__`.
    pub implicit_module: BTreeMap<String, String>,
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
    /// A bare `*` or equivalent: parameters after it are keyword-only.
    pub separator: Vec<String>,
    /// Object/array binding patterns, which occupy one positional slot.
    pub destructured: Vec<String>,
    /// Node kinds that wrap the real parameter pattern to carry a type
    /// annotation. `*args: str` is a `typed_parameter` around a splat, so
    /// classifying on the wrapper alone would call a rest parameter positional.
    pub wrapper: Vec<String>,
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
    /// Node kinds that mark a callable `async`.
    pub async_markers: Vec<String>,
    /// Node kinds carrying a decorator/annotation.
    pub decorator_nodes: Vec<String>,
    /// Statement wrappers to unwrap when reading top-level kinds. Python's
    /// grammar nests small statements one level deeper than the meaningful node.
    pub unwrap_nodes: Vec<String>,
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
    #[serde(default)]
    pub params: ParamSpec,
    #[serde(default)]
    pub fields: Fields,
    #[serde(default)]
    pub calls: CallSpec,
    #[serde(default)]
    pub imports: ImportSpec,
    #[serde(default)]
    pub flags: Flags,
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
