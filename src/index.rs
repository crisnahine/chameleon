//! Cross-file code intelligence: symbols, imports, calls, blast radius.
//!
//! Parsing tells you what one file says; this tells you what the repo means.
//! The design posture is copied deliberately from chameleon, because it is the
//! right one: **precision over recall**. A call site whose target cannot be
//! resolved deterministically records no edge at all rather than a name-matched
//! guess. Name-only repo-wide matching is where the false positives live, and a
//! wrong edge is worse than a missing one -- it makes a blast radius lie, and a
//! blast radius is consulted precisely when someone is about to rename or delete
//! something.
//!
//! The corollary is stated everywhere it can be: **an empty caller set is not
//! evidence of dead code.** Dynamic dispatch, reflection, and calls through an
//! instance are all invisible here.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde::{Deserialize, Serialize};

use crate::core::ParsedFile;

/// How an edge was resolved. A closed set: anything that does not meet one
/// grade's full conditions is dropped, never downgraded to a fuzzy match.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Grade {
    /// Caller and callee are defined in the same file.
    SameFile,
    /// The callee name is bound by an import in the caller's file, and the
    /// target's export set is closed and contains it.
    Import,
    /// `from pkg import mod; mod.func()` -- a module object attribute call.
    ModuleAttribute,
}

/// One recorded caller of a symbol.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallerRow {
    pub path: String,
    pub caller: String,
    pub line: usize,
    pub grade: Grade,
}

/// Everything the engine knows about one symbol's callers.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CalleeEntry {
    pub callers: Vec<CallerRow>,
    /// The true count, even when `callers` was capped.
    pub total: usize,
    pub truncated: bool,
}

/// The whole cross-file picture, built once from a parsed corpus.
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct CodeIndex {
    /// file -> symbol -> who calls it.
    pub callees: BTreeMap<String, BTreeMap<String, CalleeEntry>>,
    /// file -> the names it exports, and whether the set is enumerable.
    pub exports: BTreeMap<String, ExportSet>,
    /// target file -> importing files.
    pub importers: BTreeMap<String, BTreeSet<String>>,
    /// symbol name -> defining (file, line, kind).
    pub symbols: BTreeMap<String, Vec<SymbolDef>>,
    /// Files whose call sites hit the per-file cap, so a zero here is not a
    /// verified zero.
    pub capped_files: BTreeSet<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ExportSet {
    pub names: BTreeSet<String>,
    /// True when the set cannot be enumerated (`export *`, `import *`).
    pub open: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SymbolDef {
    pub file: String,
    pub line: usize,
    pub kind: String,
    pub signature: String,
}

/// Per-symbol caller cap. The `total` field keeps the real number.
const MAX_CALLERS_PER_CALLEE: usize = 100;

/// What one import bound: the target file, the name the TARGET exports, and
/// whether the local names a MODULE rather than a value.
type ImportBinding = (String, String, bool);

/// (specifier, importer dir, importer extension, imported name). Everything
/// `resolve_module` branches on, and nothing else.
type MemoKey = (String, String, String, String);

/// A resolved target, and whether the local it binds names a module.
type Resolution = (String, bool);

impl CodeIndex {
    /// Build the index from an already-parsed corpus.
    ///
    /// Conservative form: no package hint, so a fully-qualified specifier only
    /// resolves if the corpus paths actually carry the package prefix.
    pub fn build(files: &[ParsedFile]) -> Self {
        Self::build_with_package(files, "")
    }

    /// Build with the corpus root's package name.
    ///
    /// The hint exists for one case: the caller pointed at the INSIDE of a
    /// package, so `mypkg.util` has to find a file stored as `util.py`. That
    /// prefix is the only one droppable. Without the hint, dropping any prefix
    /// lets `django.db` fall through to a repo file named `db.py` and fabricate
    /// an edge to it -- exactly the wrong-binding this module promises not to
    /// make.
    pub fn build_with_package(files: &[ParsedFile], package: &str) -> Self {
        let mut idx = CodeIndex::default();

        // Pass 1: what each file defines and exports, plus the symbol table.
        let mut module_callables: HashMap<&str, BTreeSet<&str>> = HashMap::new();
        for pf in files {
            let mut defined = BTreeSet::new();
            for sig in &pf.callable_signatures {
                defined.insert(sig.name.as_str());
                idx.symbols
                    .entry(sig.name.clone())
                    .or_default()
                    .push(SymbolDef {
                        file: pf.path.clone(),
                        line: sig.start_line,
                        kind: sig.kind.clone(),
                        signature: render_signature(sig),
                    });
            }
            for cls in &pf.class_shapes {
                idx.symbols
                    .entry(cls.name.clone())
                    .or_default()
                    .push(SymbolDef {
                        file: pf.path.clone(),
                        line: cls.start_line,
                        kind: "class".into(),
                        signature: cls.name.clone(),
                    });
            }
            module_callables.insert(pf.path.as_str(), defined);

            idx.exports.insert(
                pf.path.clone(),
                ExportSet {
                    names: pf.named_export_names.iter().cloned().collect(),
                    open: pf.export_set_open,
                },
            );
            if pf.call_sites_truncated {
                idx.capped_files.insert(pf.path.clone());
            }
        }

        // Import graph. Module specifiers are resolved by suffix match against
        // real corpus paths; a specifier that resolves to zero or many files is
        // dropped rather than guessed at.
        let paths: Vec<&str> = files.iter().map(|f| f.path.as_str()).collect();
        // Either the caller's hint, or a leading segment every path shares.
        let root_name = if package.is_empty() {
            common_root_name(&paths)
        } else {
            package.to_string()
        };
        let module_index = ModuleIndex::build(&paths);
        // A repo imports the same specifier from many files; the answer depends
        // only on the specifier, the importer's DIRECTORY, and the submodule
        // hint, so it is cached on exactly those.
        // The importer's EXTENSION is part of the key, not decoration:
        // resolve_module branches on ModuleConventions::for_path, so `src/app.py`
        // and `src/app.ts` importing `.` want different answers and would
        // otherwise share a slot -- the TS file getting the Python module.
        let mut memo: HashMap<MemoKey, Option<Resolution>> = HashMap::new();
        let mut import_targets: HashMap<&str, HashMap<String, ImportBinding>> = HashMap::new();
        for pf in files {
            // The value carries the EXPORTED name, not just the target. Keying
            // on the local alone discards which symbol was imported, so
            // `from lib import real as alias` graded `alias()` against
            // `lib::alias` -- a different function that merely happens to exist.
            // That is a wrong edge, not a missing one.
            // The third element is "this local names a MODULE, not a value".
            // Recorded where the resolution happens rather than re-derived from
            // the path afterwards: a basename comparison gets it wrong in BOTH
            // directions -- a package resolves to its `__init__`, whose basename
            // is never the module name, and a named value import of `util` from
            // `util.ts` looks exactly like a module bind.
            let mut local_to_target: HashMap<String, ImportBinding> = HashMap::new();
            let dir_of = |p: &str| {
                p.rsplit_once('/')
                    .map(|(d, _)| d.to_string())
                    .unwrap_or_default()
            };
            let ext_of = |p: &str| {
                p.rsplit_once('.')
                    .map(|(_, e)| e.to_string())
                    .unwrap_or_default()
            };
            for sym in &pf.import_symbols {
                let key = (
                    sym.module.clone(),
                    dir_of(&pf.path),
                    ext_of(&pf.path),
                    sym.name.clone(),
                );
                let resolved = memo
                    .entry(key)
                    .or_insert_with(|| {
                        resolve_module(
                            &sym.module,
                            &module_index,
                            &pf.path,
                            &root_name,
                            Some(&sym.name),
                        )
                    })
                    .clone();
                if let Some((target, is_submodule)) = resolved {
                    idx.importers
                        .entry(target.clone())
                        .or_default()
                        .insert(pf.path.clone());
                    local_to_target
                        .insert(sym.local.clone(), (target, sym.name.clone(), is_submodule));
                }
            }
            for ns in &pf.namespace_imports {
                let key = (
                    ns.module.clone(),
                    dir_of(&pf.path),
                    ext_of(&pf.path),
                    String::new(),
                );
                let resolved = memo
                    .entry(key)
                    .or_insert_with(|| {
                        resolve_module(&ns.module, &module_index, &pf.path, &root_name, None)
                    })
                    .clone();
                if let Some((target, _)) = resolved {
                    idx.importers
                        .entry(target.clone())
                        .or_default()
                        .insert(pf.path.clone());
                    // A namespace bind has no single exported name; the member
                    // call supplies it. It is a module bind by construction.
                    local_to_target.insert(ns.alias.clone(), (target, String::new(), true));
                }
            }
            import_targets.insert(pf.path.as_str(), local_to_target);
        }

        // Pass 2: grade every call site. Anything ungradeable is dropped.
        let mut raw: BTreeMap<(String, String), Vec<CallerRow>> = BTreeMap::new();
        for pf in files {
            let own = module_callables.get(pf.path.as_str());
            let imports = import_targets.get(pf.path.as_str());

            for site in &pf.call_sites {
                let mut push = |target: &str, name: &str, grade: Grade| {
                    raw.entry((target.to_string(), name.to_string()))
                        .or_default()
                        .push(CallerRow {
                            path: pf.path.clone(),
                            caller: site.caller.clone(),
                            line: site.line,
                            grade,
                        });
                };

                match site.kind.as_str() {
                    // A bare call resolves to this file's own definitions, or to
                    // an imported binding of that exact name.
                    "bare" => {
                        if own.is_some_and(|d| d.contains(site.name.as_str())) {
                            push(&pf.path, &site.name, Grade::SameFile);
                        } else if let Some((target, exported, _)) =
                            imports.and_then(|m| m.get(&site.name))
                        {
                            // The edge lands on the EXPORTED name, which is what
                            // the target actually defines -- not on the local
                            // alias the caller happens to use.
                            if !exported.is_empty()
                                && export_contains(&idx.exports, target, exported)
                            {
                                push(target, exported, Grade::Import);
                            }
                        }
                    }
                    // `mod.func()` where `mod` is an imported module binding.
                    "member" => {
                        let Some(recv) = site.receiver.as_deref() else {
                            continue;
                        };
                        if let Some((target, _, is_module_binding)) =
                            imports.and_then(|m| m.get(recv))
                        {
                            // ONLY a module binding. A named VALUE import is not
                            // one: `from pkg.models import User` then
                            // `User.create()` is a class static, and grading it
                            // as a module attribute bound the call to a
                            // module-level `create` it never reaches.
                            //
                            // A module-attribute call names its own member, so
                            // the call site's name is the right one here.
                            if *is_module_binding
                                && export_contains(&idx.exports, target, &site.name)
                            {
                                push(target, &site.name, Grade::ModuleAttribute);
                            }
                        }
                    }
                    // `self.method()` binds to a callable of that name defined
                    // in this file.
                    //
                    // The honest limit, since the module header promises
                    // precision: this is a NAME match within the file, not a
                    // resolved one. Without type information there is no way to
                    // tell which class a `self` refers to, so two classes in one
                    // file that both define `save` share the edge. The edge is
                    // still file-scoped -- it never reaches across files on a
                    // name alone, which is where the false positives live.
                    "self" if own.is_some_and(|d| d.contains(site.name.as_str())) => {
                        push(&pf.path, &site.name, Grade::SameFile);
                    }
                    _ => {}
                }
            }
        }

        for ((file, name), mut rows) in raw {
            rows.sort_by(|a, b| (&a.path, &a.caller, a.line).cmp(&(&b.path, &b.caller, b.line)));
            let total = rows.len();
            let truncated = total > MAX_CALLERS_PER_CALLEE;
            rows.truncate(MAX_CALLERS_PER_CALLEE);
            idx.callees.entry(file).or_default().insert(
                name,
                CalleeEntry {
                    callers: rows,
                    total,
                    truncated,
                },
            );
        }

        idx
    }

    /// Direct callers of `file::name`. `None` means "nothing recorded", which is
    /// never the same as "nothing calls it".
    pub fn callers_of(&self, file: &str, name: &str) -> Option<&CalleeEntry> {
        self.callees.get(file)?.get(name)
    }

    /// Whether this file declares this symbol.
    ///
    /// Distinct from having callers: `callees[file][name]` only exists once a
    /// caller row was pushed, so an exported-but-uncalled function is invisible
    /// there and reads identically to a symbol that does not exist.
    pub fn defines(&self, file: &str, name: &str) -> bool {
        self.symbols
            .get(name)
            .is_some_and(|defs| defs.iter().any(|d| d.file == file))
    }

    /// Outgoing calls from one symbol.
    ///
    /// Built by inverting the caller map. Chameleon rebuilds this by scanning
    /// every stored row on every query; doing it once here is free.
    pub fn callees_of(&self, file: &str, caller: &str) -> Vec<(String, String, Grade)> {
        let mut out = Vec::new();
        for (callee_file, names) in &self.callees {
            for (callee_name, entry) in names {
                for row in &entry.callers {
                    if row.path == file && row.caller == caller {
                        out.push((callee_file.clone(), callee_name.clone(), row.grade));
                    }
                }
            }
        }
        out.sort();
        out.dedup();
        out
    }
}

fn export_contains(exports: &BTreeMap<String, ExportSet>, target: &str, name: &str) -> bool {
    exports
        .get(target)
        .is_some_and(|e| !e.open && e.names.contains(name))
}

fn render_signature(sig: &crate::core::CallableSignature) -> String {
    let params: Vec<String> = sig
        .params
        .iter()
        .map(|p| match &p.type_annotation {
            Some(t) => format!("{}: {t}", p.name),
            None => p.name.clone(),
        })
        .collect();
    match &sig.return_type {
        Some(r) => format!("{}({}) -> {r}", sig.name, params.join(", ")),
        None => format!("{}({})", sig.name, params.join(", ")),
    }
}

/// Resolve an import specifier to a corpus path.
///
/// Deliberately conservative: a dotted or slashed specifier is turned into a
/// path suffix and matched against real files. Zero matches or more than one
/// match both yield `None` -- an ambiguous resolution is not a resolution.
fn resolve_module(
    spec: &str,
    index: &ModuleIndex,
    importer: &str,
    root_name: &str,
    submodule: Option<&str>,
) -> Option<(String, bool)> {
    // A relative specifier is anchored at the importing file's directory. That
    // anchor is the one piece of information that makes the answer decidable,
    // so resolving `.util` by global suffix match -- which is what stripping the
    // dot does -- can bind it to a same-named file anywhere in the repo.
    let conv = ModuleConventions::for_path(importer);

    if spec.starts_with('.') {
        // Two relative spellings, and the level arithmetic differs.
        //
        // Python writes `.util` / `..pkg.mod`, where dots are BOTH the level
        // marker and the separator. JS writes `./util` / `../../lib/util`,
        // where each `../` is one level and `/` separates.
        //
        // Counting only the first run of dots -- which is what a single
        // `take_while` does -- reads `../../lib/util` as two levels and leaves a
        // literal `..` in the tail, so it matches nothing. Every import two or
        // more levels up then contributes no edge at all, silently.
        // A trailing dot-segment that IS an extension the engine parses. Tried
        // only after the literal tail fails, so a module whose name really
        // contains a dot keeps precedence.
        let mut extensionless: Option<String> = None;
        let (ups, tail) = if spec.contains('/') {
            let mut rest = spec;
            let mut ups = 1usize;
            loop {
                if let Some(r) = rest.strip_prefix("./") {
                    rest = r;
                } else if let Some(r) = rest.strip_prefix("../") {
                    rest = r;
                    ups += 1;
                } else {
                    break;
                }
            }
            let mut tail = rest.trim_start_matches('/').to_string();
            // `..` with no trailing slash ends the strip loop with a literal
            // `..` in the tail, which matches no stem.
            if tail == ".." {
                ups += 1;
                tail.clear();
            } else if tail == "." {
                tail.clear();
            }
            // Node ESM and TS NodeNext REQUIRE the extension on a relative
            // specifier (`./util.js`), while every stem in the index has it
            // stripped -- so a whole repo's relative imports resolved to
            // nothing.
            if let Some((stem, ext)) = tail.rsplit_once('.') {
                if !stem.is_empty() && looks_like_extension(ext) {
                    extensionless = Some(stem.to_string());
                }
            }
            (ups, tail)
        } else {
            let dots = spec.chars().take_while(|c| *c == '.').count();
            (dots, spec[dots..].replace('.', "/"))
        };

        let mut dir: Vec<&str> = importer.split('/').collect();
        dir.pop(); // the importing file itself
        for _ in 1..ups {
            // More levels than directories: the specifier points above the root.
            dir.pop()?;
        }
        let base = dir.join("/");
        let join = |a: &str, b: &str| -> String {
            match (a.is_empty(), b.is_empty()) {
                (_, true) => a.to_string(),
                (true, false) => b.to_string(),
                _ => format!("{a}/{b}"),
            }
        };

        if tail.is_empty() {
            // Python's `from . import util` names the submodule in the imported
            // name. JS's `from "."` does not -- it means this directory's index
            // module, and the name is a symbol re-exported from it.
            if conv.bare_import_names_submodule {
                if let Some(sub) = submodule {
                    if let Some(hit) =
                        exact_module_match(&join(&base, sub), index, conv.package_index)
                    {
                        // The imported NAME was the module. That is the one form
                        // where a local really does name a module, and it holds
                        // whether the submodule is a file or a package
                        // directory -- a package resolves to its `__init__`,
                        // whose basename is never the module name.
                        return Some((hit, true));
                    }
                }
            }
        }
        // Exact, not suffix: the anchor is the whole point, and reusing the
        // suffix matcher lets a root-level `.util` bind `handlers/util.py`.
        if let Some(hit) = exact_module_match(&join(&base, &tail), index, conv.package_index) {
            return Some((hit, false));
        }
        // Node ESM and TS NodeNext REQUIRE the extension on a relative
        // specifier (`./util.js`), while every stem in the index has it
        // stripped -- so a whole repo's relative imports resolved to nothing.
        return extensionless
            .and_then(|stem| exact_module_match(&join(&base, &stem), index, conv.package_index))
            .map(|hit| (hit, false));
    }

    let cleaned = spec.replace('.', "/");
    if cleaned.is_empty() {
        return None;
    }

    // Try the full specifier, then progressively drop leading package segments.
    //
    // The corpus root is chosen by the caller and is often *inside* the package,
    // so `chameleon_mcp.thresholds` has to be able to match a corpus that stores
    // that file as plain `thresholds.py`. Without the walk the whole import
    // graph silently comes back empty, which reads as "this repo has no
    // cross-file edges" rather than "you pointed me one directory too deep".
    // A ONE-segment specifier is the library case: `import requests`,
    // `require "json"`, `from "date-fns"`. Suffix-matching it binds any repo
    // file with that basename anywhere in the tree, and a local wrapper named
    // after the library it wraps is the common shape -- so `app/clients/
    // requests.py` became the target of `from requests import get`. The
    // language would resolve that to the installed package, which is not in the
    // corpus at all. Only a corpus-root-anchored file can satisfy it.
    if !cleaned.contains('/') {
        return exact_module_match(&cleaned, index, conv.package_index).map(|hit| (hit, false));
    }

    let mut candidate = cleaned.as_str();
    let mut dropped = false;
    loop {
        // Only the ORIGINAL specifier is suffix-matched. Once a leading segment
        // has been dropped, what remains is anchored at the corpus root by
        // construction, and suffix-matching it puts the library case back:
        // indexing a checkout named `redis`, `from redis.client import Redis`
        // dropped `redis` and then bound `client` to `app/client.py` anywhere in
        // the tree. The installed package is not in the corpus at all.
        let hit = if dropped {
            exact_module_match(candidate, index, conv.package_index)
        } else {
            unique_suffix_match(candidate, index, conv.package_index)
        };
        if let Some(hit) = hit {
            return Some((hit, false));
        }
        match candidate.split_once('/') {
            // A leading segment may be dropped ONLY when it names the corpus
            // root itself -- i.e. the caller pointed at the inside of the
            // package. Dropping any prefix lets `django.db` fall through to a
            // repo file called `db.py` and fabricate an edge to it, which is
            // precisely the wrong-binding this module promises never to make.
            Some((head, rest)) if !rest.is_empty() && head == root_name => {
                candidate = rest;
                dropped = true;
            }
            _ => return None,
        }
    }
}

/// Whether a relative specifier's trailing dot-segment is an extension the
/// index has already stemmed away, rather than part of the module name.
///
/// Membership in the registry, not a shape test. Every shipped language reaches
/// the relative branch, so a fixed JS/TS/Python/Ruby list silently dropped PHP's
/// `require './helper.php'` and Lua's `./mod.lua` -- but a pure shape test is
/// worse than either, because it strips extensions the engine does NOT parse:
/// `import logo from "./logo.svg"` then resolved to a `logo.ts` sitting next to
/// it, which is a fabricated edge to a module the language never loads. An
/// extension the engine parses is one the index really has stemmed away; any
/// other trailing segment is part of the name.
fn looks_like_extension(ext: &str) -> bool {
    static PARSED: std::sync::LazyLock<HashSet<String>> = std::sync::LazyLock::new(|| {
        crate::lang::Registry::load()
            .map(|r| {
                r.extensions()
                    .iter()
                    .filter_map(|e| e.strip_prefix('.').map(str::to_string))
                    .collect()
            })
            .unwrap_or_default()
    });
    PARSED.contains(&ext.to_ascii_lowercase())
}

/// Corpus paths bucketed by their final segment, so a specifier is checked
/// against a handful of candidates instead of every file.
///
/// Both match rules end at the same place -- `stem == candidate` or
/// `stem.ends_with("/candidate")` -- so every path that can satisfy a candidate
/// shares the candidate's LAST segment. Bucketing on that segment is therefore
/// exact, not a heuristic prefilter: nothing outside the bucket could have
/// matched. Measured on a 4,893-file corpus, resolve_module was 5.20s of the
/// 5.21s index build.
struct ModuleIndex<'a> {
    /// Extension-stripped stems, parallel to the input paths.
    stems: Vec<&'a str>,
    paths: &'a [&'a str],
    by_last_segment: HashMap<&'a str, Vec<usize>>,
}

impl<'a> ModuleIndex<'a> {
    fn build(paths: &'a [&'a str]) -> Self {
        let mut stems = Vec::with_capacity(paths.len());
        let mut by_last_segment: HashMap<&str, Vec<usize>> = HashMap::new();
        for (i, p) in paths.iter().enumerate() {
            let stem = p.rsplit_once('.').map(|(s, _)| s).unwrap_or(p);
            stems.push(stem);
            let last = stem.rsplit('/').next().unwrap_or(stem);
            by_last_segment.entry(last).or_default().push(i);
        }
        Self {
            stems,
            paths,
            by_last_segment,
        }
    }

    /// Every path whose stem is exactly `candidate` or ends with `/candidate`.
    fn matches(&self, candidate: &str) -> Vec<&'a str> {
        let last = candidate.rsplit('/').next().unwrap_or(candidate);
        let Some(bucket) = self.by_last_segment.get(last) else {
            return Vec::new();
        };
        let suffix = format!("/{candidate}");
        let mut out: Vec<&str> = bucket
            .iter()
            .filter(|&&i| self.stems[i] == candidate || self.stems[i].ends_with(&suffix))
            .map(|&i| self.paths[i])
            .collect();
        out.sort_unstable();
        out.dedup();
        out
    }

    /// Exactly-one-or-nothing, over the module form and its package form.
    fn resolve_exact(&self, candidate: &str, package_index: &str) -> Option<String> {
        let mut hits = self.matches(candidate);
        // A directory's own module counts as that module.
        let pkg = format!("{candidate}/{package_index}");
        for p in self.matches(&pkg) {
            if !hits.contains(&p) {
                hits.push(p);
            }
        }
        match hits.len() {
            1 => Some(hits[0].to_string()),
            _ => None,
        }
    }
}

/// How a language spells "the module that a directory stands for", and whether
/// a bare relative import names a submodule.
///
/// Python and JS both write `.`, and they mean different things by it. Python's
/// `from . import util` names the submodule in the IMPORTED NAME; JS's
/// `import { x } from "."` means the directory's index module and the name is
/// just a symbol from it. Applying Python's rule to JS binds a same-named
/// sibling instead of the barrel, which is a fabricated edge, not an
/// approximation -- the correct target is not even a candidate.
#[derive(Debug, Clone, Copy)]
struct ModuleConventions {
    /// Basename a directory's own module lives under (`__init__`, `index`).
    package_index: &'static str,
    /// Whether a bare relative specifier takes its submodule from the imported
    /// name.
    bare_import_names_submodule: bool,
}

impl ModuleConventions {
    fn for_path(path: &str) -> Self {
        let ext = path.rsplit_once('.').map(|(_, e)| e).unwrap_or("");
        match ext {
            "py" | "pyi" => Self {
                package_index: "__init__",
                bare_import_names_submodule: true,
            },
            _ => Self {
                package_index: "index",
                bare_import_names_submodule: false,
            },
        }
    }
}

/// An exact module match: the path's stem IS this module, or it is that
/// module's package `__init__`. No suffix matching -- used where an anchor has
/// already decided the answer.
fn exact_module_match(candidate: &str, index: &ModuleIndex, package_index: &str) -> Option<String> {
    // Exact stem equality only -- the relative branch has already resolved the
    // anchor, and reusing the suffix rule there lets a root-level `.util` bind
    // `handlers/util.py`.
    let pkg = format!("{candidate}/{package_index}");
    let mut hits: Vec<&str> = index
        .matches(candidate)
        .into_iter()
        .filter(|p| index_stem(p) == candidate)
        .collect();
    for p in index.matches(&pkg) {
        if index_stem(p) == pkg && !hits.contains(&p) {
            hits.push(p);
        }
    }
    match hits.len() {
        1 => Some(hits[0].to_string()),
        _ => None,
    }
}

fn index_stem(path: &str) -> &str {
    path.rsplit_once('.').map(|(s, _)| s).unwrap_or(path)
}

/// The corpus root's package name, when every path shares one leading segment.
///
/// Empty when they do not, which disables the prefix-dropping walk entirely --
/// the conservative direction.
fn common_root_name(paths: &[&str]) -> String {
    let mut heads = paths.iter().filter_map(|p| p.split('/').next());
    let Some(first) = heads.next() else {
        return String::new();
    };
    if heads.all(|h| h == first) && paths.iter().any(|p| p.contains('/')) {
        first.to_string()
    } else {
        String::new()
    }
}

/// A specifier resolves only when exactly one corpus file matches it.
///
/// Zero matches and many matches are both `None`: an ambiguous resolution is
/// not a resolution, and picking one would fabricate an edge.
fn unique_suffix_match(
    candidate: &str,
    index: &ModuleIndex,
    package_index: &str,
) -> Option<String> {
    index.resolve_exact(candidate, package_index)
}

/// One caller chain, root-first.
pub type Chain = Vec<ChainHop>;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChainHop {
    pub path: String,
    pub name: String,
    pub line: Option<usize>,
}

/// Bounds on a blast-radius walk.
#[derive(Debug, Clone, Copy)]
pub struct BlastLimits {
    pub depth: usize,
    pub fanout_per_node: usize,
    pub total_nodes: usize,
}

impl Default for BlastLimits {
    fn default() -> Self {
        // Chameleon's defaults, kept deliberately: depth 2 is what a reviewer
        // can actually read, and the caps stop a hub symbol from producing an
        // answer nobody will finish.
        Self {
            depth: 2,
            fanout_per_node: 10,
            total_nodes: 50,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlastRadius {
    pub found: bool,
    pub chains: Vec<Chain>,
    /// Distinct (path, name) pairs reached, excluding the root.
    pub reached: usize,
    pub truncated: bool,
}

/// Callers that terminate a chain without being informative on their own.
const UNINFORMATIVE: &[&str] = &["<module>", "<anonymous>"];

/// Walk callers upward, enumerating chains.
///
/// Chains, not a reached-node set, and cycle detection is **per chain rather
/// than global**. That is deliberate and it is the subtle part: a node reached
/// by two distinct paths (a diamond) must appear in both chains, because both
/// are real routes to the change. A global visited set would silently drop one
/// and understate the impact -- which is the one direction a blast radius must
/// never err in.
pub fn blast_radius(index: &CodeIndex, file: &str, name: &str, limits: BlastLimits) -> BlastRadius {
    // A real, exported, never-called function must not answer `found: false` --
    // that is the "symbol does not exist" answer, and the module's own header
    // says an empty caller set is not evidence of dead code. Existence comes
    // from the symbol table; emptiness is carried by `reached` and `chains`.
    let found = index.defines(file, name) || index.callers_of(file, name).is_some();
    let mut chains: Vec<Chain> = Vec::new();
    let mut nodes = 0usize;
    let mut fanout_clipped = false;
    // Whether the walk actually stopped early. `reached` is deduped while the
    // cap counts expansions, so any shared caller makes reached < nodes and a
    // reached-vs-cap comparison reports a truncated walk as complete -- the one
    // direction a blast radius must never err in.
    let mut hit_node_cap = false;
    // The walk's caps are not the only way a radius can be short. A symbol with
    // more than MAX_CALLERS_PER_CALLEE callers keeps only the first 100 rows,
    // and a file that hit the per-file call-site cap recorded no row at all for
    // the sites past it -- so a walk that finishes every chain it can see is
    // still incomplete. Reporting that as complete is the one direction this
    // must never err in.
    let mut rows_truncated = !index.capped_files.is_empty();
    // Same reasoning for the DEPTH clip: a chain cut at the limit whose tip
    // still has unvisited callers is an understated radius, and understating is
    // the one direction this must never err in.
    let mut depth_clipped = false;

    let root = ChainHop {
        path: file.to_string(),
        name: name.to_string(),
        line: None,
    };
    let mut stack: Vec<(Chain, BTreeSet<(String, String)>)> = vec![(
        vec![root],
        BTreeSet::from([(file.to_string(), name.to_string())]),
    )];

    while let Some((chain, seen)) = stack.pop() {
        let cur = chain.last().expect("a chain is never empty");
        if chain.len() > limits.depth {
            depth_clipped |= index.callers_of(&cur.path, &cur.name).is_some_and(|e| {
                e.callers
                    .iter()
                    .any(|r| !seen.contains(&(r.path.clone(), r.caller.clone())))
            });
        }
        if chain.len() > limits.depth
            || {
                if nodes >= limits.total_nodes {
                    hit_node_cap = true;
                }
                nodes >= limits.total_nodes
            }
            || UNINFORMATIVE.contains(&cur.name.as_str())
        {
            chains.push(chain);
            continue;
        }

        let entry = index.callers_of(&cur.path, &cur.name);
        rows_truncated |= entry.is_some_and(|e| e.truncated);
        let mut rows: Vec<&CallerRow> = entry
            .map(|e| e.callers.iter().collect())
            .unwrap_or_default();
        rows.sort_by(|a, b| (&a.path, &a.caller, a.line).cmp(&(&b.path, &b.caller, b.line)));

        let mut expanded = Vec::new();
        let mut expanded_keys = BTreeSet::new();
        for row in rows {
            if nodes >= limits.total_nodes {
                hit_node_cap = true;
                break;
            }
            if expanded.len() >= limits.fanout_per_node {
                fanout_clipped = true;
                break;
            }
            let key = (row.path.clone(), row.caller.clone());
            if seen.contains(&key) || expanded_keys.contains(&key) {
                continue;
            }
            expanded_keys.insert(key.clone());
            nodes += 1;
            let mut next_chain = chain.clone();
            next_chain.push(ChainHop {
                path: row.path.clone(),
                name: row.caller.clone(),
                line: Some(row.line),
            });
            let mut next_seen = seen.clone();
            next_seen.insert(key);
            expanded.push((next_chain, next_seen));
        }

        if expanded.is_empty() {
            chains.push(chain);
        } else {
            // Pushed reversed so popping preserves the deterministic order.
            for e in expanded.into_iter().rev() {
                stack.push(e);
            }
        }
    }

    let mut reached = BTreeSet::new();
    chains.retain(|c| c.len() >= 2);
    for c in &chains {
        for hop in &c[1..] {
            reached.insert((hop.path.clone(), hop.name.clone()));
        }
    }

    BlastRadius {
        found,
        truncated: hit_node_cap || fanout_clipped || depth_clipped || rows_truncated,
        reached: reached.len(),
        chains,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Limits;
    use crate::lang::Registry;

    fn corpus(files: &[(&str, &str)]) -> Vec<ParsedFile> {
        let reg = Registry::load().unwrap();
        files
            .iter()
            .map(|(p, src)| {
                let bound = reg.for_path(p).unwrap();
                crate::extract::extract(p, src.as_bytes(), bound, &Limits::default()).unwrap()
            })
            .collect()
    }

    #[test]
    fn same_file_calls_are_graded_and_found() {
        let files = corpus(&[(
            "a.py",
            "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let entry = idx
            .callers_of("a.py", "helper")
            .expect("helper has a caller");
        assert_eq!(entry.total, 1);
        assert_eq!(entry.callers[0].caller, "main");
        assert_eq!(entry.callers[0].grade, Grade::SameFile);
    }

    #[test]
    fn cross_file_import_calls_resolve() {
        let files = corpus(&[
            ("pkg/util.py", "def helper():\n    return 1\n"),
            (
                "pkg/app.py",
                "from pkg.util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        let entry = idx
            .callers_of("pkg/util.py", "helper")
            .expect("cross-file caller");
        assert_eq!(entry.callers[0].path, "pkg/app.py");
        assert_eq!(entry.callers[0].grade, Grade::Import);
    }

    #[test]
    fn an_ambiguous_module_specifier_records_no_edge() {
        // Two files could satisfy `util`; resolving to either would be a guess.
        let files = corpus(&[
            ("one/util.py", "def helper():\n    return 1\n"),
            ("two/util.py", "def helper():\n    return 2\n"),
            (
                "app.py",
                "from util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(idx.callers_of("one/util.py", "helper").is_none());
        assert!(idx.callers_of("two/util.py", "helper").is_none());
    }

    #[test]
    fn an_open_export_set_blocks_the_import_grade() {
        let files = corpus(&[
            (
                "pkg/util.py",
                "from other import *\ndef helper():\n    return 1\n",
            ),
            (
                "pkg/app.py",
                "from pkg.util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        // The target's export set is unenumerable, so the edge is not asserted.
        assert!(idx.callers_of("pkg/util.py", "helper").is_none());
    }

    #[test]
    fn callees_invert_the_caller_map() {
        let files = corpus(&[(
            "a.py",
            "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let out = idx.callees_of("a.py", "main");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].1, "helper");
    }

    #[test]
    fn blast_radius_walks_transitively_to_the_depth_cap() {
        let files = corpus(&[(
            "a.py",
            "def leaf():\n    return 1\n\ndef mid():\n    return leaf()\n\ndef top():\n    return mid()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(&idx, "a.py", "leaf", BlastLimits::default());
        assert!(br.found);
        assert_eq!(br.reached, 2, "mid and top, at depth 2");
        let longest = br.chains.iter().map(|c| c.len()).max().unwrap();
        assert_eq!(longest, 3, "root + mid + top");
    }

    #[test]
    fn depth_one_stops_at_direct_callers() {
        let files = corpus(&[(
            "a.py",
            "def leaf():\n    return 1\n\ndef mid():\n    return leaf()\n\ndef top():\n    return mid()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(
            &idx,
            "a.py",
            "leaf",
            BlastLimits {
                depth: 1,
                ..Default::default()
            },
        );
        assert_eq!(br.reached, 1, "only mid");
    }

    /// The diamond case that per-chain cycle detection exists for. `leaf` is
    /// reached through both `left` and `right`; a global visited set would drop
    /// one route and understate the blast radius.
    #[test]
    fn a_diamond_keeps_both_routes() {
        let files = corpus(&[(
            "a.py",
            "def leaf():\n    return 1\n\ndef left():\n    return leaf()\n\ndef right():\n    return leaf()\n\ndef top():\n    return left() + right()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(&idx, "a.py", "leaf", BlastLimits::default());
        let routes: Vec<Vec<&str>> = br
            .chains
            .iter()
            .map(|c| c.iter().map(|h| h.name.as_str()).collect())
            .collect();
        assert!(routes.iter().any(|r| r.contains(&"left")), "got {routes:?}");
        assert!(
            routes.iter().any(|r| r.contains(&"right")),
            "got {routes:?}"
        );
        assert_eq!(br.reached, 3, "left, right, top");
        // The discriminating assertion. Under a GLOBAL visited set the second
        // route stops at `right` because `top` is already marked, and every
        // check above still passes: both names appear, and `reached` is deduped
        // so it is still 3. Only the chain SHAPE separates the two.
        assert_eq!(
            routes.iter().filter(|r| r.len() == 3).count(),
            2,
            "both routes must reach top: {routes:?}"
        );
        assert!(
            routes.iter().all(|r| r.last() == Some(&"top")),
            "got {routes:?}"
        );
    }

    /// The library case. Suffix-matching a one-segment specifier binds any repo
    /// file with that basename anywhere in the tree, and a local wrapper named
    /// after the library it wraps is the common shape -- so the language
    /// resolves to an installed package that is not in the corpus at all, while
    /// the index resolves to a repo file and records an edge to it.
    #[test]
    fn a_library_import_never_binds_a_same_named_repo_file() {
        let files = corpus(&[
            ("app/clients/requests.py", "def get():\n    return 1\n"),
            (
                "app/main.py",
                "from requests import get\n\ndef run():\n    return get()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.callers_of("app/clients/requests.py", "get").is_none(),
            "the installed package is not this file"
        );
        assert!(!idx.importers.contains_key("app/clients/requests.py"));

        // A root-anchored module of the same name IS resolvable: the anchor is
        // what makes the answer decidable.
        let rooted = corpus(&[
            ("requests.py", "def get():\n    return 1\n"),
            (
                "main.py",
                "from requests import get\n\ndef run():\n    return get()\n",
            ),
        ]);
        let idx = CodeIndex::build(&rooted);
        assert!(idx.callers_of("requests.py", "get").is_some());
    }

    /// Whether a local names a MODULE is recorded where the resolution happens.
    /// Re-deriving it from the path afterwards is wrong in BOTH directions: a
    /// package resolves to its `__init__`, whose basename is never the module
    /// name, and a named value import of `util` from `util.ts` looks exactly
    /// like a module bind.
    #[test]
    fn only_a_real_module_bind_grades_a_member_call() {
        // A package submodule. The target is `util/__init__.py`, so any
        // basename comparison says "not a module" and drops a real edge.
        let pkg = corpus(&[
            ("util/__init__.py", "def thing():\n    return 1\n"),
            (
                "app.py",
                "from . import util\n\ndef run():\n    return util.thing()\n",
            ),
        ]);
        let idx = CodeIndex::build(&pkg);
        assert!(
            idx.callers_of("util/__init__.py", "thing").is_some(),
            "a package submodule is still a module"
        );

        // A named VALUE import whose local happens to match the file stem.
        let value = corpus(&[
            (
                "src/util.ts",
                "export const util = { run: () => 1 };\nexport function run() { return 2; }\n",
            ),
            (
                "src/app.ts",
                "import { util } from \"./util\";\nexport function go() { return util.run(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&value);
        assert!(
            idx.callers_of("src/util.ts", "run").is_none(),
            "util.run() calls the object's member, not the module-level run"
        );
    }

    /// An extension the ENGINE does not parse is part of the module name, not a
    /// stemmed suffix. A shape test admitted `svg`, `vue`, `mdx` and the rest,
    /// so `import logo from "./logo.svg"` resolved to a `logo.ts` sitting next
    /// to it -- an edge to a module the language never loads.
    #[test]
    fn an_asset_import_does_not_bind_a_same_stemmed_source_file() {
        let files = corpus(&[
            ("src/logo.ts", "export function logo() { return 1; }\n"),
            (
                "src/App.tsx",
                "import { logo } from \"./logo.svg\";\nexport function App() { return logo(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(idx.importers.is_empty(), "{:?}", idx.importers);

        // The case the strip exists for -- a source extension the engine does
        // parse, which the index really has stemmed away -- still resolves.
        let esm = corpus(&[
            ("src/util.ts", "export function help() { return 1; }\n"),
            (
                "src/app.ts",
                "import { help } from \"./util.js\";\nexport function run() { return help(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&esm);
        assert!(idx.callers_of("src/util.ts", "help").is_some());
    }

    /// A class static is not a module attribute. Discarding the exported name
    /// made every imported local read as a module binding, so `User.create()`
    /// bound a module-level `create` that the call never reaches.
    #[test]
    fn an_imported_class_is_not_a_module_binding() {
        let files = corpus(&[
            (
                "pkg/models.py",
                "def create(x):\n    return x\n\nclass User:\n    @staticmethod\n    def create(d):\n        return d\n",
            ),
            (
                "pkg/app.py",
                "from pkg.models import User\n\ndef run():\n    return User.create({})\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.callers_of("pkg/models.py", "create").is_none(),
            "the module-level create is not what User.create() calls"
        );

        // A real module binding still resolves: `from . import util` names the
        // SUBMODULE, so `util` is the module and `util.thing()` is genuinely a
        // module attribute.
        let ns = corpus(&[
            ("pkg/util.py", "def thing():\n    return 1\n"),
            (
                "pkg/main.py",
                "from . import util\n\ndef run():\n    return util.thing()\n",
            ),
        ]);
        let idx = CodeIndex::build(&ns);
        assert!(
            idx.callers_of("pkg/util.py", "thing").is_some(),
            "a submodule bind is still a module attribute"
        );
    }

    /// The candidate the prefix-drop loop PRODUCES is root-anchored by
    /// construction, so suffix-matching it puts the library case straight back:
    /// indexing a checkout named `redis`, `from redis.client import Redis` drops
    /// the root segment and then binds `client` to any `client.py` in the tree.
    #[test]
    fn a_dropped_root_segment_does_not_reopen_the_suffix_match() {
        let files = corpus(&[
            ("app/client.py", "class Redis:\n    pass\n"),
            (
                "app/main.py",
                "from redis.client import Redis\n\ndef run():\n    return Redis()\n",
            ),
        ]);
        let idx = CodeIndex::build_with_package(&files, "redis");
        assert!(
            idx.importers.is_empty(),
            "the installed package is not this repo file: {:?}",
            idx.importers
        );

        // The case the drop loop exists for still works: a corpus root INSIDE
        // the package, where the remainder really is the anchored path.
        let inside = corpus(&[
            ("util.py", "def helper():\n    return 1\n"),
            (
                "main.py",
                "from myapp.util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build_with_package(&inside, "myapp");
        assert!(idx.callers_of("util.py", "helper").is_some());
    }

    /// The walk's own caps are not the only way a radius comes back short. A
    /// symbol past the per-callee cap, or a file past the per-file call-site
    /// cap, loses rows before the walk ever runs -- and a walk that finishes
    /// every chain it can see then reported the result as complete.
    #[test]
    fn a_radius_built_on_capped_rows_is_not_reported_complete() {
        let files = corpus(&[("a.py", "def leaf():\n    return 1\n")]);
        // Mark the corpus as having lost call sites to the per-file cap, the
        // way a generated megafile does.
        let mut idx = CodeIndex::build(&files);
        assert!(!blast_radius(&idx, "a.py", "leaf", BlastLimits::default()).truncated);
        idx.capped_files.insert("app/generated.py".to_string());
        assert!(
            blast_radius(&idx, "a.py", "leaf", BlastLimits::default()).truncated,
            "a file that lost call sites can hold callers this radius cannot see"
        );
    }

    /// Node ESM and TS NodeNext require the extension on a relative specifier,
    /// while every stem in the index has it stripped -- so a whole repo's
    /// relative imports contributed no edges at all.
    #[test]
    fn a_relative_specifier_may_carry_its_extension() {
        let files = corpus(&[
            ("src/util.ts", "export function help() { return 1; }\n"),
            (
                "src/app.ts",
                "import { help } from \"./util.js\";\nexport function run() { return help(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.callers_of("src/util.ts", "help").is_some(),
            "./util.js is src/util.ts"
        );
    }

    /// `require_relative` is anchored at the importing file's directory, but it
    /// carries no leading dot. Falling through to the repo-wide suffix matcher
    /// is ambiguous where the language is exact, and binds the wrong file when
    /// the real sibling is outside the corpus.
    #[test]
    fn require_relative_resolves_against_its_own_directory() {
        let files = corpus(&[
            ("lib/tasks/helper.rb", "def build_it\n  1\nend\n"),
            ("spec/helper.rb", "def build_it\n  2\nend\n"),
            (
                "lib/tasks/build.rb",
                "require_relative 'helper'\n\ndef run\n  build_it\nend\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.importers
                .get("lib/tasks/helper.rb")
                .is_some_and(|s| s.contains("lib/tasks/build.rb")),
            "the sibling, not the same-named file in spec/"
        );
        assert!(!idx.importers.contains_key("spec/helper.rb"));
    }

    /// Two importers in one directory with different extensions want different
    /// answers for the same specifier, because resolution branches on the
    /// language. Keying the memo on the directory alone gave one the other's.
    #[test]
    fn the_resolution_memo_does_not_cross_languages() {
        let files = corpus(&[
            ("src/helper.py", "def go():\n    return 1\n"),
            ("src/index.ts", "export function helper() { return 1; }\n"),
            (
                "src/app.py",
                "from . import helper\n\ndef run():\n    return helper.go()\n",
            ),
            (
                "src/app.ts",
                "import { helper } from \".\";\nexport function run() { return helper(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.importers
                .get("src/index.ts")
                .is_some_and(|s| s.contains("src/app.ts")),
            "the TS file resolves `.` to the barrel"
        );
        assert!(
            !idx.importers
                .get("src/helper.py")
                .is_some_and(|s| s.contains("src/app.ts")),
            "and never to the same-named Python module next to it"
        );
    }

    /// A depth-clipped walk reported `truncated: false`, so a consumer read the
    /// caller list as exhaustive -- understating the radius, which is the one
    /// direction this must never err in.
    #[test]
    fn a_depth_clipped_walk_says_so() {
        let files = corpus(&[(
            "a.py",
            "def leaf():\n    return 1\n\ndef mid():\n    return leaf()\n\ndef top():\n    return mid()\n\ndef apex():\n    return top()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(&idx, "a.py", "leaf", BlastLimits::default());
        assert!(
            br.truncated,
            "apex is a real caller and was never enumerated"
        );

        let deep = blast_radius(
            &idx,
            "a.py",
            "leaf",
            BlastLimits {
                depth: 8,
                ..BlastLimits::default()
            },
        );
        assert!(!deep.truncated, "a complete walk is not truncated");
    }

    /// An exported, never-called function exists. Answering `found: false` is
    /// the "no such symbol" answer, and the module's own header says an empty
    /// caller set is not evidence of dead code.
    #[test]
    fn an_uncalled_symbol_is_still_found() {
        let files = corpus(&[("a.py", "def never_called():\n    return 1\n")]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(&idx, "a.py", "never_called", BlastLimits::default());
        assert!(br.found, "it exists; it just has no callers");
        assert_eq!(br.reached, 0);
        let missing = blast_radius(&idx, "a.py", "no_such_thing", BlastLimits::default());
        assert!(!missing.found);
    }

    #[test]
    fn a_cycle_terminates_rather_than_looping() {
        let files = corpus(&[(
            "a.py",
            "def ping():\n    return pong()\n\ndef pong():\n    return ping()\n",
        )]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(&idx, "a.py", "ping", BlastLimits::default());
        assert!(
            br.chains.iter().all(|c| c.len() <= 3),
            "cycle must not run away"
        );
    }

    #[test]
    fn an_unknown_symbol_is_not_found_and_reaches_nothing() {
        let files = corpus(&[("a.py", "def f():\n    return 1\n")]);
        let idx = CodeIndex::build(&files);
        let br = blast_radius(&idx, "a.py", "nope", BlastLimits::default());
        assert!(!br.found);
        assert_eq!(br.reached, 0);
    }

    /// The corpus root is often chosen one directory inside the package, so a
    /// fully-qualified specifier has to still find its file. Getting this wrong
    /// produces an empty import graph, which reads as "no cross-file edges"
    /// rather than "wrong root".
    #[test]
    fn a_package_qualified_specifier_resolves_inside_the_package() {
        let files = corpus(&[
            ("util.py", "def helper():\n    return 1\n"),
            (
                "app.py",
                "from mypkg.util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build_with_package(&files, "mypkg");
        let entry = idx
            .callers_of("util.py", "helper")
            .expect("mypkg.util must still resolve to util.py");
        assert_eq!(entry.callers[0].path, "app.py");
    }

    /// The prefix walk must not turn a third-party import into a repo edge.
    #[test]
    fn a_third_party_specifier_does_not_bind_to_a_same_named_repo_file() {
        let files = corpus(&[
            ("db.py", "def connect():\n    return 1\n"),
            (
                "app.py",
                "from django.db import connect\n\ndef run():\n    return connect()\n",
            ),
        ]);
        let idx = CodeIndex::build_with_package(&files, "myapp");
        assert!(
            idx.callers_of("db.py", "connect").is_none(),
            "django.db must not resolve to the repo's own db.py"
        );
        assert!(idx.importers.is_empty(), "no importer edge either");
    }

    /// A relative import is anchored at the importing file's directory, not
    /// matched globally by suffix.
    #[test]
    fn a_relative_import_resolves_against_its_own_directory() {
        let files = corpus(&[
            ("services/util.py", "def helper():\n    return 1\n"),
            ("handlers/util.py", "def helper():\n    return 2\n"),
            (
                "handlers/app.py",
                "from .util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.callers_of("handlers/util.py", "helper").is_some(),
            "`.util` must bind the sibling"
        );
        assert!(
            idx.callers_of("services/util.py", "helper").is_none(),
            "and must not bind the same-named file elsewhere"
        );
    }

    /// The segment walk must not buy resolution at the cost of inventing edges.
    #[test]
    fn the_segment_walk_still_refuses_an_ambiguous_match() {
        let files = corpus(&[
            ("one/util.py", "def helper():\n    return 1\n"),
            ("two/util.py", "def helper():\n    return 2\n"),
            (
                "app.py",
                "from pkg.util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(idx.callers_of("one/util.py", "helper").is_none());
        assert!(idx.callers_of("two/util.py", "helper").is_none());
    }

    /// The JS/TS relative form is slash-delimited, not dot-delimited. Treating
    /// `./util` as dotted yields `src//util`, which matches nothing -- so every
    /// relative import in a TS repo resolves to zero, and the cross-file layer
    /// is silently empty for the language family this engine most needs to serve.
    #[test]
    fn slash_relative_imports_resolve() {
        let files = corpus(&[
            ("src/util.ts", "export function helper() { return 1; }\n"),
            (
                "src/app.ts",
                "import { helper } from \"./util\";\nexport function run() { return helper(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        let entry = idx
            .callers_of("src/util.ts", "helper")
            .expect("./util must resolve to the sibling");
        assert_eq!(entry.callers[0].path, "src/app.ts");
    }

    /// `from . import util` names the submodule in the IMPORTED NAME, not in
    /// the specifier. Resolving the bare `.` binds the package's own
    /// `__init__`, so every call through `util` is graded against the package
    /// rather than the module that defines it.
    #[test]
    fn a_bare_dot_import_binds_the_submodule_not_the_package() {
        let files = corpus(&[
            ("pkg/__init__.py", "\n"),
            ("pkg/util.py", "def thing():\n    return 1\n"),
            (
                "pkg/app.py",
                "from . import util\n\ndef run():\n    return util.thing()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.importers
                .get("pkg/util.py")
                .is_some_and(|s| s.contains("pkg/app.py")),
            "the edge belongs on the submodule; got {:?}",
            idx.importers
        );
        assert!(
            !idx.importers
                .get("pkg/__init__.py")
                .is_some_and(|s| s.contains("pkg/app.py")),
            "and not on the package __init__"
        );
    }

    /// The anchor has to be exact. Reusing the suffix matcher lets a root-level
    /// `.util` bind a file in an unrelated directory.
    #[test]
    fn a_relative_import_does_not_suffix_match_another_directory() {
        let files = corpus(&[
            ("handlers/util.py", "def helper():\n    return 1\n"),
            (
                "app.py",
                "from .util import helper\n\ndef run():\n    return helper()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.callers_of("handlers/util.py", "helper").is_none(),
            "a root-level .util must not reach handlers/util.py"
        );
    }

    /// Blast truncation is load-bearing and had no test in either direction.
    #[test]
    fn blast_reports_truncation_honestly() {
        let complete = corpus(&[(
            "a.py",
            "def leaf():\n    return 1\n\ndef mid():\n    return leaf()\n",
        )]);
        let idx = CodeIndex::build(&complete);
        let br = blast_radius(&idx, "a.py", "leaf", BlastLimits::default());
        assert!(
            !br.truncated,
            "a walk that finished must not claim truncation"
        );

        // A fanout wider than the per-node cap must report truncated.
        let mut src = String::from("def leaf():\n    return 1\n");
        for i in 0..25 {
            src.push_str(&format!("\ndef c{i}():\n    return leaf()\n"));
        }
        let wide = corpus(&[("b.py", &src)]);
        let idx2 = CodeIndex::build(&wide);
        let br2 = blast_radius(&idx2, "b.py", "leaf", BlastLimits::default());
        assert!(
            br2.truncated,
            "25 callers past a fanout cap of 10 is truncated"
        );
    }

    /// Counting only the first run of dots reads `../../lib/util` as two levels
    /// and leaves a literal `..` in the tail, matching nothing -- so every
    /// import two or more levels up contributes no edge, silently.
    #[test]
    fn multi_level_relative_imports_resolve() {
        let files = corpus(&[
            ("src/lib/util.ts", "export function helper() { return 1; }\n"),
            (
                "src/a/near.ts",
                "import { helper } from \"../lib/util\";\nexport function near() { return helper(); }\n",
            ),
            (
                "src/a/b/deep.ts",
                "import { helper } from \"../../lib/util\";\nexport function deep() { return helper(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        let entry = idx
            .callers_of("src/lib/util.ts", "helper")
            .expect("both importers resolve");
        let callers: Vec<&str> = entry.callers.iter().map(|c| c.caller.as_str()).collect();
        assert!(
            callers.contains(&"deep"),
            "two levels up must resolve: {callers:?}"
        );
        assert!(
            callers.contains(&"near"),
            "one level up must resolve: {callers:?}"
        );
    }

    /// Python and JS both write `.` and mean different things. Python's
    /// `from . import util` names the submodule; JS's `from "."` means the
    /// directory's index module. Applying Python's rule to JS binds a same-named
    /// sibling instead of the barrel -- a fabricated edge, since the correct
    /// target is not even a candidate.
    #[test]
    fn a_bare_dot_import_is_language_aware() {
        let js = corpus(&[
            ("src/helper.ts", "export function helper() { return 1; }\n"),
            ("src/index.ts", "export function helper2() { return 2; }\n"),
            (
                "src/app.ts",
                "import { helper2 } from \".\";\nexport function run() { return helper2(); }\n",
            ),
        ]);
        let idx = CodeIndex::build(&js);
        assert!(
            idx.importers
                .get("src/index.ts")
                .is_some_and(|s| s.contains("src/app.ts")),
            "JS `from \".\"` means the index module; got {:?}",
            idx.importers
        );
        assert!(
            !idx.importers.contains_key("src/helper.ts"),
            "and must not bind the same-named sibling"
        );

        // Python keeps its own meaning.
        let py = corpus(&[
            ("pkg/__init__.py", "\n"),
            ("pkg/util.py", "def thing():\n    return 1\n"),
            (
                "pkg/app.py",
                "from . import util\n\ndef run():\n    return util.thing()\n",
            ),
        ]);
        let idx2 = CodeIndex::build(&py);
        assert!(
            idx2.importers
                .get("pkg/util.py")
                .is_some_and(|s| s.contains("pkg/app.py")),
            "Python `from . import util` still names the submodule"
        );
    }

    /// Keying the import map on the LOCAL alias discards which symbol was
    /// imported, so `from lib import real as alias` graded `alias()` against
    /// `lib::alias` -- a different function that merely happens to exist. A
    /// wrong edge, not a missing one.
    #[test]
    fn an_aliased_import_binds_the_exported_name() {
        let files = corpus(&[
            (
                "lib.py",
                "def real():\n    return 1\n\ndef alias():\n    return 2\n",
            ),
            (
                "app.py",
                "from lib import real as alias\n\ndef run():\n    return alias()\n",
            ),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(
            idx.callers_of("lib.py", "real").is_some(),
            "the edge belongs on `real`"
        );
        assert!(
            idx.callers_of("lib.py", "alias").is_none(),
            "and must NOT land on the unrelated `alias`"
        );
    }

    /// The bucketed index must be EXACT, not a prefilter. Both match rules end
    /// at `stem == candidate` or `stem.ends_with("/candidate")`, so every path
    /// that can satisfy a candidate shares its last segment -- which is what
    /// makes bucketing on that segment lossless. A multi-segment candidate is
    /// the case that would break a naive last-segment index.
    #[test]
    fn the_module_index_matches_multi_segment_candidates() {
        let paths = vec![
            "pkg/bootstrap/orchestrator.py",
            "other/orchestrator.py",
            "pkg/util/__init__.py",
            "pkg/util.py",
        ];
        let idx = ModuleIndex::build(&paths);

        // Multi-segment: only the one under bootstrap/.
        assert_eq!(
            idx.matches("bootstrap/orchestrator"),
            vec!["pkg/bootstrap/orchestrator.py"]
        );
        // Single segment: both files named orchestrator.
        assert_eq!(idx.matches("orchestrator").len(), 2);
        // A candidate nothing shares a last segment with.
        assert!(idx.matches("nothing/here").is_empty());
        // Ambiguity is not a resolution.
        assert_eq!(idx.resolve_exact("orchestrator", "__init__"), None);
        assert_eq!(
            idx.resolve_exact("bootstrap/orchestrator", "__init__")
                .as_deref(),
            Some("pkg/bootstrap/orchestrator.py")
        );
    }

    #[test]
    fn importers_are_recorded_per_target() {
        let files = corpus(&[
            ("pkg/util.py", "def helper():\n    return 1\n"),
            ("pkg/app.py", "from pkg.util import helper\n"),
        ]);
        let idx = CodeIndex::build(&files);
        assert!(idx.importers["pkg/util.py"].contains("pkg/app.py"));
    }
}
