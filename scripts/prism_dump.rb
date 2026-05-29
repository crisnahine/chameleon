#!/usr/bin/env ruby
# frozen_string_literal: true


require 'json'
require 'prism'

MAX_AST_NODES = 50_000
MAX_PARSE_DIAGNOSTICS = 20
MAX_FILE_SIZE = 1_000_000

def kind_name(node)
  node.class.name.split('::').last
end

def call_to_import(node)
  return nil unless node.is_a?(Prism::CallNode)
  return nil unless node.receiver.nil?
  name = node.name.to_s
  args = node.arguments&.arguments
  return nil unless args && !args.empty?

  case name
  when 'require'
    target = string_value(args[0])
    target ? [target, 'default'] : nil
  when 'require_relative'
    target = string_value(args[0])
    target ? [target, 'namespace'] : nil
  when 'autoload'
    target = string_value(args[1]) if args.length >= 2
    target ? [target, 'named'] : nil
  end
end

def string_value(node)
  return nil unless node
  case node
  when Prism::StringNode
    node.unescaped
  when Prism::SymbolNode
    node.unescaped
  end
end

def is_top_level_export?(node)
  case node
  when Prism::ClassNode, Prism::ModuleNode, Prism::DefNode
    true
  else
    false
  end
end

def extract_file(file_path)
  begin
    stat = File.lstat(file_path)
  rescue StandardError => e
    return { path: file_path, error: 'read_error', message: e.message }
  end

  if stat.symlink?
    return { path: file_path, error: 'symlink_refused' }
  end

  if stat.size > MAX_FILE_SIZE
    return { path: file_path, error: 'file_too_large', size: stat.size }
  end

  begin
    content = File.read(file_path, mode: 'r:UTF-8', invalid: :replace, undef: :replace)
  rescue StandardError => e
    return { path: file_path, error: 'read_error', message: e.message }
  end

  result = Prism.parse(content, filepath: file_path)
  diagnostics = result.errors.length

  if diagnostics > MAX_PARSE_DIAGNOSTICS
    return { path: file_path, error: 'too_many_parse_errors', count: diagnostics }
  end

  ast = result.value
  statements = ast.statements&.body || []

  top_level_kinds = statements.map { |stmt| kind_name(stmt) }

  top_level_class_or_module = statements.select do |stmt|
    stmt.is_a?(Prism::ClassNode) || stmt.is_a?(Prism::ModuleNode)
  end
  default_export_kind = top_level_class_or_module.length == 1 ? kind_name(top_level_class_or_module.first) : nil

  named_export_count = statements.count { |stmt| is_top_level_export?(stmt) }

  import_specifiers = []
  ast_node_count = 0
  walk_error = nil

  walker = lambda do |node|
    ast_node_count += 1
    if ast_node_count > MAX_AST_NODES
      walk_error = 'ast_node_ceiling_exceeded'
      return
    end

    imp = call_to_import(node)
    import_specifiers << imp if imp

    node.compact_child_nodes.each { |child| walker.call(child) }
  end

  begin
    walker.call(ast)
  rescue StandardError => e
    return { path: file_path, error: 'walk_error', message: e.message }
  end

  return { path: file_path, error: walk_error } if walk_error

  {
    path: file_path,
    content_first_200_bytes: content[0...200],
    top_level_node_kinds: top_level_kinds,
    default_export_kind: default_export_kind,
    named_export_count: named_export_count,
    import_specifiers: import_specifiers,
    has_jsx: false,
    parse_diagnostics_count: diagnostics
  }
end

STDIN.each_line do |line|
  path = line.strip
  next if path.empty?

  begin
    record = extract_file(path)
    STDOUT.puts JSON.generate(record)
    STDOUT.flush
  rescue StandardError => e
    STDOUT.puts JSON.generate(path: path, error: 'extractor_crash', message: e.message)
    STDOUT.flush
  end
end
