"""Build a credential-read-only probe for catalog installs.

Remove refresh implementations physically, including indirect CLI and Hermes
OAuth resolvers. Usage HTTP requests and the separate result cache remain.
"""
import ast
import re


def readonly_probe(source):
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    removed = {
        "_write_secret_json", "_kimi_code_refresh_tokens", "_grok_refresh_entry",
        "_note_vendor_refresh", "_vendor_refresh_note", "_begin_secret_write",
        "_end_secret_write", "_wait_for_credential_writes", "_cursor_agent_executable",
    }
    constants = {
        "CREDENTIAL_WRITE_GRACE_SECONDS", "_refresh_writes", "_refresh_writes_lock",
        "_refreshed_vendors", "KIMI_CODE_CLIENT_ID", "KIMI_CODE_OAUTH_TOKEN_URL",
        "GROK_OAUTH_TOKEN_URL", "GROK_OAUTH_CLIENT_ID",
    }
    found = set()
    for node in tree.body:
        original = "".join(lines[node.lineno - 1:node.end_lineno])
        replacement = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
            if name in removed:
                replacement = ""
                found.add(name)
            elif name in ("_kimi_code_access_token", "_grok_access_context"):
                # Keep only the existing file-read and access-token extraction.
                body = original[original.index('    path = ' if name == '_kimi_code_access_token' else '    loaded = '):]
                body = body[:body.index('    if not allow_refresh:')] + '    return None\n'
                annotation = 'Optional[str]' if name == '_kimi_code_access_token' else 'Optional[tuple[str, str]]'
                replacement = f'def {name}(*, previous: Optional[str] = None) -> {annotation}:\n    """Read an existing access token without exchanging or saving credentials."""\n' + body
            elif name == "_hermes_anthropic_oauth_token":
                body = original[original.index('    for home in _hermes_homes():'):]
                replacement = 'def _hermes_anthropic_oauth_token() -> Optional[str]:\n    """Read the saved token; Hermes token resolvers may refresh it."""\n' + body
            elif name == "_cursor_cli_json":
                replacement = 'def _cursor_cli_json(args: list[str]) -> Optional[dict]:\n    """Do not launch a CLI that may refresh its login as a side effect."""\n    return None\n'
            elif name in ("_fetch_kimi_cli_usage", "_fetch_grok_account_usage"):
                vendor = "Kimi" if name == "_fetch_kimi_cli_usage" else "Grok"
                start = original.index('        if response.status_code == 401:')
                end = original.index('        response.raise_for_status()', start)
                replacement = original[:start] + f'        if response.status_code == 401:\n            raise RuntimeError("{vendor} login expired. Sign in with the {vendor} CLI, then refresh usage. Catalog installs never refresh login tokens.")\n' + original[end:]
                if vendor == "Kimi":
                    replacement = replacement[:replacement.index('    note = _vendor_refresh_note')] + '    return snap\n'
                else:
                    replacement = replacement[:replacement.index('    note = _vendor_refresh_note')] + '    return _snapshot("grok", plan, windows)\n'
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if names & constants:
                replacement = ""
            elif "HERMES_PROVIDERS" in names:
                replacement = '# Codex is read directly below; the Hermes resolver can refresh OAuth.\nHERMES_PROVIDERS = ("openrouter",)\n'
        if replacement is not None:
            edits.append((node.lineno - 1, node.end_lineno, replacement))
    if found != removed:
        raise ValueError("Credential helpers changed; review read-only packaging: " + repr(removed - found))
    for start, end, replacement in reversed(edits):
        lines[start:end] = [replacement]
    result = "".join(lines)
    result = re.sub(r'(?m)^ *\_wait_for_credential_writes\(\)\n', '', result)
    result = result.replace('import threading\n', '')
    result = result.replace('# Credential write grace: wait briefly before os._exit so a finishing\n# Kimi/Grok refresh can land on disk.\n', '')
    result = result.replace('# Vendors whose login file this run rewrote after a token refresh. Their\n# cards carry a note so a later CLI sign-out is not a mystery.\n', '')
    result = re.sub(r'\n{4,}', '\n\n\n', result)
    # Replace the standalone contract so the shipped documentation is accurate.
    end = result.index('"""', 3) + 3
    result = '''"""Read-only catalog usage probe for Resetwatch.

Reads existing CLI and Hermes access tokens and calls vendor usage APIs. Never
exchanges refresh tokens or writes login files. Expired Kimi/Grok credentials
produce an error asking the user to sign in with the vendor CLI. Cursor CLI
commands and Hermes OAuth resolvers are not invoked. Result/rate-limit caches
under $HERMES_HOME/cache/resetwatch may be written. No tokens on stdout.

Private vendor APIs are best-effort and may change without notice.
"""''' + result[end:]
    for forbidden in (*removed, *constants, 'allow_refresh', 'resolve_anthropic_token', 'grant_type'):
        if forbidden in result:
            raise ValueError("Read-only probe contains refresh code: " + forbidden)
    ast.parse(result)
    return result
