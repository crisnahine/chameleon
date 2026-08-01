//! The universal rule schema and its evaluator.
//!
//! One shape encodes every kind of knowledge the taxonomy names -- conventions,
//! idioms, anti-patterns, smells, contracts, secrets, dependency policy -- so
//! that mining, teaching, enforcing, and rendering are all plumbing around a
//! single type. Chameleon has no declarative rule layer at all: its rules are
//! hardcoded emitters in Python, which is why a team cannot add one.
//!
//! Two things make a rule trustworthy enough to block on, and both are carried
//! *in the rule*: `calibrated_confidence`, measured by running the rule against
//! code the team already accepted, and `drift`, which demotes a rule whose
//! confidence is falling. Blocking is therefore an earned state rather than a
//! configuration flag -- a human cannot simply declare a rule blocking.

use std::collections::BTreeMap;

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use tree_sitter::{Parser, Query, QueryCursor, StreamingIterator};

use crate::lang::BoundLanguage;

/// What kind of knowledge this rule encodes. The bridge to the taxonomy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RuleKind {
    Convention,
    Idiom,
    Pattern,
    AntiPattern,
    Smell,
    Contract,
    Secret,
    DependencyPolicy,
    Coupling,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Info,
    Warn,
    Error,
}

/// `off` collects drift data without reporting; `shadow` reports without
/// blocking; `enforce` blocks. Every newly mined rule starts in shadow -- that
/// is the calibration runway, not a formality.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Off,
    Shadow,
    Enforce,
}

/// Which matcher evaluates this rule.
///
/// `TreeSitterQuery` and `Literal` are implemented. `Regex`, `AstGrep`,
/// `Semgrep`, and `Coupling` are declared because the schema is the contract
/// and a consumer must be able to round-trip a rule this build cannot run;
/// evaluating one returns an explicit `Unsupported` rather than silently
/// matching nothing. A rule that quietly never fires is worse than one that
/// says it cannot run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Engine {
    TreeSitterQuery,
    /// Literal substring scan, per line. Named for what it does: an engine
    /// called `regex` that silently treats `AKIA[0-9A-Z]{16}` as a literal
    /// finds nothing and reports clean, which is the exact failure this module
    /// exists to prevent.
    Literal,
    Regex,
    AstGrep,
    Semgrep,
    Coupling,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Paths {
    pub include: Vec<String>,
    pub exclude: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Match {
    pub engine: Engine,
    /// The engine-native rule body: an S-expression query, or a regex.
    pub rule: String,
    #[serde(default)]
    pub paths: Paths,
}

/// Whether a rule's confidence is holding, rising, or falling.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Drift {
    #[default]
    Stable,
    Weakening,
    Strengthening,
}

/// Where a rule came from. `Derived` rules were observed; `Taught` ones were
/// ratified by a human. The distinction is kept because it changes how a
/// disagreement should be resolved.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Source {
    Derived,
    Taught,
    MinedFromDocs,
}

/// One rule, complete.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Rule {
    pub id: String,
    pub kind: RuleKind,
    /// Empty means language-agnostic.
    #[serde(default)]
    pub languages: Vec<String>,
    /// `["*"]` means repo-wide.
    #[serde(default)]
    pub archetypes: Vec<String>,
    pub severity: Severity,
    pub mode: Mode,
    #[serde(rename = "match")]
    pub matcher: Match,
    pub message: String,
    #[serde(default)]
    pub rationale: String,
    /// Mined from the archetype's canonical witness, never authored: a synthetic
    /// example teaches a style the repo does not actually use.
    #[serde(default)]
    pub good_example: Option<String>,
    /// Mined from the repo's own highest-visibility violation. Real and
    /// falsifiable -- when the team fixes that file, the next refresh must find
    /// a new one or mark the rule fully green.
    #[serde(default)]
    pub counterexample: Option<String>,
    #[serde(default)]
    pub calibrated_confidence: f64,
    pub source: Source,
    #[serde(default)]
    pub drift: Drift,
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
}

/// The confidence a rule of this kind must reach before it may block.
///
/// Security and cross-file breakage must be near-certain because a false block
/// there is expensive and infuriating. Smells and idioms are judgment calls and
/// never earn a hard block at all, whatever their measured confidence.
pub fn block_threshold(kind: RuleKind) -> Option<f64> {
    match kind {
        RuleKind::Secret | RuleKind::Coupling | RuleKind::Contract => Some(0.99),
        RuleKind::Convention | RuleKind::DependencyPolicy | RuleKind::AntiPattern => Some(0.90),
        RuleKind::Smell | RuleKind::Idiom | RuleKind::Pattern => None,
    }
}

impl Rule {
    /// Whether this rule is currently allowed to block an edit.
    ///
    /// Three conditions, all required. A rule may be set to `enforce` by hand
    /// and still refuse to block, because the evidence has not been earned --
    /// that is the intended behavior, not a bug.
    pub fn may_block(&self) -> bool {
        self.mode == Mode::Enforce
            && self.drift != Drift::Weakening
            && block_threshold(self.kind).is_some_and(|t| self.calibrated_confidence >= t)
    }

    /// The escape-hatch comment for a language, rendered in its comment syntax.
    ///
    /// A reason is required. A bare ignore records nothing, and a cluster of
    /// ignores on one rule is the strongest available signal that the rule is
    /// wrong -- friction is calibration data, so it has to be legible.
    pub fn ignore_comment(&self, language: &str) -> String {
        let body = format!("chromatophore-ignore: {} <reason>", self.id);
        match language {
            "python" | "ruby" | "bash" | "yaml" | "toml" | "r" | "elixir" => format!("# {body}"),
            "lua" | "haskell" | "sql" => format!("-- {body}"),
            "html" => format!("<!-- {body} -->"),
            "css" => format!("/* {body} */"),
            _ => format!("// {body}"),
        }
    }

    fn applies_to_path(&self, path: &str) -> bool {
        if self
            .matcher
            .paths
            .exclude
            .iter()
            .any(|g| glob_match(g, path))
        {
            return false;
        }
        self.matcher.paths.include.is_empty()
            || self
                .matcher
                .paths
                .include
                .iter()
                .any(|g| glob_match(g, path))
    }
}

/// A single violation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Finding {
    pub rule_id: String,
    pub severity: Severity,
    pub message: String,
    pub path: String,
    pub line: usize,
    /// The offending source text, trimmed.
    pub excerpt: String,
    /// True when this finding may block, given the rule's earned confidence.
    pub blocking: bool,
}

/// Why a rule could not be evaluated. Explicit, never silent.
#[derive(Debug, Clone, PartialEq)]
pub enum Unsupported {
    Engine(Engine),
    BadQuery(String),
}

/// Evaluate one rule against one file.
///
/// Returns `Err(Unsupported)` when this build cannot run the rule's engine, so
/// the caller can report "not checked" rather than "clean".
pub fn evaluate(
    rule: &Rule,
    path: &str,
    source: &str,
    bound: &BoundLanguage,
) -> std::result::Result<Vec<Finding>, Unsupported> {
    if !rule.applies_to_path(path) {
        return Ok(Vec::new());
    }
    if !rule.languages.is_empty()
        && !rule
            .languages
            .iter()
            .any(|l| bound.spec.matches_language(l))
    {
        return Ok(Vec::new());
    }
    if rule.mode == Mode::Off {
        return Ok(Vec::new());
    }

    match rule.matcher.engine {
        Engine::Literal => Ok(eval_literal(rule, path, source)),
        Engine::TreeSitterQuery => eval_query(rule, path, source, bound),
        other => Err(Unsupported::Engine(other)),
    }
}

/// Substring/prefix matching standing in for a glob.
///
/// Deliberately small: only `*` is honored, at either end. A full glob engine is
/// a dependency this layer does not need, and a half-correct one that silently
/// mismatches paths would be worse than an obvious limitation.
fn glob_match(pattern: &str, path: &str) -> bool {
    let mut p = pattern;
    // `**/x` means "at any depth"; `x/**` means "anything under x".
    p = p.strip_prefix("**/").unwrap_or(p);
    if let Some(dir) = p.strip_suffix("/**") {
        let seg = dir.trim_start_matches("*/").trim_start_matches('*');
        return path == seg
            || path.starts_with(&format!("{seg}/"))
            || path.contains(&format!("/{seg}/"));
    }
    match (p.starts_with('*'), p.ends_with('*')) {
        (true, true) => path.contains(p.trim_matches('*')),
        (true, false) => path.ends_with(p.trim_start_matches('*')),
        (false, true) => path.starts_with(p.trim_end_matches('*')),
        (false, false) => path == p || path.ends_with(&format!("/{p}")),
    }
}

/// A deliberately literal line scan.
///
/// Not a regex engine: the `regex` crate would be one more dependency for what
/// mined dependency-policy rules actually need, which is "does this line contain
/// this import". A rule that needs real matching declares `regex` and is
/// reported `Unsupported` rather than silently mis-evaluated here.
fn eval_literal(rule: &Rule, path: &str, source: &str) -> Vec<Finding> {
    let needle = &rule.matcher.rule;
    // `"".contains("")` is true, so an empty needle would emit one finding per
    // line of every file in the corpus, each carrying the rule's severity.
    if needle.is_empty() {
        return Vec::new();
    }
    source
        .lines()
        .enumerate()
        .filter(|(_, line)| line.contains(needle.as_str()))
        .map(|(i, line)| Finding {
            rule_id: rule.id.clone(),
            severity: rule.severity,
            message: rule.message.clone(),
            path: path.to_string(),
            line: i + 1,
            excerpt: line.trim().to_string(),
            blocking: rule.may_block(),
        })
        .collect()
}

/// Evaluate a tree-sitter S-expression query.
///
/// The capture named `@violation` marks the reported node; without one, the
/// whole match is reported. This is the engine that makes a rule structural
/// rather than textual: `print($X)` inside a function is a different thing from
/// the word "print" in a docstring, and only the tree knows which is which.
fn eval_query(
    rule: &Rule,
    path: &str,
    source: &str,
    bound: &BoundLanguage,
) -> std::result::Result<Vec<Finding>, Unsupported> {
    let query = Query::new(&bound.language, &rule.matcher.rule)
        .map_err(|e| Unsupported::BadQuery(format!("{e:?}")))?;

    let mut parser = Parser::new();
    parser
        .set_language(&bound.language)
        .map_err(|e| Unsupported::BadQuery(e.to_string()))?;
    let Some(tree) = parser.parse(source, None) else {
        return Ok(Vec::new());
    };

    let violation_idx = query.capture_index_for_name("violation");
    let bytes = source.as_bytes();
    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), bytes);
    let mut out = Vec::new();

    // StreamingIterator, not Iterator: the underlying cursor is mutated per
    // step, so owned data has to be pulled out inside the loop.
    while let Some(m) = matches.next() {
        let node = match violation_idx {
            Some(idx) => m.captures.iter().find(|c| c.index == idx).map(|c| c.node),
            None => m.captures.first().map(|c| c.node),
        };
        let Some(node) = node else { continue };
        out.push(Finding {
            rule_id: rule.id.clone(),
            severity: rule.severity,
            message: rule.message.clone(),
            path: path.to_string(),
            line: node.start_position().row + 1,
            excerpt: node.utf8_text(bytes).unwrap_or("").trim().to_string(),
            blocking: rule.may_block(),
        });
    }
    Ok(out)
}

/// Load rules from a YAML-ish TOML document.
///
/// TOML rather than YAML because the engine already parses TOML for language
/// specs, and one serialization format is one fewer way for a rule file to be
/// subtly wrong.
pub fn load_rules(doc: &str) -> Result<Vec<Rule>> {
    #[derive(Deserialize)]
    struct Doc {
        #[serde(default)]
        rule: Vec<Rule>,
    }
    let parsed: Doc =
        toml::from_str(doc).map_err(|e| anyhow!("rule document is malformed: {e}"))?;
    Ok(parsed.rule)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::Registry;

    fn rule_toml() -> &'static str {
        r#"
[[rule]]
id = "py.logging.no-print-in-lib"
kind = "convention"
languages = ["python"]
archetypes = ["*"]
severity = "warn"
mode = "enforce"
message = "print() in library code -- use the project logger"
rationale = "All lib modules route logging through get_logger(__name__)."
calibrated_confidence = 0.97
source = "derived"

[rule.match]
engine = "tree-sitter-query"
rule = '(call function: (identifier) @violation (#eq? @violation "print"))'

[rule.match.paths]
exclude = ["**/tests/**", "scripts/*"]
"#
    }

    fn load_one() -> Rule {
        load_rules(rule_toml()).unwrap().pop().unwrap()
    }

    #[test]
    fn a_rule_round_trips_through_the_schema() {
        let r = load_one();
        assert_eq!(r.id, "py.logging.no-print-in-lib");
        assert_eq!(r.kind, RuleKind::Convention);
        assert_eq!(r.mode, Mode::Enforce);
        assert_eq!(r.matcher.engine, Engine::TreeSitterQuery);
        assert_eq!(r.drift, Drift::Stable, "drift defaults to stable");
    }

    /// Text predicates have to actually be applied.
    ///
    /// This is not a formality: if `#eq?` were ignored, `no-print` would match
    /// every bare call in the repo and the rule would read as catastrophically
    /// broken rather than subtly wrong. The earlier docstring test does NOT
    /// cover it -- a docstring is not a call, so it passes either way.
    #[test]
    fn text_predicates_are_honored_not_ignored() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let r = load_one();
        let src = "def f():\n    print('a')\n    logger('b')\n    other('c')\n";
        let found = evaluate(&r, "app/lib.py", src, bound).unwrap();
        assert_eq!(
            found.len(),
            1,
            "#eq? must exclude logger() and other(); got {found:?}"
        );
        assert_eq!(found[0].line, 2);
    }

    /// `#match?` is the other predicate the shipped example rules rely on.
    #[test]
    fn regex_predicates_are_honored() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let mut r = load_one();
        r.matcher.rule =
            r#"((call function: (identifier) @violation) (#match? @violation "^log_"))"#.into();
        let src = "def f():\n    log_start()\n    print('x')\n    log_end()\n";
        let found = evaluate(&r, "app/lib.py", src, bound).unwrap();
        assert_eq!(found.len(), 2, "only the log_* calls; got {found:?}");
    }

    #[test]
    fn a_structural_query_finds_the_call_and_not_the_docstring() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let r = load_one();
        let src = "def f():\n    \"\"\"never call print() here\"\"\"\n    print('x')\n";
        let found = evaluate(&r, "app/lib.py", src, bound).unwrap();
        assert_eq!(
            found.len(),
            1,
            "the docstring mention must not match: {found:?}"
        );
        assert_eq!(found[0].line, 3);
    }

    #[test]
    fn excluded_paths_are_not_evaluated() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let r = load_one();
        let src = "print('x')\n";
        assert!(evaluate(&r, "app/tests/test_x.py", src, bound)
            .unwrap()
            .is_empty());
        assert!(evaluate(&r, "scripts/run.py", src, bound)
            .unwrap()
            .is_empty());
        assert_eq!(evaluate(&r, "app/lib.py", src, bound).unwrap().len(), 1);
    }

    #[test]
    fn a_rule_for_another_language_does_not_fire() {
        let reg = Registry::load().unwrap();
        let go = reg.by_name("go").unwrap();
        let r = load_one();
        assert!(evaluate(&r, "main.go", "package m\n", go)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn mode_off_evaluates_to_nothing() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let mut r = load_one();
        r.mode = Mode::Off;
        assert!(evaluate(&r, "app/lib.py", "print('x')\n", bound)
            .unwrap()
            .is_empty());
    }

    /// The core of calibrated blocking: enforce alone is not enough.
    #[test]
    fn blocking_is_earned_not_declared() {
        let mut r = load_one();
        assert!(r.may_block(), "0.97 clears the 0.90 convention bar");

        r.calibrated_confidence = 0.80;
        assert!(!r.may_block(), "below the bar, enforce must not block");

        r.calibrated_confidence = 0.97;
        r.drift = Drift::Weakening;
        assert!(!r.may_block(), "a weakening rule is auto-demoted");

        r.drift = Drift::Stable;
        r.mode = Mode::Shadow;
        assert!(!r.may_block(), "shadow never blocks");
    }

    #[test]
    fn judgment_call_kinds_can_never_hard_block() {
        let mut r = load_one();
        r.kind = RuleKind::Smell;
        r.calibrated_confidence = 1.0;
        r.mode = Mode::Enforce;
        assert!(
            !r.may_block(),
            "a smell is a judgment call, whatever its confidence"
        );
        assert_eq!(block_threshold(RuleKind::Idiom), None);
        assert_eq!(block_threshold(RuleKind::Secret), Some(0.99));
    }

    /// A `.tsx` file must be covered by a rule that names `typescript`. TSX is
    /// a separate grammar but the same language for rule purposes, and in a
    /// React repo the alternative silently exempts every component.
    #[test]
    fn a_typescript_rule_covers_tsx() {
        let reg = Registry::load().unwrap();
        let tsx = reg.by_name("tsx").unwrap();
        let mut r = load_one();
        r.languages = vec!["typescript".into()];
        r.matcher.engine = Engine::Literal;
        r.matcher.rule = "console.log".into();
        r.matcher.paths = Paths::default();
        let found = evaluate(
            &r,
            "src/App.tsx",
            "const A = () => { console.log(1); };\n",
            tsx,
        )
        .unwrap();
        assert_eq!(found.len(), 1, "a typescript rule must reach .tsx");
    }

    /// The literal matcher must not masquerade as a regex engine: a secret rule
    /// written with a real pattern would find nothing and report clean.
    #[test]
    fn a_real_regex_is_unsupported_rather_than_silently_literal() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let mut r = load_one();
        r.matcher.engine = Engine::Regex;
        r.matcher.rule = "AKIA[0-9A-Z]{16}".into();
        match evaluate(&r, "app/lib.py", "KEY = 'AKIAIOSFODNN7EXAMPLE'\n", bound) {
            Err(Unsupported::Engine(Engine::Regex)) => {}
            other => panic!("expected Unsupported, got {other:?}"),
        }
    }

    #[test]
    fn an_unrunnable_engine_reports_unsupported_rather_than_clean() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let mut r = load_one();
        r.matcher.engine = Engine::Semgrep;
        match evaluate(&r, "app/lib.py", "print('x')\n", bound) {
            Err(Unsupported::Engine(Engine::Semgrep)) => {}
            other => panic!("expected an explicit Unsupported, got {other:?}"),
        }
    }

    #[test]
    fn a_malformed_query_is_reported_not_swallowed() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("python").unwrap();
        let mut r = load_one();
        r.matcher.rule = "(this is not a valid query".into();
        assert!(matches!(
            evaluate(&r, "app/lib.py", "x = 1\n", bound),
            Err(Unsupported::BadQuery(_))
        ));
    }

    #[test]
    fn ignore_comments_render_in_each_language_syntax() {
        let r = load_one();
        assert!(r
            .ignore_comment("python")
            .starts_with("# chromatophore-ignore:"));
        assert!(r
            .ignore_comment("rust")
            .starts_with("// chromatophore-ignore:"));
        assert!(r
            .ignore_comment("lua")
            .starts_with("-- chromatophore-ignore:"));
        assert!(r.ignore_comment("html").starts_with("<!-- "));
        assert!(
            r.ignore_comment("python").contains("<reason>"),
            "a reason is required"
        );
    }

    #[test]
    fn glob_matching_handles_the_shapes_rules_actually_use() {
        assert!(glob_match("**/tests/**", "app/tests/test_x.py"));
        assert!(glob_match("scripts/*", "scripts/run.py"));
        assert!(glob_match("*.spec.ts", "src/a.spec.ts"));
        assert!(glob_match("src/lib/http.ts", "src/lib/http.ts"));
        assert!(!glob_match("**/tests/**", "app/lib.py"));
    }

    #[test]
    fn a_literal_rule_scans_lines() {
        let reg = Registry::load().unwrap();
        let bound = reg.by_name("typescript").unwrap();
        let r = Rule {
            id: "ts.deps.no-axios".into(),
            kind: RuleKind::DependencyPolicy,
            languages: vec!["typescript".into()],
            archetypes: vec!["*".into()],
            severity: Severity::Error,
            mode: Mode::Enforce,
            matcher: Match {
                engine: Engine::Literal,
                rule: "from \"axios\"".into(),
                paths: Paths::default(),
            },
            message: "import the http wrapper, not axios".into(),
            rationale: String::new(),
            good_example: None,
            counterexample: None,
            calibrated_confidence: 0.95,
            source: Source::Taught,
            drift: Drift::Stable,
            metadata: BTreeMap::new(),
        };
        let src = "import axios from \"axios\";\nconst x = 1;\n";
        let found = evaluate(&r, "src/a.ts", src, bound).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].line, 1);
        assert!(
            found[0].blocking,
            "0.95 clears the 0.90 dependency-policy bar"
        );
    }
}
