import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ORG = "uhret"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"
    rb"|(?:gh[pousr]_[A-Za-z0-9]{35,}|github_pat_[A-Za-z0-9_]{50,})"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{18,}"
    rb"|sk_live_[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{35}"
)
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
REVIEW_FILE = re.compile(r"^(?:copying|licen[cs]e|notice|copyright|attribution|sources)(?:[._-].*)?$", re.I)
MAX_RATE_LIMIT_WAIT = 120


class SyncError(Exception):
    pass


def report(message):
    print(message)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as output:
            print(f"    {message}", file=output)


def git(directory, *args, authenticated=False, data=None):
    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false", GH_PROMPT_DISABLED="1")
    options = ["-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
               "-c", "http.followRedirects=false"]
    if authenticated:
        options += ["-c", "credential.helper=!gh auth git-credential"]
    else:
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
            env.pop(key, None)
    return subprocess.run(
        ["git", *options, *args], cwd=directory, env=env, input=data,
        capture_output=True, timeout=240, check=False,
    )


def checked(result, description):
    if result.returncode:
        raise SyncError(f"{description} failed (exit {result.returncode}).")
    return result.stdout


def read_git(directory, description, *args, authenticated=False):
    for attempt in range(2):
        try:
            result = git(directory, *args, authenticated=authenticated)
        except subprocess.TimeoutExpired:
            if attempt:
                raise SyncError(f"{description} timed out.") from None
        else:
            if not result.returncode:
                return result.stdout
            transient = re.search(
                rb"timed out|connection reset|resolve host|HTTP (?:429|50[0234])",
                result.stderr, re.I,
            )
            if attempt or not transient:
                return checked(result, description)
        time.sleep(2)


def public_api_rate_limit(error):
    if error.code not in (403, 429):
        return None
    retry_after = error.headers.get("Retry-After", "")
    primary = error.headers.get("X-RateLimit-Remaining") == "0"
    if not primary and not retry_after and error.code == 403:
        message = error.read(4096).lower()
        if b"secondary rate limit" not in message and b"rate limit exceeded" not in message:
            return None
    delay = max(1, int(retry_after)) if retry_after.isdecimal() else 60
    if primary:
        reset = error.headers.get("X-RateLimit-Reset", "")
        if not reset.isdecimal():
            return "primary", None
        reset_delay = math.ceil(int(reset) - time.time()) + 1
        return "primary", max(delay if retry_after else 1, reset_delay)
    return "secondary or unspecified", delay


def public_api(path):
    for attempt in range(2):
        delay = 2
        try:
            with urlopen(f"https://api.github.com/{path}", timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                rate_limit = public_api_rate_limit(error)
            if rate_limit:
                kind, delay = rate_limit
                message = f"GitHub public API {kind} rate limit (HTTP {error.code})"
                if attempt:
                    raise SyncError(f"{message} persisted after one retry.") from None
                if delay is None or delay > MAX_RATE_LIMIT_WAIT:
                    raise SyncError(f"{message}; the retry window is unknown or exceeds {MAX_RATE_LIMIT_WAIT}s.") from None
                report(f"{message}; retrying once after {delay}s.")
            elif attempt or error.code not in (500, 502, 503, 504):
                raise SyncError(f"GitHub public API returned HTTP {error.code}; check visibility and access.") from None
        except (URLError, TimeoutError):
            if attempt:
                raise SyncError("GitHub public API was unreachable after one retry.") from None
        time.sleep(delay)


def private_api(path):
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", path],
        env=os.environ | {"GH_PROMPT_DISABLED": "1"},
        capture_output=True, timeout=40, check=False,
    )
    return json.loads(checked(result, "GitHub App access; check installation, key and repository permissions"))


def verify_settings(entry):
    source = public_api(f"repos/{entry['source']}")
    destination = private_api(f"repos/{entry['destination']}")
    for repo, role in ((source, "source"), (destination, "destination")):
        if repo.get("full_name") != entry[role] or repo.get("id") != entry[f"{role}_id"]:
            raise SyncError(f"Unexpected {role} repository identity.")
        if repo.get("visibility") != "public" or repo.get("archived") or repo.get("disabled"):
            raise SyncError(f"The {role} must be an active public repository.")
    expected_parent = (entry["source"], entry["source_id"])
    parent = destination.get("parent", {})
    if destination.get("fork") is not True or (parent.get("full_name"), parent.get("id")) != expected_parent:
        raise SyncError("Destination is no longer a native fork of the configured upstream.")
    if destination.get("has_pages") is not False:
        raise SyncError("Destination Pages is enabled or undetermined.")
    if public_api(f"repos/{entry['destination']}/deployments?per_page=1") != []:
        raise SyncError("Destination has a deployment record. Check hosting integrations.")
    actions = private_api(f"repos/{entry['destination']}/actions/permissions")
    if actions.get("enabled") is not False:
        raise SyncError("Destination Actions is enabled or undetermined.")
    if not source.get("default_branch") or source["default_branch"] != destination.get("default_branch"):
        raise SyncError("Default branch settings differ.")
    return "refs/heads/" + source["default_branch"]


def git_url(name):
    return f"https://github.com/{name}.git"


def refs(raw):
    result = {}
    for line in raw.splitlines():
        oid, ref = line.split(b"\t")
        if SECRET.search(ref):
            raise SyncError("Possible credential in a Git reference name; name omitted.")
        result[ref.decode()] = oid.decode()
    return result


def remote_refs(directory, url, authenticated=False):
    raw = read_git(directory, "Listing remote branches and tags", "ls-remote", "--refs",
                   url, "refs/heads/*", "refs/tags/*", authenticated=authenticated)
    return refs(raw)


def local_refs(directory):
    result = git(directory, "for-each-ref", "--format=%(objectname)%09%(refname)",
                 "refs/heads", "refs/tags")
    return refs(checked(result, "Listing fetched refs"))


def get_updates(directory, source, destination):
    changes = {}
    for ref, new in sorted(source.items()):
        old = destination.get(ref)
        if old == new:
            continue
        if old and ref.startswith("refs/tags/"):
            raise SyncError(f"Upstream replaced {ref}. Review the tag; its destination value was retained.")
        if old and git(directory, "merge-base", "--is-ancestor", old, new).returncode:
            raise SyncError(f"Divergence or upstream rewrite at {ref}. Its destination value was retained.")
        changes[ref] = (old, new)
    return changes


def inspect_notices(directory, baseline, changes):
    for ref, (old, new) in changes.items():
        if not ref.startswith("refs/heads/"):
            continue
        if not old and baseline:
            common = git(directory, "merge-base", baseline, new)
            old = common.stdout.decode().strip() if not common.returncode else None
        if not old:
            continue
        result = git(directory, "diff", "--no-ext-diff", "--name-only", "--no-renames",
                     "--diff-filter=DMT", "-z", old, new)
        paths = checked(result, "Checking existing notice files").decode(errors="replace").split("\0")
        if any(REVIEW_FILE.fullmatch(PurePosixPath(path).name) or
               PurePosixPath(path).name in (".gitmodules", ".lfsconfig") for path in paths):
            raise SyncError(f"A license, attribution, source notice or Git integration changed on {ref}. Review before publishing.")


def inspect_incoming(directory, changes, already_synced):
    tips = list(dict.fromkeys(new for _, new in changes.values()))
    baseline = list(dict.fromkeys(already_synced))
    revisions = "\n".join([*tips, *(f"^{oid}" for oid in baseline)]).encode() + b"\n"
    series = git(directory, "rev-list", "--objects", "--no-object-names", "--stdin", data=revisions)
    objects = git(directory, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                  data=checked(series, "Listing incoming Git objects"))
    for line in checked(objects, "Reading incoming Git object metadata").decode().splitlines():
        oid, kind, size = line.split()
        if kind == "tree":
            tree = checked(git(directory, "ls-tree", "-z", oid), "Checking incoming trees")
            if any(item.startswith(b"160000 ") for item in tree.split(b"\0")):
                raise SyncError("New submodule entries require a separate dependency review.")
            continue
        if kind not in ("blob", "commit", "tag"):
            continue
        if int(size) > 100 * 1024 * 1024:
            raise SyncError("An incoming Git blob exceeds 100 MiB.")
        payload = checked(git(directory, "cat-file", kind, oid), "Scanning incoming Git objects")
        if kind == "blob" and payload.startswith(LFS_POINTER):
            raise SyncError("New Git LFS content needs an object availability and quota review.")
        if SECRET.search(payload):
            raise SyncError("Possible credential in incoming Git objects; evidence intentionally omitted.")


def sync(entry, dry_run=False):
    source, destination = entry["source"], entry["destination"]
    if (not REPOSITORY.fullmatch(source) or not REPOSITORY.fullmatch(destination)
            or source.split("/")[0].lower() == ORG or not destination.startswith(f"{ORG}/")
            or not entry["source_id"] or not entry["destination_id"]):
        raise SyncError("Invalid source or destination coordinates.")
    default = verify_settings(entry)
    source_url, destination_url = git_url(source), git_url(destination)
    with tempfile.TemporaryDirectory(prefix="uhret-fork-sync-") as directory:
        checked(git(directory, "init", "--bare"), "Creating a temporary bare Git repository")
        upstream = remote_refs(directory, source_url)
        before = remote_refs(directory, destination_url, authenticated=True)
        if default not in upstream or default not in before:
            raise SyncError("The configured default branch is missing.")
        if entry["mode"] == "snapshot" and upstream != entry.get("approved_refs"):
            raise SyncError("Upstream no longer matches the optional reviewed snapshot.")
        if entry["mode"] not in ("automatic", "snapshot"):
            raise SyncError("Unsupported synchronization mode.")
        report(f"Default before: {before[default]}; upstream: {upstream[default]}")
        retained = sorted(before.keys() - upstream.keys())
        unchanged = sorted(ref for ref, oid in upstream.items() if before.get(ref) == oid)
        for ref in retained:
            report(f"Retained destination-only ref {ref} at {before[ref]}.")
        for ref in unchanged:
            report(f"Unchanged {ref} at {before[ref]}.")
        if len(unchanged) == len(upstream):
            return f"Verified {len(upstream)} upstream branches/tags; default {before[default]}."
        specs = [f"{ref}:{ref}" for ref in sorted(upstream)]
        read_git(directory, "Fetching the upstream snapshot", "fetch", "--no-tags", "--", source_url, *specs)
        if local_refs(directory) != upstream:
            raise SyncError("Upstream moved during fetch; retry on the next run.")
        updates = get_updates(directory, upstream, before)
        inspect_notices(directory, before[default], updates)
        synced = (oid for ref, oid in before.items() if ref in upstream)
        inspect_incoming(directory, updates, synced)
        for ref, (old, new) in updates.items():
            report(f"Planned {ref}: {old or 'new'} → {new}")
        if dry_run:
            return f"Dry run verified {len(updates)} proposed updates; default would be {upstream[default]}."
        if verify_settings(entry) != default:
            raise SyncError("Default branch changed after preflight.")
        if (remote_refs(directory, source_url) != upstream
                or remote_refs(directory, destination_url, authenticated=True) != before):
            raise SyncError("References changed after preflight; retry on the next run.")
        refspecs = [f"{ref}:{ref}" for ref in updates]
        try:
            pushed = git(directory, "push", "--porcelain", "--", destination_url, *refspecs,
                         authenticated=True).returncode == 0
        except subprocess.TimeoutExpired:
            pushed = False
        after = remote_refs(directory, destination_url, authenticated=True)
        mismatched = [ref for ref, oid in upstream.items() if after.get(ref) != oid]
        for ref, (old, new) in updates.items():
            if ref not in mismatched:
                report(f"Synchronized {ref}: {old or 'new'} → {new}")
        if mismatched:
            raise SyncError(f"{len(mismatched)} expected ref updates were not verified. Check GitHub permissions, rules and connection.")
        if not pushed:
            report("Git push did not report success, but all expected destination refs were verified.")
        return f"Verified {len(upstream)} upstream branches/tags after push; default {after[default]}."


def main(argv):
    entries = json.loads(Path("config/mirrors.json").read_text(encoding="utf-8"))
    matches = [row for row in entries if row["destination"] == f"{ORG}/{argv[1]}"]
    if len(matches) != 1 or matches[0]["mode"] == "disabled":
        raise SyncError("Destination is missing, duplicated or disabled.")
    entry = matches[0]
    report(f"{entry['source']} → {entry['destination']}")
    return sync(entry, os.environ.get("SYNC_DRY_RUN") == "true")


if __name__ == "__main__":
    status = 0
    try:
        message = main(sys.argv)
    except (SyncError, KeyError, IndexError, ValueError, OSError, subprocess.SubprocessError) as error:
        explanation = str(error) if isinstance(error, SyncError) else type(error).__name__
        message, status = f"Synchronization stopped: {explanation}", 1
    report(message)
    sys.exit(status)
