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

use std::collections::BTreeSet;

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
        // Anything that is not a regular file is refused BEFORE the size check,
        // because the size check does not stop it: a FIFO reports a length of 0,
        // and `fs::read` then blocks in open(2) until a writer appears -- POSIX
        // semantics, and Rust passes no O_NONBLOCK. Under `dump` that stalls the
        // whole batch and the caller eventually kills the child, losing the
        // corpus. A character device is worse: `fs::read` grows its buffer from a
        // zero size hint and does not stop.
        Ok(meta) if !meta.file_type().is_file() => {
            return Record::Failed(ParseError::new(display, "not_a_regular_file"));
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
        Ok(mut parsed) => {
            add_package_siblings(path, bound, &mut parsed);
            Record::Parsed(Box::new(parsed))
        }
        Err(e) => Record::Failed(e),
    }
}

/// A package module re-exports its sibling submodules.
///
/// `from pkg import submodule` resolves to `pkg/submodule.py` on disk whatever
/// the package module names, so a phantom-symbol check run against the module's
/// own text alone reports every submodule import as a hallucinated binding.
/// This lives here rather than in `extract` because it is the one export rule
/// that reads the filesystem, and the extractor is pure by design: path and
/// bytes in, record out.
fn add_package_siblings(
    path: &std::path::Path,
    bound: &lang::BoundLanguage,
    parsed: &mut ParsedFile,
) {
    let names = &bound.spec.flags.package_index_names;
    if names.is_empty() {
        return;
    }
    let base = path.file_name().map(|n| n.to_string_lossy().to_string());
    if !base.is_some_and(|b| names.iter().any(|n| n == &b)) {
        return;
    }
    // A PEP 562 module-level `__getattr__` exports names that cannot be
    // enumerated, so adding the enumerable siblings would flip a sparse package
    // module to a CLOSED set that false-flags every lazily-exported name.
    if parsed.named_export_names.iter().any(|n| n == "__getattr__") {
        parsed.export_set_open = true;
    }
    let Some(dir) = path.parent() else { return };
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut found: BTreeSet<String> = parsed.named_export_names.iter().cloned().collect();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with("__") {
            continue;
        }
        if let Some(stem) = name
            .strip_suffix(".py")
            .or_else(|| name.strip_suffix(".pyi"))
        {
            found.insert(stem.to_string());
        } else if name.ends_with(".so") || name.ends_with(".pyd") {
            // A compiled extension carries platform and ABI tags after the
            // module name (`_speedups.cpython-311-darwin.so`).
            found.insert(name.split('.').next().unwrap_or(&name).to_string());
        } else if names
            .iter()
            .any(|idx| entry.path().join(idx.as_str()).is_file())
        {
            found.insert(name);
        }
    }
    parsed.named_export_names = found.into_iter().collect();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    /// Anything that is not a regular file must be refused as DATA, not read.
    /// A FIFO reports a length of 0, so the size guard passes it, and `fs::read`
    /// then blocks in open(2) until a writer appears -- stalling the whole batch
    /// under `dump` and costing the caller the corpus, not the file.
    #[test]
    #[cfg(unix)]
    fn a_non_regular_file_is_refused_rather_than_opened() {
        let dir = std::env::temp_dir().join(format!("chromo-fifo-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let fifo = dir.join("pipe.py");
        let c = std::ffi::CString::new(fifo.to_string_lossy().as_bytes()).unwrap();
        // SAFETY: a libc call with a valid NUL-terminated path.
        let rc = unsafe { libc_mkfifo(c.as_ptr()) };
        assert_eq!(rc, 0, "mkfifo failed");

        let reg = Registry::load().unwrap();
        let rec = parse_path(&fifo, &reg, &Limits::default());
        match rec {
            Record::Failed(e) => assert_eq!(e.error, "not_a_regular_file"),
            Record::Parsed(_) => panic!("a FIFO must never be parsed"),
        }
        // A directory reaching parse_path is the same class.
        match parse_path(&dir.join("sub.py"), &reg, &Limits::default()) {
            Record::Failed(e) => assert_eq!(e.error, "read_error"),
            Record::Parsed(_) => panic!("a missing path is not parseable"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    extern "C" {
        #[link_name = "mkfifo"]
        fn libc_mkfifo(path: *const std::os::raw::c_char) -> std::os::raw::c_int;
    }

    /// `from pkg import submodule` resolves on disk whatever the package module
    /// names, so a package's export set includes its siblings -- and a PEP 562
    /// `__getattr__` opens the set rather than closing it around a sparse one.
    #[test]
    fn a_package_module_re_exports_its_siblings() {
        let dir = std::env::temp_dir().join(format!("chromo-pkg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("nested")).unwrap();
        for (name, body) in [
            ("__init__.py", "VERSION = \"1\"\n"),
            ("sibling.py", "def go():\n    pass\n"),
            ("nested/__init__.py", ""),
        ] {
            let mut f = std::fs::File::create(dir.join(name)).unwrap();
            f.write_all(body.as_bytes()).unwrap();
        }
        let reg = Registry::load().unwrap();
        let Record::Parsed(pf) = parse_path(&dir.join("__init__.py"), &reg, &Limits::default())
        else {
            panic!("should parse")
        };
        let mut names = pf.named_export_names.clone();
        names.sort();
        assert_eq!(names, vec!["VERSION", "nested", "sibling"]);
        assert!(!pf.export_set_open);

        let mut f = std::fs::File::create(dir.join("__init__.py")).unwrap();
        f.write_all(b"def __getattr__(n):\n    pass\n").unwrap();
        let Record::Parsed(lazy) = parse_path(&dir.join("__init__.py"), &reg, &Limits::default())
        else {
            panic!("should parse")
        };
        assert!(
            lazy.export_set_open,
            "a lazily-exporting package must not close its set around the siblings"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
