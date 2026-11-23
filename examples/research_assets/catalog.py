"""A standalone, versioned research asset catalog for the Chem-Agent demo."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

Kind = Literal["calculation", "script", "analysis"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
MAX_FILE_BYTES = 64 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    seq INTEGER PRIMARY KEY,
    revision TEXT UNIQUE NOT NULL,
    owner TEXT NOT NULL,
    slug TEXT NOT NULL,
    manifest TEXT NOT NULL,
    reviewer TEXT
);
CREATE TABLE IF NOT EXISTS files (
    revision TEXT REFERENCES revisions(revision),
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES blobs(sha256),
    PRIMARY KEY (revision, name)
);
CREATE TABLE IF NOT EXISTS inputs (
    revision TEXT REFERENCES revisions(revision),
    upstream TEXT REFERENCES revisions(revision),
    PRIMARY KEY (revision, upstream)
);
CREATE TABLE IF NOT EXISTS grants (
    revision TEXT REFERENCES revisions(revision),
    recipient TEXT NOT NULL,
    PRIMARY KEY (revision, recipient)
);
CREATE VIRTUAL TABLE IF NOT EXISTS asset_search USING fts5(
    title, description, keywords, tokenize='unicode61'
);
"""

# A derived revision is readable only while every upstream revision is readable.
# The seed of the recursive query is already restricted to the caller's grants.
ACCESS = """
WITH RECURSIVE directly_visible(revision) AS (
    SELECT revision FROM revisions WHERE owner = :group
    UNION
    SELECT revision FROM grants WHERE recipient = :group
), ancestry(root, upstream) AS (
    SELECT inputs.revision, inputs.upstream FROM inputs
    JOIN directly_visible ON directly_visible.revision = inputs.revision
    UNION
    SELECT ancestry.root, inputs.upstream FROM ancestry
    JOIN inputs ON inputs.revision = ancestry.upstream
), visible(revision) AS (
    SELECT candidate.revision FROM directly_visible AS candidate
    WHERE NOT EXISTS (
        SELECT 1 FROM ancestry
        WHERE root = candidate.revision
        AND upstream NOT IN (SELECT revision FROM directly_visible)
    )
)
"""


class AssetUnavailable(LookupError):
    """The revision or file is absent or outside the caller's scope."""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Contract):
    node: Name
    record_id: str = Field(min_length=1)
    record_type: Literal["job", "workspace", "analysis_run"]


class Method(Contract):
    engine: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    functional: str = Field(min_length=1)
    comparison_key: str = Field(min_length=1)


class Energy(Contract):
    metric: Literal["TOTEN", "energy_without_entropy"]
    value: FiniteFloat
    unit: Literal["eV"] = "eV"


class Calculation(Contract):
    formula: str = Field(min_length=1)
    method: Method
    energy: Energy
    scheduler_state: Literal["completed", "running", "failed"]
    electronic_converged: bool
    ionic_converged: bool


class Script(Contract):
    entry_point: str
    runtime: str = Field(min_length=1)
    dependencies: tuple[str, ...]
    invocation: tuple[str, ...]
    input_contract: str = Field(min_length=1)


class Asset(Contract):
    slug: Name
    kind: Kind
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    keywords: tuple[str, ...] = ()
    source: Source
    calculation: Calculation | None = None
    script: Script | None = None
    inputs: tuple[Digest, ...] = ()

    @model_validator(mode="after")
    def check_kind(self) -> "Asset":
        if (self.kind == "calculation") != (self.calculation is not None):
            raise ValueError("calculation metadata must match the asset kind")
        if (self.kind == "script") != (self.script is not None):
            raise ValueError("script metadata must match the asset kind")
        if self.kind == "analysis" and not self.inputs:
            raise ValueError("analysis requires fixed input revisions")
        return self


class Search(Contract):
    text: str = Field(default="", max_length=300)
    kind: Kind | None = None
    formula: str | None = None
    functional: str | None = None
    comparison_key: str | None = None
    energy_metric: Literal["TOTEN", "energy_without_entropy"] | None = None
    approved_only: bool = True
    limit: int = Field(default=10, ge=1, le=30)


class Inspect(Contract):
    revision: Digest


class Fetch(Inspect):
    name: str


class Catalog:
    def __init__(self, database: Path) -> None:
        self.database = database
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def register(self, owner: str, asset: Asset, files: Mapping[str, bytes]) -> str:
        """Commit metadata, upstream links and content-addressed files together."""
        if not files or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", name)
            or len(content) > MAX_FILE_BYTES
            for name, content in files.items()
        ):
            raise ValueError("use simple filenames and files no larger than 64 KiB")
        if asset.script and asset.script.entry_point not in files:
            raise ValueError("script entry point is missing")
        if asset.calculation and "CONTCAR" not in files:
            raise ValueError("calculation requires a CONTCAR snapshot")
        digests = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
        parents = sorted(set(asset.inputs))
        manifest = asset.model_dump(mode="json") | {
            "schema_version": 1,
            "owner": owner,
            "inputs": parents,
            "files": [
                {"name": name, "sha256": digests[name], "size": len(files[name])}
                for name in sorted(files)
            ],
        }
        encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        revision = hashlib.sha256(encoded.encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for parent in parents:
                self._authorized(db, owner, parent)
            cursor = db.execute(
                "INSERT INTO revisions(revision, owner, slug, manifest) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(revision) DO NOTHING",
                (revision, owner, asset.slug, encoded),
            )
            if cursor.rowcount == 0:
                return revision
            db.executemany(
                "INSERT OR IGNORE INTO blobs VALUES (?, ?)",
                [(digests[name], content) for name, content in files.items()],
            )
            db.executemany(
                "INSERT INTO files VALUES (?, ?, ?)",
                [(revision, name, digest) for name, digest in digests.items()],
            )
            db.executemany("INSERT INTO inputs VALUES (?, ?)", [(revision, p) for p in parents])
            db.execute(
                "INSERT INTO asset_search(rowid, title, description, keywords) VALUES (?, ?, ?, ?)",
                (cursor.lastrowid, asset.title, asset.description, " ".join(asset.keywords)),
            )
        return revision

    def approve(self, owner: str, revision: str, reviewer: str) -> None:
        """Record an explicit review through the owner-side demo intake."""
        if not reviewer.strip():
            raise ValueError("a reviewer is required")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned(db, owner, revision)
            self._authorized(db, owner, revision)
            manifest = json.loads(row["manifest"])
            calculation = manifest["calculation"]
            if calculation and (
                calculation["scheduler_state"] != "completed"
                or not calculation["electronic_converged"]
                or not calculation["ionic_converged"]
            ):
                raise ValueError("optimization evidence does not qualify for approval")
            for parent in manifest["inputs"]:
                if not self._authorized(db, owner, parent)["reviewer"]:
                    raise ValueError("upstream revision requires review")
            db.execute(
                "UPDATE revisions SET reviewer = ? WHERE revision = ? AND reviewer IS NULL",
                (reviewer, revision),
            )

    def share(self, owner: str, revision: str, recipient: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned(db, owner, revision)
            self._authorized(db, owner, revision)
            if not row["reviewer"]:
                raise ValueError("only reviewed revisions may be shared")
            for parent in json.loads(row["manifest"])["inputs"]:
                self._authorized(db, recipient, parent)
            db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?)", (revision, recipient))

    def revoke(self, owner: str, revision: str, recipient: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, owner, revision)
            db.execute(
                "DELETE FROM grants WHERE revision = ? AND recipient = ?", (revision, recipient)
            )

    @staticmethod
    def _owned(db: sqlite3.Connection, owner: str, revision: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM revisions WHERE owner = ? AND revision = ?", (owner, revision)
        ).fetchone()
        if row is None:
            raise AssetUnavailable("asset revision or file is unavailable")
        return row

    @staticmethod
    def _authorized(db: sqlite3.Connection, group: str, revision: str) -> sqlite3.Row:
        row = db.execute(
            ACCESS + " SELECT * FROM revisions JOIN visible USING (revision) WHERE revision = :id",
            {"group": group, "id": revision},
        ).fetchone()
        if row is None:
            raise AssetUnavailable("asset revision or file is unavailable")
        return row

    @staticmethod
    def _describe(row: sqlite3.Row) -> dict:
        return json.loads(row["manifest"]) | {
            "asset_id": f"{row['owner']}/{row['slug']}",
            "revision": row["revision"],
            "review_status": "approved" if row["reviewer"] else "draft",
            "reviewer": row["reviewer"],
        }

    def search(self, group: str, query: Search) -> list[dict]:
        fields = {
            "kind": "$.kind",
            "formula": "$.calculation.formula",
            "functional": "$.calculation.method.functional",
            "comparison_key": "$.calculation.method.comparison_key",
            "energy_metric": "$.calculation.energy.metric",
        }
        criteria = [
            f"json_extract(manifest, '{field}') = :{key}"
            for key, field in fields.items()
            if getattr(query, key) is not None
        ]
        if query.approved_only:
            criteria.append("reviewer IS NOT NULL")
        terms = re.findall(r"[^\W_]+", query.text)
        if query.text.strip() and not terms:
            return []
        match = " AND ".join(f'"{term}"' for term in terms)
        join_fts = " JOIN asset_search ON asset_search.rowid = revisions.seq" if match else ""
        if match:
            criteria.append("asset_search MATCH :match")
        where = f" WHERE {' AND '.join(criteria)}" if criteria else ""
        order = "bm25(asset_search), " if match else ""
        sql = (
            ACCESS
            + " SELECT revisions.* FROM revisions JOIN visible USING (revision)"
            + join_fts
            + where
            + f" ORDER BY {order}seq DESC LIMIT :limit"
        )
        params = query.model_dump() | {"group": group, "match": match}
        with closing(self.connect()) as db:
            rows = db.execute(sql, params).fetchall()
        hits = []
        for row in rows:
            asset = self._describe(row)
            hits.append(
                {
                    key: asset[key]
                    for key in (
                        "asset_id",
                        "revision",
                        "kind",
                        "title",
                        "description",
                        "calculation",
                        "review_status",
                    )
                }
            )
        return hits

    def inspect(self, group: str, revision: str) -> dict:
        with closing(self.connect()) as db:
            return self._describe(self._authorized(db, group, revision))

    def fetch(self, group: str, revision: str, name: str) -> tuple[bytes, str]:
        """Read an authorized, reviewed payload under the same SQLite snapshot."""
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            if not self._authorized(db, group, revision)["reviewer"]:
                raise AssetUnavailable("asset revision or file is unavailable")
            row = db.execute(
                "SELECT content, sha256 FROM files JOIN blobs USING (sha256) "
                "WHERE revision = ? AND name = ?",
                (revision, name),
            ).fetchone()
        if row is None:
            raise AssetUnavailable("asset revision or file is unavailable")
        if hashlib.sha256(row["content"]).hexdigest() != row["sha256"]:
            raise ValueError("stored content failed SHA-256 verification")
        return row["content"], row["sha256"]
