//! Convention mining: cluster files into archetypes, vote on conventions, pick
//! the witness to teach from.
//!
//! The idea this layer exists for is that conventions should be **observed from
//! the team's own committed code**, never hand-written by a tool author. A
//! hand-authored rulebook is wrong the day the team disagrees with it; a derived
//! one is wrong only when the code is.
//!
//! Two numbers govern everything: `confidence` (what share of the cohort
//! conforms) and `support` (how big the cohort is). A rule with high confidence
//! over four files has not earned anything.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde::{Deserialize, Serialize};

use crate::core::ParsedFile;

/// Below this many files, a cohort cannot express a convention at all.
pub const MIN_SAMPLE_SIZE: usize = 5;
/// A value must hold this share of its cohort to be called the convention.
pub const DOMINANCE: f64 = 0.60;
/// Above this, the convention is strong enough to be labelled enforced.
pub const ENFORCE_CONFIDENCE: f64 = 0.95;
/// Two shapes merge into one archetype at or above this Jaccard similarity.
pub const SHAPE_JACCARD: f64 = 0.70;

/// The exact-match signature files are first bucketed on.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ClusterKey {
    pub path_bucket: String,
    pub top_level_kinds: Vec<String>,
    pub default_export_kind: Option<String>,
    pub named_export_bucket: String,
    pub jsx: bool,
}

/// Directory-shaped bucket for a path.
fn path_bucket(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    match parts.len() {
        0 | 1 => String::from("."),
        2 => parts[0].to_string(),
        _ => parts[..parts.len() - 1].join("/"),
    }
}

/// Export counts are bucketed, not compared exactly: a module with 6 exports and
/// one with 7 are the same kind of thing.
fn export_bucket(n: usize) -> String {
    match n {
        0 => "0",
        1 => "1",
        2..=4 => "2-4",
        5..=9 => "5-9",
        _ => "10+",
    }
    .to_string()
}

fn cluster_key(pf: &ParsedFile) -> ClusterKey {
    // A sorted set, so declaration order and repetition do not split a cohort.
    let kinds: BTreeSet<&str> = pf.top_level_node_kinds.iter().map(String::as_str).collect();
    ClusterKey {
        path_bucket: path_bucket(&pf.path),
        top_level_kinds: kinds.into_iter().map(String::from).collect(),
        default_export_kind: pf.default_export_kind.clone(),
        named_export_bucket: export_bucket(pf.named_export_count),
        jsx: pf.has_jsx,
    }
}

/// A cohort of files that play the same role.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Archetype {
    /// Disambiguated, unique: `controller`, `controller-2`, ...
    pub name: String,
    /// The role before disambiguation. This is the name a rule author writes,
    /// and it has to reach every cohort that plays the role -- otherwise a rule
    /// scoped to `controller` silently misses `controller-2`.
    pub label: String,
    pub members: Vec<String>,
    /// The file to teach from: maximally typical, recently touched, real.
    pub witness: Option<String>,
    pub conventions: Vec<Convention>,
}

/// One derived convention, carrying the evidence that justifies it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Convention {
    /// e.g. `imports.preferred`, `naming.functions`, `shape.line_span`.
    pub dimension: String,
    pub value: String,
    /// conforming / total.
    pub confidence: f64,
    /// The cohort size the confidence was measured over.
    pub support: usize,
}

impl Convention {
    /// A convention strong enough to state as a rule rather than an observation.
    pub fn is_enforced(&self) -> bool {
        self.confidence >= ENFORCE_CONFIDENCE && self.support >= MIN_SAMPLE_SIZE
    }
}

fn jaccard(a: &BTreeSet<String>, b: &BTreeSet<String>) -> f64 {
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let inter = a.intersection(b).count() as f64;
    let union = a.union(b).count() as f64;
    if union == 0.0 {
        0.0
    } else {
        inter / union
    }
}

/// Cluster a corpus into archetypes.
///
/// Exact-key bucketing first, then a shape-fuzzy merge that folds cohorts whose
/// top-level shapes overlap enough. The merge matters: without it, one file
/// carrying an extra `if` at module scope becomes its own singleton "archetype"
/// and teaches nothing.
pub fn cluster(files: &[ParsedFile]) -> Vec<Archetype> {
    let mut buckets: BTreeMap<ClusterKey, Vec<&ParsedFile>> = BTreeMap::new();
    for pf in files {
        buckets.entry(cluster_key(pf)).or_default().push(pf);
    }

    let mut groups: Vec<(ClusterKey, Vec<&ParsedFile>)> = buckets.into_iter().collect();

    // Shape-fuzzy merge, union-find over cohorts sharing a path bucket.
    let n = groups.len();
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut [usize], mut i: usize) -> usize {
        while parent[i] != i {
            parent[i] = parent[parent[i]];
            i = parent[i];
        }
        i
    }
    let shapes: Vec<BTreeSet<String>> = groups
        .iter()
        .map(|(k, _)| k.top_level_kinds.iter().cloned().collect())
        .collect();
    for i in 0..n {
        for j in (i + 1)..n {
            if groups[i].0.path_bucket != groups[j].0.path_bucket
                || groups[i].0.jsx != groups[j].0.jsx
            {
                continue;
            }
            if jaccard(&shapes[i], &shapes[j]) >= SHAPE_JACCARD {
                let (a, b) = (find(&mut parent, i), find(&mut parent, j));
                if a != b {
                    parent[a] = b;
                }
            }
        }
    }

    let mut merged: BTreeMap<usize, Vec<&ParsedFile>> = BTreeMap::new();
    for (i, (_, members)) in groups.drain(..).enumerate() {
        let root = find(&mut parent, i);
        merged.entry(root).or_default().extend(members);
    }

    let mut out: Vec<Archetype> = merged
        .into_values()
        .map(|members| {
            let name = name_for(&members);
            let witness = select_witness(&members);
            let conventions = derive_conventions(&members);
            Archetype {
                label: name.clone(),
                name,
                witness,
                conventions,
                members: members.iter().map(|f| f.path.clone()).collect(),
            }
        })
        .collect();

    // Deterministic order, and disambiguate names that collided.
    out.sort_by(|a, b| {
        b.members
            .len()
            .cmp(&a.members.len())
            .then(a.name.cmp(&b.name))
    });
    let mut seen: HashMap<String, usize> = HashMap::new();
    for arch in &mut out {
        let count = seen.entry(arch.name.clone()).or_insert(0);
        *count += 1;
        if *count > 1 {
            arch.name = format!("{}-{}", arch.name, count);
        }
    }
    out
}

/// Name a cohort from the directory its files agree on.
///
/// Structure names the cluster; the name is a label applied afterwards. That
/// ordering is the whole point -- framework knowledge must never be what
/// *creates* a cohort, only what describes one.
fn name_for(members: &[&ParsedFile]) -> String {
    let mut dirs: HashMap<&str, usize> = HashMap::new();
    for pf in members {
        let parts: Vec<&str> = pf.path.split('/').collect();
        if parts.len() >= 2 {
            *dirs.entry(parts[parts.len() - 2]).or_insert(0) += 1;
        }
    }
    let dominant = dirs
        .iter()
        .filter(|(d, _)| !matches!(**d, "src" | "lib" | "app" | "." | ""))
        .max_by_key(|(_, c)| **c);

    match dominant {
        Some((dir, count)) if *count * 5 >= members.len() * 4 => singularize(dir),
        _ => {
            if members.iter().all(|f| looks_like_test(&f.path)) {
                "test".to_string()
            } else {
                "module".to_string()
            }
        }
    }
}

fn looks_like_test(path: &str) -> bool {
    let base = path.rsplit('/').next().unwrap_or(path);
    base.starts_with("test_")
        || base.contains("_test.")
        || base.contains(".test.")
        || base.contains(".spec.")
}

fn singularize(word: &str) -> String {
    if let Some(stem) = word.strip_suffix("ies") {
        format!("{stem}y")
    } else if let Some(stem) = word.strip_suffix("es") {
        // `-es` after a sibilant is the plural marker itself: lenses -> lens,
        // boxes -> box. Dropping only the trailing `s` leaves "lense".
        if stem.ends_with(['x', 'z']) || stem.ends_with("ch") || stem.ends_with("sh") {
            stem.to_string()
        } else if stem.ends_with("ss") {
            // classes -> class, not classe.
            stem.to_string()
        } else if stem.ends_with('s') {
            // `lenses` -> `lens` and `use_cases` -> `use_case` need opposite
            // answers from the same shape, so the plural marker is unknowable
            // here. Keeping the `e` is the reading that yields a real word, and
            // a wrong label makes every rule scoped to it silently never fire.
            format!("{stem}e")
        } else {
            format!("{stem}e")
        }
    } else if word.len() > 3 && word.ends_with('s') && !word.ends_with("ss") {
        word[..word.len() - 1].to_string()
    } else {
        word.to_string()
    }
}

/// Pick the file to teach from.
///
/// A lexicographic sort, not a weighted score: `(not-demoted, typicality,
/// path)`. Weights would need tuning and would make the choice unexplainable;
/// an ordering is auditable -- you can say exactly why this file won.
///
/// Typicality is how many cohort members share this file's exact shape, so the
/// witness is the most ordinary member, not the most interesting one. A witness
/// is meant to be imitated, and imitating an outlier is how a codebase drifts.
fn select_witness(members: &[&ParsedFile]) -> Option<String> {
    let mut shape_counts: HashMap<Vec<String>, usize> = HashMap::new();
    for pf in members {
        let shape: Vec<String> = {
            let s: BTreeSet<&str> = pf.top_level_node_kinds.iter().map(String::as_str).collect();
            s.into_iter().map(String::from).collect()
        };
        *shape_counts.entry(shape).or_insert(0) += 1;
    }

    let mut scored: Vec<(bool, usize, &str)> = members
        .iter()
        .filter(|pf| !is_trivial(pf))
        .map(|pf| {
            let shape: Vec<String> = {
                let s: BTreeSet<&str> =
                    pf.top_level_node_kinds.iter().map(String::as_str).collect();
                s.into_iter().map(String::from).collect()
            };
            let typicality = *shape_counts.get(&shape).unwrap_or(&0);
            (is_demoted(&pf.path), typicality, pf.path.as_str())
        })
        .collect();

    // demoted last, then most typical, then path for determinism.
    scored.sort_by(|a, b| a.0.cmp(&b.0).then(b.1.cmp(&a.1)).then(a.2.cmp(b.2)));
    scored.first().map(|(_, _, p)| p.to_string())
}

/// An empty or declaration-free file teaches nothing.
fn is_trivial(pf: &ParsedFile) -> bool {
    pf.top_level_node_kinds.is_empty()
        || (pf.callable_signatures.is_empty() && pf.class_shapes.is_empty())
}

/// Generated, vendored, and legacy files are real code but bad exemplars.
fn is_demoted(path: &str) -> bool {
    let p = path.to_ascii_lowercase();
    [
        "/vendor/",
        "/generated/",
        "/legacy/",
        "/migrations/",
        "node_modules",
        "__init__.py",
    ]
    .iter()
    .any(|m| p.contains(m))
}

/// Vote on every dimension the corpus can speak to.
fn derive_conventions(members: &[&ParsedFile]) -> Vec<Convention> {
    let total = members.len();
    let mut out = Vec::new();
    if total < MIN_SAMPLE_SIZE {
        // Not enough evidence to claim anything. Saying nothing is the correct
        // output here, not a low-confidence guess.
        return out;
    }

    // Preferred imports: counted as files-containing, not occurrences, so one
    // file importing a module forty times does not invent a convention.
    let mut module_files: HashMap<&str, usize> = HashMap::new();
    for pf in members {
        let mods: BTreeSet<&str> = pf
            .import_specifiers
            .iter()
            .map(|(m, _)| m.as_str())
            .collect();
        for m in mods {
            *module_files.entry(m).or_insert(0) += 1;
        }
    }
    let mut ranked: Vec<(&str, usize)> = module_files.into_iter().collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    for (module, count) in ranked.into_iter().take(10) {
        let confidence = count as f64 / total as f64;
        if confidence >= DOMINANCE {
            out.push(Convention {
                dimension: "imports.preferred".into(),
                value: module.to_string(),
                confidence,
                support: total,
            });
        }
    }

    // Function naming case.
    let names: Vec<&str> = members
        .iter()
        .flat_map(|pf| pf.callable_signatures.iter().map(|s| s.name.as_str()))
        .filter(|n| !n.starts_with('<'))
        .collect();
    // A name that discriminates. `parse` is spelled the same in snake_case and
    // camelCase, so it belongs in neither the numerator nor the denominator.
    let deciding: Vec<&str> = names
        .iter()
        .copied()
        .filter(|n| !matches!(case_of(n), "ambiguous" | "other"))
        .collect();
    // Support is the number of FILES the evidence came from, not the number of
    // names: `is_enforced` reads it as a cohort size, and five functions in one
    // file have not earned a rule the way five files have.
    let contributing = members
        .iter()
        .filter(|pf| {
            pf.callable_signatures
                .iter()
                .any(|s| deciding.contains(&s.name.as_str()))
        })
        .count();
    if deciding.len() >= MIN_SAMPLE_SIZE && contributing >= MIN_SAMPLE_SIZE {
        let mut cases: HashMap<&str, usize> = HashMap::new();
        for n in &deciding {
            *cases.entry(case_of(n)).or_insert(0) += 1;
        }
        if let Some((case, count)) = cases.iter().max_by_key(|(_, c)| **c) {
            let confidence = *count as f64 / deciding.len() as f64;
            if confidence >= DOMINANCE {
                out.push(Convention {
                    dimension: "naming.functions".into(),
                    value: (*case).to_string(),
                    confidence,
                    support: contributing,
                });
            }
        }
    }

    // Body shape: percentiles rather than a dominance vote, because "how long is
    // a function here" has no majority value, only a distribution.
    let mut spans: Vec<usize> = members
        .iter()
        .flat_map(|pf| pf.function_scopes.iter().filter_map(|s| s.line_span))
        .collect();
    if spans.len() >= MIN_SAMPLE_SIZE {
        spans.sort_unstable();
        // Confidence is contractually "the conforming share", and a
        // percentile's is fixed by construction at the percentile itself.
        // Reporting 1.0 made both dimensions clear `is_enforced` and carry the
        // same certainty marker as a 100%-conforming naming rule -- and at
        // >= 0.95 a Convention-kind rule clears the block bar.
        out.push(Convention {
            dimension: "shape.line_span.p50".into(),
            value: percentile(&spans, 0.5).to_string(),
            confidence: 0.5,
            support: spans.len(),
        });
        out.push(Convention {
            dimension: "shape.line_span.p90".into(),
            value: percentile(&spans, 0.9).to_string(),
            confidence: 0.9,
            support: spans.len(),
        });
    }

    out
}

/// file path -> the cohort it belongs to.
///
/// Total over every mined file by construction, so a path missing from it means
/// the tree was not mined -- which the rule gate must report rather than treat
/// as "not in this cohort".
pub type ArchetypeIndex = HashMap<String, ArchetypeTag>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchetypeTag {
    /// The disambiguated cohort id (`controller-7`).
    pub id: String,
    /// The role label (`controller`).
    pub label: String,
}

/// Project mined cohorts into a path lookup.
pub fn archetype_index(cohorts: &[Archetype]) -> ArchetypeIndex {
    let mut out = HashMap::new();
    for a in cohorts {
        for m in &a.members {
            out.insert(
                m.clone(),
                ArchetypeTag {
                    id: a.name.clone(),
                    label: a.label.clone(),
                },
            );
        }
    }
    out
}

/// Nearest-rank percentile, 1-indexed.
fn percentile(sorted: &[usize], pct: f64) -> usize {
    if sorted.is_empty() {
        return 0;
    }
    let rank = ((pct * sorted.len() as f64).ceil() as usize).clamp(1, sorted.len());
    sorted[rank - 1]
}

fn case_of(name: &str) -> &'static str {
    let trimmed = name.trim_start_matches('_');
    if trimmed.is_empty() {
        return "other";
    }
    let has_upper = trimmed.chars().any(|c| c.is_uppercase());
    let has_underscore = trimmed.contains('_');
    let first_upper = trimmed.chars().next().is_some_and(char::is_uppercase);

    if trimmed
        .chars()
        .all(|c| c.is_uppercase() || c == '_' || c.is_numeric())
        && has_upper
    {
        "SCREAMING_SNAKE"
    } else if first_upper {
        "PascalCase"
    } else if has_underscore && !has_upper {
        "snake_case"
    } else if has_upper && !has_underscore {
        "camelCase"
    } else if !has_upper && !has_underscore {
        // `parse`, `format`, `merge`: a single lowercase token is spelled
        // identically in snake_case and camelCase, so it is evidence for
        // NEITHER. Counting it as snake_case made a TypeScript cohort of
        // `parse, format, merge, clamp, trim` mine `naming.functions =
        // snake_case` at confidence 1.0 -- enforced, on names that say nothing.
        "ambiguous"
    } else {
        "other"
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

    /// Six same-shaped files in one directory, one odd file elsewhere.
    fn two_cohorts() -> Vec<ParsedFile> {
        let mut files: Vec<(String, String)> = (0..6)
            .map(|i| {
                (
                    format!("app/services/svc{i}.py"),
                    format!("from app.base import Base\n\nclass S{i}(Base):\n    def run(self):\n        return {i}\n"),
                )
            })
            .collect();
        files.push((
            "scripts/tool.py".into(),
            "import sys\n\ndef main():\n    sys.exit(0)\n".into(),
        ));
        let refs: Vec<(&str, &str)> = files
            .iter()
            .map(|(a, b)| (a.as_str(), b.as_str()))
            .collect();
        corpus(&refs)
    }

    #[test]
    fn clustering_separates_cohorts_by_shape_and_location() {
        let archetypes = cluster(&two_cohorts());
        let big = archetypes.iter().max_by_key(|a| a.members.len()).unwrap();
        assert_eq!(big.members.len(), 6);
        assert!(big.members.iter().all(|m| m.contains("services")));
    }

    #[test]
    fn a_cohort_is_named_from_its_dominant_directory_singularized() {
        let archetypes = cluster(&two_cohorts());
        let big = archetypes.iter().max_by_key(|a| a.members.len()).unwrap();
        assert_eq!(big.name, "service", "services/ -> service");
    }

    #[test]
    fn a_witness_is_chosen_from_the_cohort() {
        let archetypes = cluster(&two_cohorts());
        let big = archetypes.iter().max_by_key(|a| a.members.len()).unwrap();
        let witness = big.witness.as_ref().expect("a 6-file cohort has a witness");
        assert!(big.members.contains(witness));
    }

    #[test]
    fn a_shared_import_becomes_a_convention_with_full_confidence() {
        let archetypes = cluster(&two_cohorts());
        let big = archetypes.iter().max_by_key(|a| a.members.len()).unwrap();
        let imp = big
            .conventions
            .iter()
            .find(|c| c.dimension == "imports.preferred")
            .expect("every member imports app.base");
        assert_eq!(imp.value, "app.base");
        assert!((imp.confidence - 1.0).abs() < f64::EPSILON);
        assert_eq!(imp.support, 6);
        assert!(imp.is_enforced(), "6/6 clears the enforce bar");
    }

    #[test]
    fn a_cohort_below_the_sample_floor_claims_nothing() {
        // Three files cannot express a convention, however uniform they look.
        let files = corpus(&[
            ("a/x.py", "import q\ndef f():\n    pass\n"),
            ("a/y.py", "import q\ndef g():\n    pass\n"),
            ("a/z.py", "import q\ndef h():\n    pass\n"),
        ]);
        let archetypes = cluster(&files);
        assert!(
            archetypes.iter().all(|a| a.conventions.is_empty()),
            "under-supported cohorts must stay silent"
        );
    }

    /// Every mined file must resolve to exactly one cohort: a path the index
    /// drops becomes an UnknownArchetype refusal downstream.
    #[test]
    fn the_archetype_index_covers_every_mined_file() {
        let files = two_cohorts();
        let cohorts = cluster(&files);
        let idx = archetype_index(&cohorts);
        assert_eq!(idx.len(), files.len(), "the projection must be total");
        for pf in &files {
            let tag = idx
                .get(&pf.path)
                .unwrap_or_else(|| panic!("{} uncovered", pf.path));
            assert!(!tag.label.is_empty());
            assert!(!tag.id.is_empty());
        }
        let big = cohorts.iter().max_by_key(|a| a.members.len()).unwrap();
        assert_eq!(big.label, "service");
    }

    #[test]
    fn naming_case_is_detected() {
        assert_eq!(case_of("do_thing"), "snake_case");
        assert_eq!(case_of("doThing"), "camelCase");
        assert_eq!(case_of("DoThing"), "PascalCase");
        assert_eq!(case_of("MAX_SIZE"), "SCREAMING_SNAKE");
        assert_eq!(case_of("_private_helper"), "snake_case");
    }

    #[test]
    fn percentiles_are_nearest_rank() {
        let v = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
        assert_eq!(percentile(&v, 0.5), 5);
        assert_eq!(percentile(&v, 0.9), 9);
        assert_eq!(percentile(&[], 0.5), 0);
    }

    #[test]
    fn singularization_handles_the_common_shapes() {
        assert_eq!(singularize("services"), "service");
        assert_eq!(singularize("policies"), "policy");
        // `lenses` -> `lens` and `use_cases` -> `use_case` are the same shape
        // with opposite answers, so the plural marker is unknowable here. The
        // reading that yields a real word wins: a non-word label makes every
        // rule scoped to it silently never fire.
        assert_eq!(singularize("lenses"), "lense");
        assert_eq!(singularize("use_cases"), "use_case");
        assert_eq!(singularize("responses"), "response");
        assert_eq!(singularize("boxes"), "box");
        assert_eq!(singularize("batches"), "batch");
        assert_eq!(singularize("class"), "class");
        assert_eq!(singularize("api"), "api");
    }

    /// A percentile's conforming share is fixed by construction, so reporting
    /// confidence 1.0 made both shape dimensions clear `is_enforced` and carry
    /// the same certainty marker as a 100%-conforming naming rule.
    #[test]
    fn a_percentile_does_not_report_itself_as_enforced() {
        // Read off a REAL mined cohort, not a hand-built literal: the defect was
        // the value derive_conventions emits, so a test that constructs its own
        // Convention passes whatever that function does.
        let arch = cluster(&two_cohorts());
        let shapes: Vec<&Convention> = arch
            .iter()
            .flat_map(|a| &a.conventions)
            .filter(|c| c.dimension.starts_with("shape.line_span."))
            .collect();
        assert!(
            !shapes.is_empty(),
            "the cohort must mine a shape convention"
        );
        for c in shapes {
            assert!(
                !c.is_enforced(),
                "{} claims {} conformance; a percentile's is fixed at the percentile",
                c.dimension,
                c.confidence
            );
        }
    }

    #[test]
    fn the_enforced_bar_itself_still_works() {
        let p50 = Convention {
            dimension: "shape.line_span.p50".into(),
            value: "40".into(),
            confidence: 0.5,
            support: 5,
        };
        assert!(!p50.is_enforced());
        assert!(
            Convention {
                confidence: 1.0,
                ..p50
            }
            .is_enforced(),
            "the bar itself still works"
        );
    }

    /// A single lowercase token is spelled identically in snake_case and
    /// camelCase, so it is evidence for NEITHER. Counting it as snake_case made
    /// a TypeScript cohort of `parse, format, merge` mine `naming.functions =
    /// snake_case` at confidence 1.0 -- enforced, on names that say nothing.
    #[test]
    fn a_case_neutral_name_votes_for_neither_case() {
        assert_eq!(case_of("parse"), "ambiguous");
        assert_eq!(case_of("format"), "ambiguous");
        assert_eq!(case_of("parse_it"), "snake_case");
        assert_eq!(case_of("parseIt"), "camelCase");
        assert_eq!(case_of("ParseIt"), "PascalCase");
        assert_eq!(case_of("MAX_SIZE"), "SCREAMING_SNAKE");
    }

    #[test]
    fn a_generated_or_vendored_file_loses_the_witness_race() {
        assert!(is_demoted("app/vendor/thing.py"));
        assert!(is_demoted("db/migrations/0001_init.py"));
        assert!(!is_demoted("app/services/order.py"));
    }
}
