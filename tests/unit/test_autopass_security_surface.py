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


def test_infra_paths_flagged():
    assert classify_security_surface(".github/workflows/deploy.yml") == "infra"
    assert classify_security_surface("Dockerfile") == "infra"


def test_ordinary_path_not_flagged():
    assert classify_security_surface("src/components/Button.tsx") is None
    assert classify_security_surface("app/models/listing.rb") is None


def test_categories_over_a_changeset():
    paths = [
        "src/components/Button.tsx",
        "app/controllers/sessions_controller.rb",
        "db/migrate/20260101120000_add_x.rb",
    ]
    assert security_surface_categories(paths) == {"auth", "migration"}
