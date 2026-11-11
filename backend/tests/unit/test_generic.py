from pr_explorer.infrastructure.analyzers.generic.diff_parser import parse_unified_diff
from pr_explorer.infrastructure.analyzers.generic.generic_analyzer import extract_imports, resolve_specifier
from pr_explorer.infrastructure.analyzers.generic.languages import detect_language

DIFF = """diff --git a/app/x.py b/app/x.py
index 1..2 100644
--- a/app/x.py
+++ b/app/x.py
@@ -1,3 +1,4 @@
 a
-b
+c
+d
diff --git a/new.md b/new.md
new file mode 100644
--- /dev/null
+++ b/new.md
@@ -0,0 +1 @@
+hello
diff --git a/old.go b/old.go
deleted file mode 100644
--- a/old.go
+++ /dev/null
@@ -1 +0,0 @@
-package x
diff --git a/a.txt b/b.txt
similarity index 90%
rename from a.txt
rename to b.txt
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


def test_parse_unified_diff():
    files = {f.path: f for f in parse_unified_diff(DIFF)}
    assert (files["app/x.py"].status, files["app/x.py"].additions, files["app/x.py"].deletions) == ("modified", 2, 1)
    assert files["app/x.py"].hunks[0].new_lines == 4
    assert files["new.md"].status == "added" and files["new.md"].language == "markdown"
    assert files["old.go"].status == "removed" and files["old.go"].language == "go"
    assert files["b.txt"].status == "renamed" and files["b.txt"].old_path == "a.txt"
    assert files["img.png"].is_binary


def test_regex_imports_and_language():
    src = 'import x from "./util";\nconst y = require("../lib/y");\n#include "z.h"\n'
    assert [s for s, _ in extract_imports(src)] == ["./util", "../lib/y", "z.h"]
    assert resolve_specifier("./util", "src/a.js", {"src/util.js"}) == "src/util.js"
    assert detect_language("x/.env.production") == "dotenv" and detect_language("Dockerfile") == "dockerfile"
