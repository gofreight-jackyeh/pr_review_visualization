from __future__ import annotations

EXTENSIONS = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rb": "ruby", ".java": "java", ".kt": "kotlin", ".rs": "rust", ".php": "php",
    ".cs": "csharp", ".swift": "swift", ".scala": "scala", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".md": "markdown", ".rst": "restructuredtext", ".txt": "text",
    ".html": "html", ".css": "css", ".scss": "css", ".json": "json", ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml", ".sql": "sql", ".sh": "shell",
}
NAMES = {"Dockerfile": "dockerfile", "Makefile": "make", "LICENSE": "text", ".env": "dotenv"}


def detect_language(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in NAMES:
        return NAMES[name]
    if name.startswith(".env"):
        return "dotenv"
    dot = name.rfind(".")
    return EXTENSIONS.get(name[dot:].lower(), "unknown") if dot > 0 else "unknown"
