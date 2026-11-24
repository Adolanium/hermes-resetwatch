"""Build a credential-read-only probe for catalog installs.

Remove refresh implementations physically, including indirect CLI and Hermes
OAuth resolvers. Usage HTTP requests and the separate result cache remain.
"""
import ast
import re

# Hermes-resolved providers whose catalog read path never refreshes a token.
CATALOG_RESOLVER_PROVIDERS = ("nous", "openrouter")


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
                    replacement = replacement[:replacement.index('    note = _vendor_refresh_note')] + '    return _parse_grok_billing(payload, settings, None)\n'
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if names & constants:
                replacement = ""
            elif "HERMES_PROVIDERS" in names:
                # Allowlist, not "source minus Codex": a provider reaches catalog
                # installs only after its resolver is reviewed as read-only.
                # Codex is read directly; its Hermes resolver can refresh OAuth.
                kept = tuple(provider for provider in ast.literal_eval(node.value)
                             if provider in CATALOG_RESOLVER_PROVIDERS)
                replacement = (
                    '# Codex is read directly below; the Hermes resolver can refresh OAuth.\n'
                    f'HERMES_PROVIDERS = {kept!r}\n'
                )
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

Reads existing CLI and Hermes access tokens (through Hermes's read-only
credential helpers) and calls vendor usage APIs. Never exchanges refresh
tokens, never writes login files, and never opens Hermes credential files
directly. Expired Kimi/Grok credentials produce an error asking the user to sign in with the vendor CLI. Cursor CLI commands and
refreshing Hermes OAuth resolvers are not invoked. Result/rate-limit caches
under $HERMES_HOME/cache/resetwatch may be written. No tokens on stdout.

Private vendor APIs are best-effort and may change without notice.
"""''' + result[end:]
    for forbidden in (*removed, *constants, 'allow_refresh', 'resolve_anthropic_token', 'grant_type',
                      '_fetch_portal_account', 'get_nous_portal_account_info', 'resolve_nous_access_token',
                      '.anthropic_oauth.json', '_read_env_file_value', '_read_auth_store', '_pool_rows_from_saved_store'):
        if forbidden in result:
            raise ValueError("Read-only probe contains forbidden code: " + forbidden)
    parsed = ast.parse(result)
    for node in ast.walk(parsed):
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if callee == "resolve_codex_runtime_credentials":
                read_only_kw = [kw for kw in node.keywords if kw.arg == "read_only"]
                if node.args or len(read_only_kw) != 1 or not (
                    isinstance(read_only_kw[0].value, ast.Constant) and read_only_kw[0].value.value is True
                ):
                    raise ValueError("resolve_codex_runtime_credentials must be called with read_only=True")
    return result
