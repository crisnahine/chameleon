from chameleon_mcp.autopass import parse_numstat


def test_parse_numstat_basic():
    text = "30\t10\tsrc/a.ts\n5\t0\tsrc/auth/login.ts\n"

    rows = parse_numstat(text)

    assert rows == [
        {"path": "src/a.ts", "added": 30, "removed": 10},
        {"path": "src/auth/login.ts", "added": 5, "removed": 0},
    ]


def test_parse_numstat_binary_is_zeroed():
    # Binary files render added/removed as "-"; they carry no line count.
    rows = parse_numstat("-\t-\tassets/logo.png\n")

    assert rows == [{"path": "assets/logo.png", "added": 0, "removed": 0}]


def test_parse_numstat_skips_blank_and_malformed_lines():
    rows = parse_numstat("\n12\t3\tsrc/x.ts\ngarbage line with no tabs\n")

    assert rows == [{"path": "src/x.ts", "added": 12, "removed": 3}]


def test_parse_numstat_path_with_spaces_kept_intact():
    rows = parse_numstat("2\t1\tsrc/my component/file.ts\n")

    assert rows == [{"path": "src/my component/file.ts", "added": 2, "removed": 1}]


def test_parse_numstat_empty_input():
    assert parse_numstat("") == []
