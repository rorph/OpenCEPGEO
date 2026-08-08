from __future__ import annotations

import hashlib
from pathlib import Path

from . import __version__


def builder_identity() -> dict[str, str]:
    """Return an identity for the exact installed builder implementation.

    The digest covers every Python source file in the installed package using
    normalized relative names and raw bytes. It intentionally excludes the
    repository location and timestamps so copied source trees are identical.
    """

    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for source in sorted(package_root.glob("*.py"), key=lambda path: path.name):
        name = source.name.encode("utf-8")
        payload = source.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return {
        "name": "opencepgeo",
        "version": __version__,
        "source_tree_sha256": digest.hexdigest(),
    }
