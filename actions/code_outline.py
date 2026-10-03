"""code_outline — structural symbol outline of source code.

Three honest tiers, never a silent failure:

1. **tree-sitter AST** (when the grammar for the language is installed)
   — real syntax trees for Python, JS/TS, C/C++, Java, Go, Rust, …
2. **stdlib `ast`** for Python when tree-sitter is absent — Python
   parsing is IN the standard library, so .py never depends on it.
3. **Approximate scanner** for brace languages without their grammar —
   line-based function/class detection, LABELLED approximate (never
   pretends to be an AST).

tree-sitter and the grammar packs are optional imports: the tool
registers and works (tier 2/3) without them, and says exactly what to
pip install to get tier 1.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from tree_sitter import Language as _TSLanguage
    from tree_sitter import Parser as _TSParser
    _HAS_TS = True
except Exception:                              # pragma: no cover
    _HAS_TS = False
    _TSLanguage = None
    _TSParser = None

_MAX_SYMBOLS = 400
_MAX_LINES = 40_000

_LANG_BY_EXT = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".java": "java", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".swift": "swift",
}

# tree-sitter grammar package per language (pip install <package>)
_GRAMMAR_PACKS = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "java": "tree_sitter_java",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
}

# node types that ARE symbols, mapped to a display keyword
_TS_SYMBOLS = {
    "function_definition": "def", "function_declaration": "def",
    "method_definition": "def", "method_declaration": "def",
    "class_definition": "class", "class_declaration": "class",
    "interface_declaration": "interface", "struct_item": "struct",
    "impl_item": "impl", "trait_item": "trait",
    "arrow_function": None,        # only interesting via assignment
}
_TS_IMPORTS = {
    "import_statement", "import_from_statement", "import_declaration",
    "import_header", "use_declaration", "preproc_include",
}


def _grammar(lang: str):
    """Language object for a tree-sitter grammar pack, or None."""
    if not _HAS_TS:
        return None
    pkg = _GRAMMAR_PACKS.get(lang)
    if not pkg:
        return None
    try:
        import importlib
        module = importlib.import_module(pkg)
        return _TSLanguage(module.language())
    except Exception:
        return None


def _ts_outline(code: bytes, lang: str) -> list[dict] | None:
    grammar = _grammar(lang)
    if grammar is None:
        return None
    try:
        parser = _TSParser(grammar)
        tree = parser.parse(code)
    except Exception:
        return None
    symbols: list[dict] = []
    imports: list[dict] = []

    def walk(node, depth: int):
        if len(symbols) >= _MAX_SYMBOLS:
            return
        for child in node.children:
            kind = child.type
            if kind in _TS_SYMBOLS:
                kw = _TS_SYMBOLS[kind]
                if kw:
                    name_node = child.child_by_field_name("name")
                    name = (name_node.text.decode("utf-8", "replace")
                            if name_node is not None else "?")
                    symbols.append({
                        "kw": kw, "name": name, "depth": depth,
                        "line": child.start_point[0] + 1,
                    })
                    walk(child, depth + 1)
                    continue
            elif kind in _TS_IMPORTS:
                text = child.text.decode("utf-8", "replace").strip()
                imports.append({
                    "line": child.start_point[0] + 1,
                    "text": " ".join(text.split())[:80],
                })
            walk(child, depth)

    walk(tree.root_node, 0)
    return {"symbols": symbols, "imports": imports, "engine": "tree-sitter"}


def _ast_outline_py(code: str) -> dict | None:
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    symbols: list[dict] = []
    imports: list[dict] = []

    def walk(node, depth: int):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                symbols.append({
                    "kw": "class" if isinstance(child, ast.ClassDef)
                          else "def",
                    "name": child.name, "depth": depth,
                    "line": child.lineno,
                })
                walk(child, depth + 1)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                if isinstance(child, ast.Import):
                    names = [a.name for a in child.names]
                else:
                    base = child.module or ""
                    names = [f"{base}.{a.name}" if base else a.name
                             for a in child.names]
                imports.append({"line": child.lineno,
                                "text": ", ".join(names)[:80]})
            else:
                walk(child, depth)

    walk(tree, 0)
    return {"symbols": symbols, "imports": imports, "engine": "stdlib-ast"}


_CONTROL = re.compile(
    r"^\s*(if|else|for|while|switch|catch|try|do|return|with|case)\b")


def _approx_outline(code: str) -> dict:
    """Line-based scanner for brace languages — LABELLED approximate."""
    symbols: list[dict] = []
    imports: list[dict] = []
    patterns = [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?"
                    r"(?:async\s+)?function\s+(\w+)"), "def"),
        (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"),
         "class"),
        (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface"),
        (re.compile(r"^\s*(?:export\s+)?"
                    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("),
         "def"),
        (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("), "def"),
        (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|"
                    r"final\s+)*class\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|"
                    r"(?:\w+\s+)+)(\w+)\s*\([^;]*\)\s*\{?\s*$"), "def"),
    ]
    import_re = re.compile(
        r"^\s*(?:import\s+[^;]+;?|from\s+[\w.]+\s+import\s+.+|"
        r"#include\s+[<\"][^>\"]+[>\"]|use\s+[\w:]+;?)\s*$")
    depth = 0
    for i, raw in enumerate(code.splitlines(), 1):
        if len(symbols) >= _MAX_SYMBOLS:
            break
        line = raw.strip()
        if import_re.match(line):
            imports.append({"line": i, "text": line[:80]})
            continue
        matched = False
        if not _CONTROL.match(raw):
            for rx, kw in patterns:
                m = rx.match(raw)
                if m:
                    symbols.append({"kw": kw, "name": m.group(1),
                                    "depth": max(0, depth), "line": i})
                    matched = True
                    break
        # rough depth from brace balance (approximate mode only)
        depth += raw.count("{") - raw.count("}")
        depth = max(0, depth)
        _ = matched
    return {"symbols": symbols, "imports": imports, "engine": "approx"}


def outline(source: str, language: str | None = None) -> dict:
    """Core: returns {engine, symbols, imports, language, note?}."""
    language = (language or "").lower().strip() or None
    code = source
    if language is None:
        return {"engine": "none", "symbols": [], "imports": [],
                "language": None,
                "note": "Unknown language — pass language=python|javascript"
                        "|typescript|c|cpp|java|go|rust …"}
    if language not in set(_GRAMMAR_PACKS) | set(_LANG_BY_EXT.values()):
        return {"engine": "none", "symbols": [], "imports": [],
                "language": language,
                "note": f"Language '{language}' is not recognised — "
                        f"supported: "
                        f"{', '.join(sorted(set(_GRAMMAR_PACKS) | set(_LANG_BY_EXT.values())))} "
                        f"(or omit language= to auto-detect from the file "
                        f"extension)."}

    if language == "python":
        result = _ts_outline(code.encode("utf-8"), "python")
        if result is None:
            result = _ast_outline_py(code)
        if result is None:
            return {"engine": "error", "symbols": [], "imports": [],
                    "language": language,
                    "note": "Python file has a syntax error — outline "
                            "aborted honestly rather than guessing."}
        result["language"] = language
        return result

    result = _ts_outline(code.encode("utf-8"), language)
    if result is not None:
        result["language"] = language
        return result
    approx = _approx_outline(code)
    approx["language"] = language
    approx["note"] = (f"No tree-sitter grammar for '{language}' — symbols "
                      f"below are an APPROXIMATE line scan. Install one for "
                      f"an exact AST: pip install tree-sitter "
                      f"{_GRAMMAR_PACKS.get(language, 'tree-sitter-' + language)}")
    return approx


def _render(res: dict, title: str, max_line: int) -> str:
    engine = res.get("engine")
    lang = res.get("language")
    if engine in {"none", "error"}:
        return res.get("note", "Cannot outline this input.")
    head = {
        "tree-sitter": f"{title} — {lang} (tree-sitter AST)",
        "stdlib-ast": f"{title} — {lang} (Python stdlib ast)",
        "approx": f"{title} — {lang} (APPROXIMATE scan)",
    }.get(engine, f"{title} — {lang}")
    syms = res.get("symbols") or []
    imps = res.get("imports") or []
    lines = [f"{head}  [{max_line} lines, {len(syms)} symbols]"]
    for s in syms:
        indent = "  " * (1 + min(int(s.get("depth", 0)), 6))
        lines.append(f"{indent}L{s['line']:<4} {s['kw']} {s['name']}")
    if not syms:
        lines.append("  (no top-level symbols found)")
    if imps:
        shown = ", ".join(f"L{i['line']} {i['text']}" for i in imps[:8])
        more = f" … +{len(imps) - 8}" if len(imps) > 8 else ""
        lines.append(f"  imports: {shown}{more}")
    if res.get("note"):
        lines.append(f"  note: {res['note']}")
    return "\n".join(lines)


def code_outline(parameters: dict, ctx: dict | None = None) -> str:
    parameters = parameters or {}
    language = parameters.get("language") or None
    code_param = parameters.get("code")
    file_path = parameters.get("file_path") or parameters.get("path")

    if code_param:
        code = str(code_param)
        if len(code) > 4_000_000:
            return "Code too large to outline (4 MB cap)."
        res = outline(code, language=language)
        return _render(res, "inline code", code.count("\n") + 1)

    if file_path:
        p = Path(str(file_path)).expanduser()
        if not p.is_file():
            return f"No such file: {p}"
        try:
            if p.stat().st_size > 8_000_000:
                return f"File too large ({p.stat().st_size} bytes, 8 MB cap)."
            code = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Cannot read {p}: {e}"
        if language is None:
            language = _LANG_BY_EXT.get(p.suffix.lower())
            if language is None:
                return (f"Unknown extension '{p.suffix}' for {p.name} — "
                        f"pass language= explicitly (python, javascript, "
                        f"c, java, go, rust, …).")
        res = outline(code, language=language)
        return _render(res, str(p), code.count("\n") + 1)

    return ("code_outline needs either file_path=/path/to/file.py or "
            "code='…' (+ optional language=python|javascript|…).")


TOOL = {
    "name": "code_outline",
    "description": (
        "Structural symbol outline of source code — functions, classes, "
        "methods, imports with line numbers and nesting. Exact tree-sitter "
        "AST when the grammar is installed, Python stdlib ast for .py "
        "otherwise, honest approximate line-scan (labelled) for other "
        "languages. Args: file_path (or path) OR inline code=, optional "
        "language=. Use before explaining unfamiliar codebases or to map a "
        "file's structure quickly."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {"type": "STRING",
                          "description": "File to outline (path)"},
            "code": {"type": "STRING",
                     "description": "Inline source instead of a file"},
            "language": {
                "type": "STRING",
                "description": "python | javascript | typescript | c | cpp "
                               "| java | go | rust … (auto from extension "
                               "for files)",
            },
        },
        "required": [],
    },
    "handler": code_outline,
}
