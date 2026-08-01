//! chromatophore -- a universal code-convention engine.
//!
//! The organ that actually changes a chameleon's colour is the chromatophore.
//! This crate is the equivalent layer for the `chameleon` plugin: it does the
//! compute-heavy, language-bound work -- parse, extract, index, mine, match --
//! and leaves policy, trust, hooks, and artifact lifecycle to the host.
//!
//! The design constraint, from the god-mode codex, is one parse engine over
//! many grammars rather than one native compiler integration per language.
//! Chameleon supports three languages and pays roughly 2,500 lines of
//! hand-written per-language Python for them; here a language is a TOML file.
//!
//! Layers, bottom to top:
//!
//! - [`core`] -- the `ParsedFile` wire contract the host reads.
//! - [`lang`] -- declarative language specs bound to tree-sitter grammars.
//! - [`extract`] -- one iterative walk producing a `ParsedFile`.
//! - [`index`] -- symbols, imports, calls, and blast radius across files.
//! - [`mine`] -- archetype clustering, convention voting, witness selection.
//! - [`rules`] -- the universal rule schema and its evaluator.

pub mod core;
pub mod extract;
pub mod index;
pub mod lang;
pub mod mine;
pub mod rules;

pub use core::{Limits, ParseError, ParsedFile, Record};
pub use lang::Registry;

/// Parse one file from disk, resolving its language by extension.
///
/// Returns a `Record` rather than a `Result` because a per-file failure is
/// data, not an error: the caller is usually streaming a corpus, and one
/// unreadable file must cost that file and nothing else.
pub fn parse_path(path: &std::path::Path, registry: &Registry, limits: &Limits) -> Record {
    let display = path.to_string_lossy().to_string();

    // A symlink is refused rather than followed: the corpus is a list of paths
    // the engine did not choose, and following links lets one escape the tree
    // the host meant to analyze.
    match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_symlink() => {
            return Record::Failed(ParseError::new(display, "symlink_refused"));
        }
        Ok(meta) if meta.len() > limits.max_file_bytes => {
            return Record::Failed(
                ParseError::new(display, "file_too_large").with_size(meta.len()),
            );
        }
        Ok(_) => {}
        Err(e) => {
            return Record::Failed(
                ParseError::new(display, "read_error").with_message(e.to_string()),
            );
        }
    }

    let Some(bound) = registry.for_path(&display) else {
        return Record::Failed(ParseError::new(display, "unsupported_language"));
    };

    let source = match std::fs::read(path) {
        Ok(s) => s,
        Err(e) => {
            return Record::Failed(
                ParseError::new(display, "read_error").with_message(e.to_string()),
            );
        }
    };

    match extract::extract(&display, &source, bound, limits) {
        Ok(parsed) => Record::Parsed(Box::new(parsed)),
        Err(e) => Record::Failed(e),
    }
}
