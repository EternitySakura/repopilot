"""Static Python definitions, parsed from bounded public bytes and never imported."""

import ast
import io
import tokenize
from hashlib import sha256

from repopilot.contracts.action import SearchSymbolArgs
from repopilot.contracts.tool_data import SearchSymbolData, SymbolFileIssue, SymbolMatch
from repopilot.tools.policy import ReadPolicy, ToolFailure
from repopilot.tools.search import SearchScan


def definitions(tree: ast.AST):
    stack = [(tree, ())]
    while stack:
        node, scope = stack.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "method" if scope and scope[-1][1] == "class" else "function"
            qualified = ".".join([part[0] for part in scope] + [node.name])
            yield node, qualified, kind
            scope = (*scope, (node.name, kind))
        stack.extend((child, scope) for child in reversed(list(ast.iter_child_nodes(node))))


def search_symbol(policy: ReadPolicy, args: SearchSymbolArgs) -> SearchSymbolData:
    if args.limit > policy.profile.max_search_matches:
        raise ToolFailure("INVALID_ARGUMENTS", "Search request exceeds the repository profile limit")
    target = policy.check(args.path)
    if target.is_file() and target.suffix != ".py":
        raise ToolFailure("NOT_PYTHON_FILE", "Symbol search accepts Python files or a public directory")
    scan = SearchScan(policy)
    paths = [args.path] if target.is_file() else scan.paths(args.path)
    symbols, skipped = [], []
    scanned = 0

    def result():
        return SearchSymbolData(symbols=symbols, skipped_files=skipped, files_scanned=scanned,
                                bytes_read=scan.bytes_read, truncated=scan.truncated)

    for path in paths:
        if not path.endswith(".py"):
            continue
        read = scan.read(path)
        if read is None:
            break
        content, clipped = read
        scanned += 1
        if clipped:
            skipped.append(SymbolFileIssue(path=path, code="FILE_TRUNCATED"))
            continue
        try:
            tree = ast.parse(content, filename=path)
            encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
            lines = content.decode(encoding).splitlines()
        except (SyntaxError, UnicodeError) as exc:
            skipped.append(SymbolFileIssue(path=path, code="SYNTAX_ERROR", line=getattr(exc, "lineno", None)))
            continue
        content_hash = sha256(content).hexdigest()
        for node, qualified, kind in definitions(tree):
            if args.query not in {node.name, qualified} or args.kind not in {"any", kind}:
                continue
            if len(symbols) == args.limit:
                scan.truncated = True
                return result()
            symbols.append(SymbolMatch(path=path, name=node.name, qualified_name=qualified, kind=kind,
                line=node.lineno, end_line=node.end_lineno or node.lineno, text=lines[node.lineno - 1],
                is_async=isinstance(node, ast.AsyncFunctionDef), sha256=content_hash))
    return result()
