from pr_explorer.infrastructure.security.secret_scrubber import REDACTED, scrub, scrub_tree

# Fake credentials are assembled at runtime so secret scanners / push protection never see a token-shaped literal.
ALNUM = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
AWS_SECRET = "wJalr" + "XUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
GH_TOKEN = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----"


def test_redacts_known_secret_shapes_and_keeps_context():
    cases = {
        f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}": "AWS_SECRET_ACCESS_KEY=",
        "key " + "AKIA" + "IOSFODNN7EXAMPLE": "key ",
        f'if t == "{GH_TOKEN}":': 'if t == "ghp_',
        "DB_PASSWORD=hunter2hunter2": "DB_PASSWORD=",
        'password: "s3cr3t-value"': 'password: "',
        "postgres://admin:pa55word@db:5432/x": "postgres://admin:",
        "slack " + "xox" + "b-1234567890-abcdefghij": "slack xoxb-",
        PEM: "-----BEGIN PRIVATE KEY-----",
    }
    for raw, kept in cases.items():
        out = scrub(raw)
        assert REDACTED in out and out.startswith(kept), (raw, out)
    assert "wJalrXUtnFEMI" not in scrub(f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}")


def test_redacts_vendor_tokens_bearer_and_yaml_values():
    cases = {  # raw -> exact expected output
        "key=" + "sk-" + "proj-" + ALNUM + "_-x": "key=sk-proj-" + REDACTED,
        "OPENAI " + "sk-" + ALNUM: "OPENAI sk-" + REDACTED,
        "Authorization: " + "Bearer " + ALNUM + ".x-y_z": "Authorization: Bearer " + REDACTED,
        "curl -H 'authorization: " + "bearer " + ALNUM[:20] + "=='": "curl -H 'authorization: bearer " + REDACTED + "'",
        "gl" + "pat-" + ALNUM[:20]: "glpat-" + REDACTED,
        "//registry/:_authToken " + "npm" + "_" + ALNUM[:36]: "//registry/:_authToken npm_" + REDACTED,
        "HF " + "hf" + "_" + ALNUM[:34]: "HF hf_" + REDACTED,
        f"aws_secret_access_key: {AWS_SECRET}": "aws_secret_access_key: " + REDACTED,
        f"  - api_token: {ALNUM}  # rotated": "  - api_token: " + REDACTED + "  # rotated",
        'password = "correct horse battery"': 'password = "' + REDACTED + '"',
        '{"password": "correct horse battery"}': '{"password": "' + REDACTED + '"}',
    }
    for raw, want in cases.items():
        assert scrub(raw) == want, (raw, scrub(raw))


def test_leaves_ordinary_text_alone():
    for s in ["Authentication: required", "def get_token(user):", "token = os.environ['TOKEN']", "Views use @login_required",
              "Token: see docs", "password: required", "api_key: ${API_KEY}", "secret_key: settings.SECRET_KEY",
              "Bearer tokens are documented", 'f"Bearer {token}"', '"Bearer " + token', "use sk-learn for ML",
              '"token": "", "name": "x"', 'password = "", user = "bob"', "hf_hub_download(repo_id)"]:
        assert scrub(s) == s


def test_scrub_tree_walks_nested_values_and_honors_skip_keys():
    doc = {"a": ["pwd=abcdef"], "b": {"c": GH_TOKEN}, "head_sha": "abc", "n": 3}
    out = scrub_tree(doc, frozenset({"head_sha"}))
    assert out["a"] == [f"pwd={REDACTED}"] and REDACTED in out["b"]["c"] and out["n"] == 3


def test_redaction_keeps_closing_backtick_and_quotes():
    assert scrub(f"set `AWS_SECRET_ACCESS_KEY={AWS_SECRET[:20]}` first") == f"set `AWS_SECRET_ACCESS_KEY={REDACTED}` first"
    assert scrub('"DB_PASSWORD=hunter2hunter2"') == f'"DB_PASSWORD={REDACTED}"'


def test_quoted_values_with_spaces_backticks_and_html_are_redacted():
    from pr_explorer.infrastructure.security.secret_scrubber import scrub
    assert scrub('password = "my pass word"') == 'password = "[REDACTED]"'
    assert scrub("TOKEN=`x`") == "TOKEN=`[REDACTED]`"
    assert scrub("<input password=hunter22>") == "<input password=[REDACTED]>"
    assert scrub("""export DB_PASSWORD="p@ss'word" """) == """export DB_PASSWORD="[REDACTED]" """
