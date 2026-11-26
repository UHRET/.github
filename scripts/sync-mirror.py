import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

ORG = "oztimurlarmtaluhret"
NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
POINTER = b"version https://git-lfs.github.com/spec/v1"


class SyncError(Exception):
    pass


def git(directory, *args, authenticated=False, input_data=None):
    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false")
    config = ["-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
              "-c", "http.followRedirects=false"]
    if authenticated:
        config += ["-c", "credential.helper=!gh auth git-credential"]
    else:
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
    return subprocess.run(
        ["git", *config, *args], cwd=directory, env=env, input=input_data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def checked(result, action):
    if result.returncode != 0:
        raise SyncError(f"{action} failed (exit {result.returncode}).")
    return result.stdout


def public_repo(name):
    with urlopen(f"https://api.github.com/repos/{name}", timeout=20) as response:
        return json.load(response)


def private_api(path):
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return json.loads(checked(result, "GitHub repository settings lookup"))


def verify_settings(source, destination, entry):
    for repo, name, repo_id in (
        (source, entry["source"], entry["source_id"]),
        (destination, entry["destination"], entry["destination_id"]),
    ):
        if repo.get("full_name") != name or repo.get("id") != repo_id:
            raise SyncError(f"Repository identity mismatch for {name}.")
        if repo.get("visibility") != "public" or repo.get("archived"):
            raise SyncError(f"{name} is not an active public repository.")
    if destination.get("has_pages") is not False:
        raise SyncError("Destination Pages is enabled or undetermined.")
    settings = private_api(f"repos/{entry['destination']}/actions/permissions")
    if settings.get("enabled") is not False:
        raise SyncError("Destination Actions is enabled or undetermined.")
    if source.get("default_branch") != destination.get("default_branch"):
        raise SyncError("Default branches differ.")


def git_url(name):
    return f"https://github.com/{name}.git"


def remote_refs(directory, url, authenticated=False):
    raw = checked(git(directory, "ls-remote", "--refs", url, "refs/heads/*", "refs/tags/*",
                      authenticated=authenticated), "Listing remote branches and tags")
    return {ref: oid for oid, ref in (line.split("\t") for line in raw.decode().splitlines())}


def local_refs(directory):
    raw = checked(git(directory, "for-each-ref", "--format=%(objectname)%09%(refname)",
                      "refs/heads", "refs/tags"), "Listing fetched refs")
    return {ref: oid for oid, ref in (line.split("\t") for line in raw.decode().splitlines())}


def policy_check(directory):
    submodules = checked(git(directory, "log", "--all", "--format=", "--name-only",
                             "--", ".gitmodules", ":(glob)**/.gitmodules"),
                         "Inspecting submodule history")
    if submodules.strip():
        raise SyncError("Submodule history requires separate review and synchronization.")
    oids = checked(git(directory, "rev-list", "--objects", "--all", "--no-object-names"),
                   "Listing reachable Git objects")
    objects = checked(git(directory, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                          input_data=oids), "Inspecting object types and sizes")
    for line in objects.decode().splitlines():
        oid, type_name, size = line.split()
        if type_name != "blob":
            continue
        if int(size) > 100 * 1024 * 1024:
            raise SyncError("A Git blob exceeds 100 MiB.")
        if int(size) <= 1024:
            blob = checked(git(directory, "cat-file", "blob", oid), "Inspecting a small blob")
            if blob.startswith(POINTER):
                raise SyncError("Git LFS pointers found. This version requires a separate verified LFS procedure.")


def updates(directory, source, destination):
    for ref, old in destination.items():
        if ref not in source:
            raise SyncError(f"Destination-only ref {ref}. Investigate it without deleting it.")
        if source[ref] == old:
            continue
        if ref.startswith("refs/tags/"):
            raise SyncError(f"Tag {ref} changed. Investigate it without replacing it.")
        ancestor = git(directory, "merge-base", "--is-ancestor", old, source[ref])
        if ancestor.returncode:
            raise SyncError(f"Ref {ref} is divergent or the previous tip is absent from upstream history.")
    return [f"{ref}:{ref}" for ref, oid in sorted(source.items()) if destination.get(ref) != oid]


def sync(entry):
    source, destination = entry["source"], entry["destination"]
    if not NAME.fullmatch(source) or not NAME.fullmatch(destination):
        raise SyncError("Invalid repository coordinates.")
    if source.split("/")[0].lower() == ORG or not destination.startswith(f"{ORG}/"):
        raise SyncError("Source must be external and destination must be owned by UHRET.")
    if not entry["source_id"] or not entry["destination_id"] or not entry["approved_refs"]:
        raise SyncError("Repository IDs and approved refs are required.")
    up_url, dest_url = git_url(source), git_url(destination)
    up_meta = public_repo(source)
    down_meta = private_api(f"repos/{destination}")
    verify_settings(up_meta, down_meta, entry)
    default_ref = "refs/heads/" + up_meta["default_branch"]
    with tempfile.TemporaryDirectory(prefix="uhret-mirror-") as tmp:
        checked(git(tmp, "init", "--bare"), "Initializing a temporary bare repository")
        approved = entry["approved_refs"]
        current = remote_refs(tmp, up_url)
        if current != approved or default_ref not in current:
            raise SyncError("Upstream refs differ from the reviewed snapshot. Update the review before publishing.")
        existing = remote_refs(tmp, dest_url, authenticated=True)
        fetch_specs = [f"{ref}:{ref}" for ref in sorted(current)]
        checked(git(tmp, "fetch", "--no-tags", "--", up_url, *fetch_specs), "Fetching reviewed refs")
        if local_refs(tmp) != current:
            raise SyncError("Upstream changed during fetch.")
        policy_check(tmp)
        if existing == current:
            return f"verified {len(current)} refs; default branch is {current[default_ref]}"
        changes = updates(tmp, current, existing)
        checked(git(tmp, "push", "--atomic", "--dry-run", "--", dest_url, *changes,
                    authenticated=True), "Atomic push preflight")
        verify_settings(public_repo(source), private_api(f"repos/{destination}"), entry)
        if remote_refs(tmp, up_url) != current or remote_refs(tmp, dest_url, authenticated=True) != existing:
            raise SyncError("Remote refs changed after preflight.")
        checked(git(tmp, "push", "--atomic", "--", dest_url, *changes,
                    authenticated=True), "Atomic non-forcing push")
        if remote_refs(tmp, dest_url, authenticated=True) != current:
            raise SyncError("Post-push verification did not match all approved branches and tags.")
        return f"verified {len(current)} refs; default branch is {current[default_ref]}"


def main(repository):
    config = json.loads(Path("config/mirrors.json").read_text(encoding="utf-8"))
    matches = [row for row in config if row.get("destination") == f"{ORG}/{repository}"]
    if len(matches) != 1 or matches[0].get("enabled") is not True:
        raise SyncError("Destination is missing or disabled.")
    outcome = sync(matches[0])
    summary = f"{ORG}/{repository}: {outcome}"
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as output:
            print(summary, file=output)


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except (SyncError, ValueError, OSError, KeyError, IndexError) as error:
        print(f"Mirror failed: {error}", file=sys.stderr)
        sys.exit(1)
