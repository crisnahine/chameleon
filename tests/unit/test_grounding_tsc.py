from chameleon_mcp.grounding import files_with_type_errors, parse_tsc_output


def test_parse_tsc_basic():
    text = "src/index.ts(10,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"

    rows = parse_tsc_output(text)

    assert rows == [
        {
            "file": "src/index.ts",
            "line": 10,
            "col": 7,
            "code": "TS2322",
            "message": "Type 'string' is not assignable to type 'number'.",
        }
    ]


def test_parse_tsc_skips_summary_and_blank_lines():
    text = "\nFound 2 errors in 2 files.\nsrc/a.ts(1,1): error TS1005: ';' expected.\n"

    rows = parse_tsc_output(text)

    assert len(rows) == 1
    assert rows[0]["file"] == "src/a.ts"
    assert rows[0]["code"] == "TS1005"


def test_parse_tsc_path_with_spaces():
    text = "src/my dir/a.ts(2,3): error TS2304: Cannot find name 'foo'.\n"

    rows = parse_tsc_output(text)

    assert rows[0]["file"] == "src/my dir/a.ts"
    assert rows[0]["line"] == 2


def test_parse_tsc_empty():
    assert parse_tsc_output("") == []


def test_files_with_type_errors_dedups():
    text = (
        "src/a.ts(1,1): error TS1005: ';' expected.\n"
        "src/a.ts(9,2): error TS2304: Cannot find name 'x'.\n"
        "src/b.ts(3,3): error TS2322: bad.\n"
    )

    assert files_with_type_errors(text) == {"src/a.ts", "src/b.ts"}
