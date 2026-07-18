from pathlib import Path
import re


VERSIONS_DIR = Path("migrations/versions")


def _revision_value(text: str, name: str) -> str | None:
    match = re.search(rf'^{name}:.*?=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else None


def test_alembic_revision_chain_is_linear_and_complete() -> None:
    files = sorted(VERSIONS_DIR.glob("*.py"))
    assert files, "No Alembic revisions found"

    revisions: dict[str, str | None] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        revision = _revision_value(text, "revision")
        assert revision, f"Missing revision in {path}"
        assert revision not in revisions, f"Duplicate revision {revision}"
        revisions[revision] = _revision_value(text, "down_revision")
        assert "def upgrade()" in text
        assert "def downgrade()" in text

    roots = [revision for revision, parent in revisions.items() if parent is None]
    assert len(roots) == 1

    for revision, parent in revisions.items():
        if parent is not None:
            assert parent in revisions, f"Revision {revision} points to missing parent {parent}"

    children: dict[str, list[str]] = {revision: [] for revision in revisions}
    for revision, parent in revisions.items():
        if parent is not None:
            children[parent].append(revision)

    assert all(len(items) <= 1 for items in children.values()), "Alembic chain contains a branch"
    assert "20260718_0004" in revisions
    assert revisions["20260718_0004"] == "20260718_0003"
