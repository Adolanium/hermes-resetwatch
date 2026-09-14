"""Remove self-update code from packages installed through the pinned catalog."""
import re


def cut(source, start, end, keep_end=True):
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("Updater structure changed; review catalog packaging")
    first, last = source.index(start), source.index(end)
    if first >= last:
        raise ValueError("Unexpected updater section order")
    return source[:first] + source[last if keep_end else last + len(end):]


def strip_updater(source, mode):
    if mode == "shared":
        source = cut(source, "// BEGIN SIGNED DESKTOP UPDATER", "// END SIGNED DESKTOP UPDATER", False)
        source = cut(source, "const UPDATE_KEY =", "function Page(")
        source, panels = re.subn(r"jsx\(desktopUpdater\.Panel, \{\}\)", "null", source)
        source, registrations = re.subn(r"(?m)^ *desktopUpdater\.register\(ctx\);?\n", "", source)
        if (panels, registrations) != (1, 1):
            raise ValueError("Updater UI changed; review catalog packaging")
    elif mode == "ssh":
        source = cut(source, "// Public verification key only.", 'const ROUTE = "/ssh-connections";')
        source = cut(source, "function UpdateControls(", "export default {")
        source, panels = re.subn(r"jsx\(UpdateControls, \{ disabled: !!s\.busy \|\| s\.panel \}\)", "null", source)
        source, registrations = re.subn(r"(?m)^ *loadUpdateBackup\(\);\n", "", source)
        if (panels, registrations) != (1, 1):
            raise ValueError("SSH updater UI changed; review catalog packaging")
        for symbol in ("UPDATE_KEY", "updateState", "newerVersion", "verifyRelease", "fetchUpdateText", "replacePlugin", "runUpdate", "dismissUpdate", "UpdateControls"):
            source, count = re.subn(r"(?m)^  " + symbol + r",\n", "", source)
            if count != 1:
                raise ValueError("SSH updater exports changed: " + symbol)
    elif mode != "none":
        raise ValueError("Unknown updater mode: " + mode)
    forbidden = ("desktopUpdater", "createDesktopUpdater", "UPDATE_KEY", "UpdateControls", "runUpdate", "loadUpdateBackup", "Check for updates", "releases/latest", "release-manifest.json")
    if mode != "none" and any(word in source for word in forbidden):
        raise ValueError("Catalog package still contains self-update code")
    return source
