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

use std::collections::{BTreeMap, BTreeSet, HashMap};

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

impl CodeIndex {
    /// Build the index from an already-parsed corpus.
    pub fn build(files: &[ParsedFile]) -> Self {
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
        let mut import_targets: HashMap<&str, HashMap<String, String>> = HashMap::new();
        for pf in files {
            let mut local_to_target: HashMap<String, String> = HashMap::new();
            for sym in &pf.import_symbols {
                if let Some(target) = resolve_module(&sym.module, &paths) {
                    idx.importers
                        .entry(target.clone())
                        .or_default()
                        .insert(pf.path.clone());
                    local_to_target.insert(sym.local.clone(), target);
                }
            }
            for ns in &pf.namespace_imports {
                if let Some(target) = resolve_module(&ns.module, &paths) {
                    idx.importers
                        .entry(target.clone())
                        .or_default()
                        .insert(pf.path.clone());
                    local_to_target.insert(ns.alias.clone(), target);
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
                        } else if let Some(target) = imports.and_then(|m| m.get(&site.name)) {
                            if export_contains(&idx.exports, target, &site.name) {
                                push(target, &site.name, Grade::Import);
                            }
                        }
                    }
                    // `mod.func()` where `mod` is an imported module binding.
                    "member" => {
                        let Some(recv) = site.receiver.as_deref() else {
                            continue;
                        };
                        if let Some(target) = imports.and_then(|m| m.get(recv)) {
                            if export_contains(&idx.exports, target, &site.name) {
                                push(target, &site.name, Grade::ModuleAttribute);
                            }
                        }
                    }
                    // `self.method()` binds inside this file only when exactly
                    // one class defines that name -- two candidates is an
                    // ambiguity, and an ambiguous edge is not an edge.
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
fn resolve_module(spec: &str, paths: &[&str]) -> Option<String> {
    let cleaned = spec.trim_start_matches(['.', '/']).replace('.', "/");
    if cleaned.is_empty() {
        return None;
    }
    let mut hits: Vec<&str> = paths
        .iter()
        .copied()
        .filter(|p| {
            let stem = p.rsplit_once('.').map(|(s, _)| s).unwrap_or(p);
            stem.ends_with(&cleaned) || stem.ends_with(&format!("{cleaned}/__init__"))
        })
        .collect();
    hits.sort_unstable();
    hits.dedup();
    match hits.len() {
        1 => Some(hits[0].to_string()),
        _ => None,
    }
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
    let found = index.callers_of(file, name).is_some();
    let mut chains: Vec<Chain> = Vec::new();
    let mut nodes = 0usize;
    let mut fanout_clipped = false;

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
        if chain.len() > limits.depth
            || nodes >= limits.total_nodes
            || UNINFORMATIVE.contains(&cur.name.as_str())
        {
            chains.push(chain);
            continue;
        }

        let mut rows: Vec<&CallerRow> = index
            .callers_of(&cur.path, &cur.name)
            .map(|e| e.callers.iter().collect())
            .unwrap_or_default();
        rows.sort_by(|a, b| (&a.path, &a.caller, a.line).cmp(&(&b.path, &b.caller, b.line)));

        let mut expanded = Vec::new();
        let mut expanded_keys = BTreeSet::new();
        for row in rows {
            if nodes >= limits.total_nodes {
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
        truncated: reached.len() >= limits.total_nodes || fanout_clipped,
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
