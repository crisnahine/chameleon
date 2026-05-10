#!/usr/bin/env ruby
# prism_dump.rb — Ruby AST extractor (Phase 8 / v1.5 scaffold).
#
# Mirrors scripts/ts_dump.mjs design:
#   - Long-lived process; Prism (Ruby AST library) loaded once
#   - Reads file paths from stdin (one per line)
#   - Emits NDJSON ParsedFile records to stdout
#   - Per-file caps: 50k AST nodes, 1 MB file size, 20 parse errors
#   - One file's parse error never aborts the run
#
# Per ARCHITECTURE.md "TypeScript-first extractor" -> "v1.5 expansion"
# + ADR-0003 (TypeScript only in v1.0; Ruby in v1.5).

require 'json'

STDERR.puts 'prism_dump.rb: Phase 8 (v1.5) placeholder.'
STDERR.puts 'Real implementation pending after v1.0 ships + validation gate passes.'
exit 0
