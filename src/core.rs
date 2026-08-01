//! The `ParsedFile` wire contract.
//!
//! These types are the engine's output shape and the reason it can stand in for
//! chameleon's own AST dumpers. The field names, the nesting, and the
//! omit-vs-null distinctions are all copied from a live capture of
//! `plugin/scripts/libcst_dump.py` output, not from documentation: a field that
//! is absent there is `Option` + `skip_serializing_if` here, and a field that
//! comes back as an explicit `null` there stays an always-serialized `Option`.
//! Getting that split wrong is invisible in a smoke test and fatal in the
//! differential harness, which compares serialized values.

use serde::{Deserialize, Serialize};

/// One parameter of a callable.
///
/// `type` carries the declared annotation text where the language has one; it is
/// omitted entirely rather than nulled when a parameter is unannotated.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Param {
    pub name: String,
    pub optional: bool,
    /// `positional` | `optional` | `rest` | `keyword` | `keyword_rest` |
    /// `destructured` -- each language uses the subset its own syntax has.
    pub kind: String,
    #[serde(rename = "type", skip_serializing_if = "Option::is_none", default)]
    pub type_annotation: Option<String>,
}

/// Body-shape metrics for one function-like node.
///
/// The six keys are identical across every chameleon extractor, which is what
/// lets the downstream norms compare a Go function against a Ruby one. Nested
/// closures are measured as their own frame, not folded into the parent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FunctionScope {
    pub start_line: usize,
    pub end_line: usize,
    /// `end - start + 1`, or null when either bound is unknown.
    pub line_span: Option<usize>,
    pub max_depth: usize,
    pub branch_count: usize,
    pub param_count: usize,
}

/// A callable header: everything about a function that is not its body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallableSignature {
    pub name: String,
    /// `function` | `method` | `constructor` | `getter` | `setter` |
    /// `staticmethod` | `classmethod` | `singleton_method`.
    pub kind: String,
    pub params: Vec<Param>,
    pub is_default_export: bool,
    pub is_async: bool,
    /// Null rather than omitted: the dumpers emit an explicit null for a plain
    /// function, and the harness compares the serialized value.
    pub enclosing_class: Option<String>,
    /// Nesting-qualified form (`Api.FooController`); equals `enclosing_class`
    /// for a top-level class.
    pub enclosing_class_path: Option<String>,
    pub base_class: Option<String>,
    pub decorators: Vec<String>,
    pub start_line: usize,
    pub end_line: usize,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub return_type: Option<String>,
}

/// A class/module declaration's shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClassShape {
    pub name: String,
    pub start_line: usize,
    /// Every base, in source order.
    pub bases: Vec<String>,
    /// The first base, rendered for display. Multiple inheritance collapses to
    /// `First (+N more)`, matching the dumpers, so a consumer reading a single
    /// string is never silently told the class has one parent when it has four.
    pub extends: Option<String>,
    pub decorators: Vec<String>,
    /// Names (never values) of direct class-body assignments. Config attributes
    /// like `permission_classes` are a role signal; their values are not.
    pub class_attrs: Vec<String>,
}

/// One recorded call edge, before cross-file resolution.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallSite {
    pub name: String,
    pub receiver: Option<String>,
    /// `bare` | `member` | `self` | `new` | `super` | `constant`.
    pub kind: String,
    pub line: usize,
    /// Innermost enclosing callable, or the literal `<module>` at top level.
    pub caller: String,
    /// Enclosing module/class segments; only Ruby-style constant dispatch
    /// carries it, so it is omitted everywhere else.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub nesting: Option<Vec<String>>,
}

/// A named import binding: `from m import a as b` -> name=a, local=b.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportSymbol {
    pub name: String,
    pub local: String,
    pub module: String,
    pub line: usize,
}

/// A whole-module bind: `import x as y` / `import * as y from "x"`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NamespaceImport {
    pub alias: String,
    pub module: String,
    pub line: usize,
}

/// The full record for one successfully parsed file.
///
/// Serialized flat, one JSON object per line. The docs describe several of these
/// as living under an `extras` key; that is the Python-side mapping, and the
/// wire shape is flat.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParsedFile {
    pub path: String,
    pub content_first_200_bytes: String,
    /// Each language's own vocabulary (`FunctionDef`, `DefNode`,
    /// `FunctionDeclaration`), translated at emission from tree-sitter's node
    /// names so downstream keeps reading what it already reads.
    pub top_level_node_kinds: Vec<String>,
    pub default_export_kind: Option<String>,
    pub named_export_count: usize,
    pub named_export_names: Vec<String>,
    /// True when the export set cannot be enumerated (`export * from`,
    /// `from x import *`). A consumer must skip symbol-existence checks then.
    pub export_set_open: bool,
    /// `[module, kind]` pairs; kind is `named` | `namespace` | `default`.
    pub import_specifiers: Vec<(String, String)>,
    pub import_symbols: Vec<ImportSymbol>,
    pub namespace_imports: Vec<NamespaceImport>,
    pub has_jsx: bool,
    /// 0 clean, 1 recovered. tree-sitter is error-recovering and exposes no
    /// graded diagnostic count, so this is deliberately binary.
    pub parse_diagnostics_count: usize,
    pub function_scopes: Vec<FunctionScope>,
    pub callable_signatures: Vec<CallableSignature>,
    pub class_shapes: Vec<ClassShape>,
    pub call_sites: Vec<CallSite>,
    /// The true count even when `call_sites` was capped -- an honest total is
    /// what keeps a capped answer distinguishable from a verified zero.
    pub call_sites_total: usize,
    pub call_sites_truncated: bool,
}

/// A file the engine refused or failed to parse.
///
/// Emitted in place of a `ParsedFile` on the same stream. Per-file failure is
/// data, never an exception: one unreadable file must not cost the corpus.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParseError {
    pub path: String,
    /// `read_error` | `symlink_refused` | `file_too_large` |
    /// `ast_node_ceiling_exceeded` | `parse_error` | `walk_error` |
    /// `unsupported_language` | `extractor_crash`.
    pub error: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub size: Option<u64>,
}

impl ParseError {
    pub fn new(path: impl Into<String>, error: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            error: error.into(),
            message: None,
            size: None,
        }
    }

    pub fn with_message(mut self, message: impl Into<String>) -> Self {
        self.message = Some(message.into());
        self
    }

    pub fn with_size(mut self, size: u64) -> Self {
        self.size = Some(size);
        self
    }
}

/// One line of the NDJSON stream: a record or a refusal, never both.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Record {
    Parsed(Box<ParsedFile>),
    Failed(ParseError),
}

impl Record {
    pub fn path(&self) -> &str {
        match self {
            Record::Parsed(p) => &p.path,
            Record::Failed(e) => &e.path,
        }
    }
}

/// Per-file ceilings, mirroring the caps compiled into chameleon's dumpers.
///
/// `max_file_bytes` is the real denial-of-service bound; the node ceiling exists
/// for the pathological-but-small file (50k nested parens parses fine and
/// produces a 50k-deep tree).
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub max_file_bytes: u64,
    pub max_ast_nodes: usize,
    pub max_callable_signatures: usize,
    pub max_call_sites: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_file_bytes: 1_000_000,
            max_ast_nodes: 165_000,
            max_callable_signatures: 200,
            max_call_sites: 10_000,
        }
    }
}

/// xxhash64 of the file bytes, hex. Chameleon computes this host-side today;
/// the engine has the bytes in hand already, so it emits it and saves the reread.
pub fn sha_hint(bytes: &[u8]) -> String {
    format!("{:016x}", twox_hash::XxHash64::oneshot(0, bytes))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn return_type_is_omitted_not_nulled() {
        let sig = CallableSignature {
            name: "f".into(),
            kind: "function".into(),
            params: vec![],
            is_default_export: false,
            is_async: false,
            enclosing_class: None,
            enclosing_class_path: None,
            base_class: None,
            decorators: vec![],
            start_line: 1,
            end_line: 2,
            return_type: None,
        };
        let json = serde_json::to_string(&sig).unwrap();
        assert!(
            !json.contains("return_type"),
            "absent return_type must not serialize: {json}"
        );
        // enclosing_class is the opposite case: an explicit null, always present.
        assert!(json.contains("\"enclosing_class\":null"), "got {json}");
    }

    #[test]
    fn param_type_is_omitted_when_unannotated() {
        let p = Param {
            name: "a".into(),
            optional: false,
            kind: "positional".into(),
            type_annotation: None,
        };
        assert_eq!(
            serde_json::to_string(&p).unwrap(),
            r#"{"name":"a","optional":false,"kind":"positional"}"#
        );
    }

    #[test]
    fn error_record_carries_only_what_it_has() {
        let e = ParseError::new("/a.py", "file_too_large").with_size(2_000_000);
        let json = serde_json::to_string(&e).unwrap();
        assert_eq!(
            json,
            r#"{"path":"/a.py","error":"file_too_large","size":2000000}"#
        );
    }

    #[test]
    fn sha_hint_is_stable_and_hex() {
        let h = sha_hint(b"hello");
        assert_eq!(h.len(), 16);
        assert_eq!(h, sha_hint(b"hello"));
        assert_ne!(h, sha_hint(b"hellp"));
    }
}
