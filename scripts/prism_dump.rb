#!/usr/bin/env ruby
# frozen_string_literal: true


require 'json'
require 'prism'

MAX_AST_NODES = 50_000
MAX_PARSE_DIAGNOSTICS = 20
MAX_FILE_SIZE = 1_000_000
# A real source file declares a few dozen methods; cap the recorded headers so
# one outlier file cannot bloat the dump record (consensus needs a sample).
MAX_CALLABLE_SIGNATURES = 200
# One file's recorded call sites are capped so a generated megafile cannot
# bloat the dump; the true total is preserved for honest truncation.
MAX_CALL_SITES = 2000

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

# Classify one CallNode into the dump's call-site shape, or nil for callees the
# index can never resolve (a chained call result's name becomes the member
# receiver; send/public_send and other metaprogramming stay invisible by design).
def call_site_of(node)
  return nil unless node.is_a?(Prism::CallNode)
  name = node.name.to_s
  # Operator sends (+, <<, [], !) can never be index-resolved and would crowd
  # identifier calls out of the per-file cap; record identifier-named sends only.
  return nil unless name.match?(/\A[a-z_]/i)
  recv = node.receiver
  if recv.nil?
    { name: name, receiver: nil, kind: 'bare' }
  elsif recv.is_a?(Prism::SelfNode)
    { name: name, receiver: 'self', kind: 'self' }
  elsif (const = constant_name(recv))
    { name: name, receiver: const, kind: 'constant' }
  elsif recv.respond_to?(:name) && recv.name
    { name: name, receiver: recv.name.to_s, kind: 'member' }
  else
    nil
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

# A method definition opens a body-shape frame. Blocks (do..end / {}) are NOT
# frames: Ruby uses blocks pervasively for iteration and DSL config, so a block
# is idiomatic structure, not a separate "function" whose length is a smell. A
# block's branches and nesting are attributed to the enclosing def, which is the
# unit a reviewer reasons about.
def is_function_like?(node)
  node.is_a?(Prism::DefNode)
end

# Decision points for branch_count. Mirrors the cyclomatic decision set minus
# boolean operators. `when` clauses count individually so a long flat case reads
# as branchy rather than deep.
def is_branch_node?(node)
  case node
  when Prism::IfNode, Prism::UnlessNode, Prism::WhileNode, Prism::UntilNode,
       Prism::ForNode, Prism::WhenNode, Prism::InNode, Prism::RescueNode
    true
  else
    false
  end
end

# Branch nodes that also open a structural indent level (raise max_depth). A
# `when`/`in` clause adds a decision point but sits at the case's indent, so it
# counts toward branch_count only. Blocks raise depth because deeply chained
# iteration inside a method is the nesting reviewers flag.
def is_nesting_node?(node)
  case node
  when Prism::IfNode, Prism::UnlessNode, Prism::WhileNode, Prism::UntilNode,
       Prism::ForNode, Prism::CaseNode, Prism::BeginNode,
       Prism::BlockNode, Prism::RescueNode
    true
  else
    false
  end
end

def line_of(location)
  location&.start_line
rescue StandardError
  nil
end

def end_line_of(location)
  location&.end_line
rescue StandardError
  nil
end

def param_count_of(node)
  params = node.parameters
  return 0 unless params

  count = 0
  count += params.requireds.length if params.respond_to?(:requireds) && params.requireds
  count += params.optionals.length if params.respond_to?(:optionals) && params.optionals
  count += params.keywords.length if params.respond_to?(:keywords) && params.keywords
  count += 1 if params.respond_to?(:rest) && params.rest
  count += 1 if params.respond_to?(:keyword_rest) && params.keyword_rest
  count += 1 if params.respond_to?(:block) && params.block
  count
rescue StandardError
  0
end

# Flatten a constant path (`Api::V1::FooController`) or plain constant into a
# dotted-free string the consensus can key on. Returns nil for a dynamic
# superclass expression (`Class.new`, a method call) the static walk can't name.
def constant_name(node)
  case node
  when Prism::ConstantReadNode
    node.name.to_s
  when Prism::ConstantPathNode
    node.full_name
  end
rescue StandardError
  nil
end

# Structured parameter shape for one method header: each entry carries the
# binding name, whether it can be dropped (optional/keyword-with-default/splat
# all read as droppable), and its kind. Mirrors the TS extractor's param shape
# so the consensus comparison treats both languages the same way.
def param_shapes(node)
  params = node.parameters
  return [] unless params

  shapes = []
  if params.respond_to?(:requireds) && params.requireds
    params.requireds.each do |p|
      name = p.respond_to?(:name) && p.name ? p.name.to_s : '_'
      shapes << { name: name, optional: false, kind: 'positional' }
    end
  end
  if params.respond_to?(:optionals) && params.optionals
    params.optionals.each do |p|
      name = p.respond_to?(:name) && p.name ? p.name.to_s : '_'
      shapes << { name: name, optional: true, kind: 'optional' }
    end
  end
  if params.respond_to?(:rest) && params.rest
    name = params.rest.respond_to?(:name) && params.rest.name ? params.rest.name.to_s : '*'
    shapes << { name: name, optional: true, kind: 'rest' }
  end
  if params.respond_to?(:keywords) && params.keywords
    params.keywords.each do |p|
      name = p.respond_to?(:name) && p.name ? p.name.to_s.chomp(':') : '_'
      # A RequiredKeywordParameterNode has no default; an OptionalKeyword
      # parameter does. Both surface as keywords; only the latter is droppable.
      optional = p.is_a?(Prism::OptionalKeywordParameterNode)
      shapes << { name: name, optional: optional, kind: 'keyword' }
    end
  end
  if params.respond_to?(:keyword_rest) && params.keyword_rest
    shapes << { name: '**', optional: true, kind: 'keyword_rest' }
  end
  shapes
rescue StandardError
  []
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
  function_scopes = []
  callable_signatures = []
  call_sites = []
  call_sites_total = 0
  call_sites_truncated = false
  ast_node_count = 0
  walk_error = nil
  # Active body-shape frames, innermost last. Each frame tracks its own max
  # nesting depth and branch count so a nested def is measured independently of
  # its enclosing def.
  frame_stack = []
  # Enclosing class frames, innermost last. A method header records the class it
  # is defined in plus that class's superclass so a later override comparison can
  # tell which base contract the method belongs to without a second parse.
  class_stack = []
  # Enclosing def names, innermost last. Call sites read this to record which
  # method they were invoked from.
  def_stack = []

  walker = lambda do |node|
    ast_node_count += 1
    if ast_node_count > MAX_AST_NODES
      walk_error = 'ast_node_ceiling_exceeded'
      return
    end

    imp = call_to_import(node)
    import_specifiers << imp if imp

    site = call_site_of(node)
    if site
      call_sites_total += 1
      if call_sites.length < MAX_CALL_SITES
        call_sites << site.merge(
          line: line_of(node.location),
          caller: def_stack.last || '<module>'
        )
      else
        call_sites_truncated = true
      end
    end

    pushed_class = false
    if node.is_a?(Prism::ClassNode)
      class_stack.push({ name: constant_name(node.constant_path),
                         superclass: constant_name(node.superclass) })
      pushed_class = true
    end

    is_fn = is_function_like?(node)
    if is_fn
      start = line_of(node.location)
      finish = end_line_of(node.location)
      frame_stack.push({
        start_line: start,
        end_line: finish,
        line_span: start && finish ? finish - start + 1 : nil,
        param_count: param_count_of(node),
        max_depth: 0,
        branch_count: 0,
        depth: 0
      })
      def_stack.push(node.name.to_s)
      # An instance method `def foo` carries no explicit receiver; a class
      # method `def self.foo` does. The contract treats them separately so a
      # class-method override is not compared against an instance signature.
      receiver = node.respond_to?(:receiver) && node.receiver ? 'self' : nil
      enclosing = class_stack.last
      if callable_signatures.length < MAX_CALLABLE_SIGNATURES
        callable_signatures << {
          name: node.name.to_s,
          kind: receiver ? 'singleton_method' : 'method',
          params: param_shapes(node),
          is_default_export: false,
          enclosing_class: enclosing && enclosing[:name],
          base_class: enclosing && enclosing[:superclass],
          # Body span for the duplication catalog's body-hash fallback: a
          # body-exact clone whose name shares no tokens with the original
          # can only be paired by body identity.
          start_line: start,
          end_line: finish
        }
      end
    elsif !frame_stack.empty?
      frame = frame_stack.last
      frame[:branch_count] += 1 if is_branch_node?(node)
      if is_nesting_node?(node)
        frame[:depth] += 1
        frame[:max_depth] = frame[:depth] if frame[:depth] > frame[:max_depth]
      end
    end

    node.compact_child_nodes.each { |child| walker.call(child) }

    if is_fn
      def_stack.pop
      frame = frame_stack.pop
      function_scopes << {
        start_line: frame[:start_line],
        end_line: frame[:end_line],
        line_span: frame[:line_span],
        max_depth: frame[:max_depth],
        branch_count: frame[:branch_count],
        param_count: frame[:param_count]
      }
    elsif !frame_stack.empty? && is_nesting_node?(node)
      frame_stack.last[:depth] -= 1
    end

    class_stack.pop if pushed_class
  end

  begin
    walker.call(ast)
  rescue StandardError, SystemStackError => e
    # SystemStackError (deep recursion) is NOT a StandardError; without it a
    # modestly-nested file would crash the whole subprocess and silently
    # truncate the bootstrap sample from that file onward.
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
    parse_diagnostics_count: diagnostics,
    function_scopes: function_scopes,
    callable_signatures: callable_signatures,
    call_sites: call_sites,
    call_sites_total: call_sites_total,
    call_sites_truncated: call_sites_truncated
  }
end

STDIN.each_line do |line|
  path = line.strip
  next if path.empty?

  begin
    record = extract_file(path)
    STDOUT.puts JSON.generate(record)
    STDOUT.flush
  rescue StandardError, SystemStackError => e
    STDOUT.puts JSON.generate(path: path, error: 'extractor_crash', message: e.message)
    STDOUT.flush
  end
end
