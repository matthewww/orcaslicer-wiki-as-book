"""Decide whether the release workflow should build a new edition of the books.

One edition per OrcaSlicer minor version (2.3, 2.4, ...), built once that minor has settled: no new
patch release for SETTLE_DAYS, so the post-release fix burst has passed and the wiki has caught up.
A long-lived minor gets a refresh when a new patch lands and its current edition is REFRESH_DAYS old.

    python release_check.py      # in GitHub Actions: writes version=/build= to $GITHUB_OUTPUT
"""
import datetime
import json
import os
import re
import subprocess

SETTLE_DAYS = 21
REFRESH_DAYS = 90
STABLE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")  # some old -beta tags are marked stable; ignore them


def pick(app_releases, editions, today):
    """app_releases: [(tag, date)] from OrcaSlicer. editions: [(tag, date)] already published here,
    tagged wiki-<app tag>. Returns the app tag to build, or None."""
    stable = sorted(((t, d) for t, d in app_releases if STABLE.match(t)), key=lambda r: r[1])
    if not stable:
        return None
    latest, released = stable[-1]
    if (today - released).days < SETTLE_DAYS:
        return None
    minor = latest.rsplit(".", 1)[0]  # "v2.4"
    same_minor = [(t, d) for t, d in editions if t.startswith(f"wiki-{minor}.")]
    if not same_minor:
        return latest
    newest_tag, newest_date = max(same_minor, key=lambda e: e[1])
    if newest_tag != f"wiki-{latest}" and (today - newest_date).days >= REFRESH_DAYS:
        return latest
    return None


def gh_list(endpoint):
    """All items of a paginated GitHub list endpoint (--slurp wraps the pages in one array)."""
    out = subprocess.run(["gh", "api", "--paginate", "--slurp", endpoint], capture_output=True, encoding="utf-8", check=True).stdout
    return [item for page in json.loads(out) for item in page]


def main():
    date = lambda s: datetime.date.fromisoformat(s[:10])
    app = [(r["tag_name"], date(r["published_at"])) for r in gh_list("repos/OrcaSlicer/OrcaSlicer/releases")
           if not r["draft"] and not r["prerelease"]]
    ours = [(r["tag_name"], date(r["published_at"])) for r in gh_list(f"repos/{os.environ['GITHUB_REPOSITORY']}/releases")]
    version = pick(app, ours, datetime.date.today())
    print(f"Build edition for {version}." if version else "No new edition due.")
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"version={version or ''}\nbuild={'true' if version else 'false'}\n")


if __name__ == "__main__":
    main()
