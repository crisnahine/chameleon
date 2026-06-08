import textwrap

from chameleon_mcp.tools import parse_edited_functions


def test_parse_edited_functions_ruby(tmp_path):
    # A Gemfile is needed so the Ruby extractor recognises this as a Ruby repo.
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    f = tmp_path / "probe.rb"
    f.write_text(
        textwrap.dedent("""
        class Probe
          def assemble(items)
            items.each do |row|
              process(row)
              log(row, row)
            end
          end
        end
    """).lstrip()
    )
    fns = parse_edited_functions(tmp_path, str(f))
    names = {fn.name for fn in fns}
    assert "assemble" in names
    a = next(fn for fn in fns if fn.name == "assemble")
    assert a.body_hash is not None
    assert a.start_line >= 1
    assert "process" in a.excerpt
    assert a.arity == 1
    assert a.required == 1
