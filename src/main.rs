//! The chromatophore binary.
//!
//! `dump` is the load-bearing subcommand: it speaks the exact protocol
//! chameleon's extractors already use -- absolute paths on stdin, one NDJSON
//! record per file on stdout -- so it can stand in for `libcst_dump.py`,
//! `prism_dump.rb`, and `ts_dump.mjs` at once, for every language it supports
//! rather than the three they cover between them.
//!
//! Argument parsing is hand-rolled. The surface is a handful of flags, and a
//! single offline binary is a stated design constraint, so a parser dependency
//! would cost more than it returns.

use std::io::{BufRead, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use rayon::prelude::*;

use chromatophore::core::{Limits, Record};
use chromatophore::index::{blast_radius, BlastLimits, CodeIndex};
use chromatophore::lang::Registry;
use chromatophore::{mine, parse_path, rules};

const USAGE: &str = "\
chromatophore -- a universal code-convention engine

USAGE:
    chromatophore <COMMAND> [OPTIONS]

COMMANDS:
    dump              Read absolute paths on stdin, emit one NDJSON record per file.
                      The drop-in protocol for chameleon's AST extractors.
    languages         List every supported language and its extensions.
    parse <FILE>      Parse one file and pretty-print its record.
    index <DIR>       Build the cross-file index over a directory; emit JSON.
    blast <DIR> <FILE> <SYMBOL>
                      Who breaks if SYMBOL changes. Chains, depth-stratified.
    mine <DIR>        Cluster into archetypes and derive conventions; emit JSON.
    check <DIR> <RULES.toml>
                      Evaluate a rule document over a directory.

OPTIONS:
    --max-file-bytes N     Per-file size ceiling (default 1000000)
    --max-call-sites N     Per-file recorded call-site cap (default 10000)
    --depth N              Blast-radius depth (default 2, max 4)
    --jobs N               Worker threads (default: all cores)
    -h, --help             Show this help
";

fn main() {
    if let Err(e) = run() {
        eprintln!("chromatophore: {e:#}");
        std::process::exit(1);
    }
}

struct Args {
    command: String,
    positional: Vec<String>,
    limits: Limits,
    depth: usize,
    jobs: Option<usize>,
}

fn parse_args() -> Result<Args> {
    let mut argv = std::env::args().skip(1);
    let command = argv.next().unwrap_or_else(|| "--help".to_string());
    let mut positional = Vec::new();
    let mut limits = Limits::default();
    let mut depth = BlastLimits::default().depth;
    let mut jobs = None;

    let rest: Vec<String> = argv.collect();
    let mut i = 0;
    while i < rest.len() {
        let arg = &rest[i];
        let take_value = |name: &str| -> Result<String> {
            rest.get(i + 1)
                .cloned()
                .with_context(|| format!("{name} needs a value"))
        };
        match arg.as_str() {
            "--max-file-bytes" => {
                limits.max_file_bytes = take_value("--max-file-bytes")?.parse()?;
                i += 2;
            }
            "--max-call-sites" => {
                limits.max_call_sites = take_value("--max-call-sites")?.parse()?;
                i += 2;
            }
            "--depth" => {
                depth = take_value("--depth")?.parse()?;
                i += 2;
            }
            "--jobs" => {
                jobs = Some(take_value("--jobs")?.parse()?);
                i += 2;
            }
            other if other.starts_with('-') => bail!("unknown option {other}"),
            other => {
                positional.push(other.to_string());
                i += 1;
            }
        }
    }

    Ok(Args {
        command,
        positional,
        limits,
        depth,
        jobs,
    })
}

fn run() -> Result<()> {
    let args = parse_args()?;

    if let Some(n) = args.jobs {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    match args.command.as_str() {
        "-h" | "--help" | "help" => {
            print!("{USAGE}");
            Ok(())
        }
        "--version" | "version" => {
            println!("chromatophore {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        "dump" => cmd_dump(&args),
        "languages" => cmd_languages(),
        "parse" => cmd_parse(&args),
        "index" => cmd_index(&args),
        "blast" => cmd_blast(&args),
        "mine" => cmd_mine(&args),
        "check" => cmd_check(&args),
        other => {
            eprint!("{USAGE}");
            bail!("unknown command {other}")
        }
    }
}

/// The extractor protocol.
///
/// Paths are read in chunks and parsed in parallel, then written in the order
/// they arrived. Chunking rather than draining stdin whole keeps this usable as
/// a genuinely long-lived streaming subprocess -- a caller that writes a batch,
/// reads it back, then writes more never deadlocks -- while still getting
/// parallelism across cores within each batch.
fn cmd_dump(args: &Args) -> Result<()> {
    const CHUNK: usize = 256;

    let registry = Registry::load()?;
    let stdin = std::io::stdin();
    let mut out = BufWriter::new(std::io::stdout().lock());
    let mut batch: Vec<String> = Vec::with_capacity(CHUNK);

    let flush_batch = |batch: &mut Vec<String>, out: &mut BufWriter<_>| -> Result<()> {
        if batch.is_empty() {
            return Ok(());
        }
        let records: Vec<Record> = batch
            .par_iter()
            .map(|p| parse_path(Path::new(p), &registry, &args.limits))
            .collect();
        for record in records {
            // A record that cannot be serialized must still be accounted for:
            // dropping the line silently would make the file look unattempted.
            match serde_json::to_string(&record) {
                Ok(line) => writeln!(out, "{line}")?,
                Err(e) => writeln!(
                    out,
                    r#"{{"path":{},"error":"extractor_crash","message":{}}}"#,
                    serde_json::to_string(record.path()).unwrap_or_else(|_| "\"?\"".into()),
                    serde_json::to_string(&e.to_string()).unwrap_or_else(|_| "\"?\"".into()),
                )?,
            }
        }
        out.flush()?;
        batch.clear();
        Ok(())
    };

    for line in stdin.lock().lines() {
        let line = line.context("reading stdin")?;
        let path = line.trim();
        if path.is_empty() {
            continue;
        }
        batch.push(path.to_string());
        if batch.len() >= CHUNK {
            flush_batch(&mut batch, &mut out)?;
        }
    }
    flush_batch(&mut batch, &mut out)?;
    Ok(())
}

fn cmd_languages() -> Result<()> {
    let registry = Registry::load()?;
    println!("{} languages", registry.len());
    for name in registry.names() {
        let bound = registry.by_name(name).expect("name came from the registry");
        println!(
            "  {:<12} abi={:<3} {}",
            name,
            bound.language.abi_version(),
            bound.spec.extensions.join(" ")
        );
    }
    Ok(())
}

fn cmd_parse(args: &Args) -> Result<()> {
    let path = args.positional.first().context("parse needs a file path")?;
    let registry = Registry::load()?;
    let record = parse_path(Path::new(path), &registry, &args.limits);
    println!("{}", serde_json::to_string_pretty(&record)?);
    Ok(())
}

/// Walk a directory for files the registry can parse.
///
/// Common heavy directories are pruned. This is a convenience for the CLI
/// subcommands; the `dump` protocol takes its file list from the caller, which
/// is the right split -- discovery policy belongs to the host.
fn collect_sources(root: &Path, registry: &Registry) -> Result<Vec<PathBuf>> {
    const PRUNE: &[&str] = &[
        ".git",
        "node_modules",
        "target",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
    ];
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name();
            let name = name.to_string_lossy();
            let Ok(ft) = entry.file_type() else { continue };
            if ft.is_symlink() {
                continue;
            }
            if ft.is_dir() {
                if !PRUNE.contains(&name.as_ref()) && !name.starts_with('.') {
                    stack.push(path);
                }
            } else if registry.for_path(&path.to_string_lossy()).is_some() {
                out.push(path);
            }
        }
    }
    out.sort();
    Ok(out)
}

fn parse_corpus(root: &Path, limits: &Limits) -> Result<(Vec<chromatophore::ParsedFile>, usize)> {
    let registry = Registry::load()?;
    let paths = collect_sources(root, &registry)?;
    let records: Vec<Record> = paths
        .par_iter()
        .map(|p| parse_path(p, &registry, limits))
        .collect();

    let mut parsed = Vec::new();
    let mut skipped = 0;
    for record in records {
        match record {
            Record::Parsed(pf) => {
                // Paths are stored repo-relative so an index is reproducible
                // byte-for-byte regardless of where the checkout lives.
                let mut pf = *pf;
                if let Ok(rel) = Path::new(&pf.path).strip_prefix(root) {
                    pf.path = rel.to_string_lossy().to_string();
                }
                parsed.push(pf);
            }
            Record::Failed(_) => skipped += 1,
        }
    }
    Ok((parsed, skipped))
}

fn cmd_index(args: &Args) -> Result<()> {
    let root = PathBuf::from(args.positional.first().context("index needs a directory")?);
    let (files, skipped) = parse_corpus(&root, &args.limits)?;
    let index = CodeIndex::build(&files);
    eprintln!("indexed {} files ({skipped} skipped)", files.len());
    println!("{}", serde_json::to_string_pretty(&index)?);
    Ok(())
}

fn cmd_blast(args: &Args) -> Result<()> {
    let root = PathBuf::from(args.positional.first().context("blast needs a directory")?);
    let file = args.positional.get(1).context("blast needs a file")?;
    let symbol = args.positional.get(2).context("blast needs a symbol")?;

    let (files, _) = parse_corpus(&root, &args.limits)?;
    let index = CodeIndex::build(&files);
    let limits = BlastLimits {
        depth: args.depth.clamp(1, 4),
        ..Default::default()
    };
    let result = blast_radius(&index, file, symbol, limits);

    println!("{}", serde_json::to_string_pretty(&result)?);
    if !result.found {
        // Never let an empty answer read as a verified zero.
        eprintln!(
            "note: nothing recorded for {file}::{symbol}. Absence of a caller edge is NOT \
             evidence of dead code -- dynamic dispatch and calls through an instance are \
             invisible to a static snapshot. Confirm with a grep before renaming or deleting."
        );
    }
    Ok(())
}

fn cmd_mine(args: &Args) -> Result<()> {
    let root = PathBuf::from(args.positional.first().context("mine needs a directory")?);
    let (files, skipped) = parse_corpus(&root, &args.limits)?;
    let archetypes = mine::cluster(&files);
    eprintln!(
        "mined {} archetypes from {} files ({skipped} skipped)",
        archetypes.len(),
        files.len()
    );
    println!("{}", serde_json::to_string_pretty(&archetypes)?);
    Ok(())
}

fn cmd_check(args: &Args) -> Result<()> {
    let root = PathBuf::from(args.positional.first().context("check needs a directory")?);
    let rules_path = args.positional.get(1).context("check needs a rules file")?;

    let mut doc = String::new();
    std::fs::File::open(rules_path)
        .with_context(|| format!("opening {rules_path}"))?
        .read_to_string(&mut doc)?;
    let rule_set = rules::load_rules(&doc)?;

    let registry = Registry::load()?;
    let paths = collect_sources(&root, &registry)?;

    let mut findings = Vec::new();
    let mut unsupported = Vec::new();
    for path in &paths {
        let display = path.to_string_lossy().to_string();
        let Some(bound) = registry.for_path(&display) else {
            continue;
        };
        let Ok(source) = std::fs::read_to_string(path) else {
            continue;
        };
        let rel = path
            .strip_prefix(&root)
            .unwrap_or(path)
            .to_string_lossy()
            .to_string();
        for rule in &rule_set {
            match rules::evaluate(rule, &rel, &source, bound) {
                Ok(mut f) => findings.append(&mut f),
                Err(reason) => {
                    let note = format!("{}: {reason:?}", rule.id);
                    if !unsupported.contains(&note) {
                        unsupported.push(note);
                    }
                }
            }
        }
    }

    // Rules that could not run are reported, never counted as clean.
    for note in &unsupported {
        eprintln!("not checked -- {note}");
    }
    eprintln!("{} finding(s) over {} files", findings.len(), paths.len());
    println!("{}", serde_json::to_string_pretty(&findings)?);
    Ok(())
}
