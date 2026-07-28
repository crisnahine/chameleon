from chameleon_mcp.autopass import classify_security_surface, security_surface_categories


def test_auth_paths_flagged():
    assert classify_security_surface("app/controllers/sessions_controller.rb") == "auth"
    assert classify_security_surface("src/auth/login.ts") == "auth"
    assert classify_security_surface("app/policies/listing_policy.rb") == "auth"


def test_payment_paths_flagged():
    assert classify_security_surface("app/services/billing/charge.rb") == "payment"
    assert classify_security_surface("src/checkout/stripe.ts") == "payment"


def test_crypto_secret_paths_flagged():
    assert classify_security_surface("app/lib/encryption/lockbox.rb") == "crypto"
    assert classify_security_surface("src/lib/credentials.ts") == "crypto"


def test_migration_paths_flagged():
    assert classify_security_surface("db/migrate/20260101120000_add_x.rb") == "migration"


def test_every_supported_migration_layout_is_a_migration_surface():
    """An irreversible schema change is one of the five classes this row exists
    for, and it is the one whose layout differs most per framework. Alembic's
    default `alembic init alembic` puts revisions in `alembic/versions/` with no
    path component saying "migration" at all, and TypeORM's is the SINGULAR
    `src/migration/`. Both are what cochange._is_ts_migration_dir and
    signatures.python_role_for_path already treat as migrations, so a miss here
    is three classifiers disagreeing about one file, not a conservative default.
    """
    for path in (
        "alembic/versions/a1b2c3_add_column.py",
        "migrations/versions/a1b2c3_add_column.py",
        "app/migrations/0001_initial.py",
        "src/migration/1700000000000-AddColumn.ts",
        "src/migrations/1700000000000-AddColumn.ts",
        "db/schema.rb",
        "db/structure.sql",
    ):
        assert classify_security_surface(path) == "migration", path


def test_a_migration_named_file_outside_a_migration_dir_is_not_a_surface():
    """The row is structural on purpose (its exact and prefix sets are empty):
    only a migration DIRECTORY counts. Widening to a "migration" token would
    route every service and test that mentions the word, the same over-match the
    auth category's exact-only "auth" needle exists to avoid."""
    assert classify_security_surface("src/services/UserMigrationService.ts") is None
    assert classify_security_surface("app/jobs/run_migration.rb") is None


def test_infra_paths_flagged():
    assert classify_security_surface(".github/workflows/deploy.yml") == "infra"
    assert classify_security_surface("Dockerfile") == "infra"


def test_ordinary_path_not_flagged():
    assert classify_security_surface("src/components/Button.tsx") is None
    assert classify_security_surface("app/models/listing.rb") is None


def test_author_tokens_are_not_auth_surfaces():
    # Word-boundary precision: "author" is a whole token and must not trip the
    # auth category the way the old substring matcher did.
    assert classify_security_surface("src/components/AuthorCard.tsx") is None
    assert classify_security_surface("src/utils/authorship.ts") is None
    assert classify_security_surface("app/models/author.rb") is None


def test_camel_case_tokens_are_split_for_recall():
    assert classify_security_surface("src/services/loginThrottler.ts") == "auth"
    assert classify_security_surface("src/PasswordResetForm.tsx") == "auth"


def test_extension_token_matches_exactly():
    assert classify_security_surface("main.tf") == "infra"
    assert classify_security_surface("src/draft.ts") is None


def test_payment_prefix_matches_compound_token():
    assert classify_security_surface("app/services/charge_back.rb") == "payment"


def test_structural_needles_still_match_substrings():
    assert classify_security_surface("docker-compose.override.yml") == "infra"


def test_categories_over_a_changeset():
    paths = [
        "src/components/Button.tsx",
        "app/controllers/sessions_controller.rb",
        "db/migrate/20260101120000_add_x.rb",
    ]
    assert security_surface_categories(paths) == {"auth", "migration"}
