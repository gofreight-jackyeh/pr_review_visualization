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


def test_trigger_prefilter_never_skips_a_string_a_pattern_would_change():
    """`scrub` skips strings without a trigger literal; that is only sound if every pattern needs one. Every pattern
    has a sample here (add one with each new pattern); Unicode case variants (ſ ~ s, K ~ k, ı ~ i) match `(?i)` patterns
    and must reach them too."""
    from pr_explorer.infrastructure.security.secret_scrubber import _PATTERNS, _TRIGGER, _apply_patterns

    samples = [
        PEM, "-----BEGIN EC " + "PRIVATE KEY-----\nabc", GH_TOKEN, "github" + "_pat_" + ALNUM[:22],
        "AS" + "IA" + "IOSFODNN7EXAMPLE", "xox" + "p-1234567890-abc", "sk-" + "ant-" + ALNUM[:12],
        "sk-" + "svcacct-" + ALNUM[:22], "sk-" + ALNUM[:24], "AI" + "za" + ALNUM[:35], "rk" + "_live_" + ALNUM[:12],
        "gl" + "pat-" + ALNUM[:20], "npm" + "_" + ALNUM[:36], "hf" + "_" + ALNUM[:34], "Bearer " + ALNUM[:20],
        "ey" + "J" + ALNUM[:10] + ".ey" + "J" + ALNUM[:10] + "." + ALNUM[:10], "https://u:pa55@host/x",
        'API_KEY = "a b c"', "ACCESS-KEY=abcdef123", 'credential: "abc def"', "  private_key: abc/def+123",
        f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}", "Authorization: bearer " + ALNUM[:20], "auth_token='xyz123'",
    ]
    variants = {"s": "ſ", "k": "K", "i": "ı", "S": "ſ", "K": "K", "I": "ı"}  # ſ long s, K Kelvin sign, ı dotless i
    samples += [s.translate(str.maketrans({a: b})) for s in samples for a, b in variants.items() if a in s]
    samples += ["paſſword=abcdef12", "ToKEN=abcdef12", "aPı_key: 'abcdef'", "BEARER " + ALNUM[:20]]
    hit = [False] * len(_PATTERNS)
    for s in samples:
        for i, (pat, _) in enumerate(_PATTERNS):
            if pat.search(s):
                hit[i] = True
                assert _TRIGGER.search(s), (i, s)
        assert scrub(s) == _apply_patterns(s), s
    assert all(hit), [i for i, h in enumerate(hit) if not h]  # a pattern without a sample: add one above


def test_scrub_tree_memo_returns_the_same_result_for_repeated_strings():
    doc = {"a": [GH_TOKEN, GH_TOKEN, "x"], "b": {"c": GH_TOKEN, "d": "x"}, "head_sha": GH_TOKEN}
    out = scrub_tree(doc, frozenset({"head_sha"}))
    assert out == {"a": [scrub(GH_TOKEN)] * 2 + ["x"], "b": {"c": scrub(GH_TOKEN), "d": "x"}, "head_sha": GH_TOKEN}
