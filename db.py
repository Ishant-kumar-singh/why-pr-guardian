"""FalkorDB connection, schema and small query helpers.

Only this module talks to the database. Everything else calls ``run`` /
``run_one`` which take parameterised Cypher (never string-formatted values).
"""

from __future__ import annotations

from typing import Any

from falkordb import FalkorDB
from falkordb.edge import Edge
from falkordb.node import Node

import config

# Label -> property used to build stable node ids in traces.
ID_PROP = {
    "Person": "login",
    "Commit": "sha",
    "PullRequest": "number",
    "Issue": "number",
    "File": "path",
    "Decision": "id",
}

# Property used for a human title in traces / UI.
TITLE_PROP = {
    "Person": "login",
    "Commit": "message",
    "PullRequest": "title",
    "Issue": "title",
    "File": "path",
    "Decision": "summary",
}

_graph = None


def graph():
    """Return a lazy singleton Graph handle."""
    global _graph
    if _graph is None:
        db = FalkorDB(host=config.FALKORDB_HOST, port=config.FALKORDB_PORT)
        _graph = db.select_graph(config.FALKORDB_GRAPH)
    return _graph


def run(cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
    """Run a parameterised query and return raw rows."""
    return graph().query(cypher, params or {}).result_set


def run_one(cypher: str, params: dict[str, Any] | None = None) -> list[Any] | None:
    rows = run(cypher, params)
    return rows[0] if rows else None


def init_schema() -> None:
    """Create indexes idempotently (FalkorDB ignores duplicate index creates)."""
    stmts = [
        "CREATE INDEX FOR (f:File) ON (f.path)",
        "CREATE INDEX FOR (p:PullRequest) ON (p.number)",
        "CREATE INDEX FOR (i:Issue) ON (i.number)",
        "CREATE INDEX FOR (p:Person) ON (p.login)",
        "CREATE INDEX FOR (c:Commit) ON (c.sha)",
        "CREATE INDEX FOR (d:Decision) ON (d.id)",
    ]
    for stmt in stmts:
        try:
            graph().query(stmt)
        except Exception as err:  # already exists -- FalkorDB raises, we ignore
            if "already" not in str(err).lower():
                raise
    # Full-text index powers fuzzy find_file.
    try:
        graph().query("CALL db.idx.fulltext.createNodeIndex('File', 'path')")
    except Exception as err:  # pragma: no cover - depends on server state
        if "already" not in str(err).lower() and "exists" not in str(err).lower():
            raise
    for label, properties in (
        ("Decision", ("summary", "rationale")),
        ("PullRequest", ("title", "body")),
        ("Issue", ("title", "body")),
    ):
        try:
            graph().query(f"CALL db.idx.fulltext.createNodeIndex('{label}', {', '.join(repr(p) for p in properties)})")
        except Exception as err:
            if "already" not in str(err).lower() and "exists" not in str(err).lower():
                raise


def node_type(node: Node) -> str:
    return node.labels[0] if node.labels else "Node"


def node_id(node: Node) -> str:
    t = node_type(node)
    key = node.properties.get(ID_PROP.get(t, "id"), node.id)
    return f"{t}:{key}"


def node_title(node: Node) -> str:
    t = node_type(node)
    return str(node.properties.get(TITLE_PROP.get(t, "id"), node_id(node)))


def _node_trace(node: Node) -> dict[str, Any]:
    return {
        "id": node_id(node),
        "db_id": node.id,
        "type": node_type(node),
        "label": node_title(node),
        "props": dict(node.properties),
    }


def collect(rows: list[list[Any]]) -> tuple[list[dict], list[dict]]:
    """Extract nodes and edges from a raw result set for the trace.

    FalkorDB reports edge endpoints as integer node ids, not Node objects, so
    we first index every node in the result set by its db id and then resolve
    the edges against that map.
    """
    nodes: dict[int, dict] = {}
    for row in rows:
        for value in row:
            if isinstance(value, Node):
                nodes[value.id] = _node_trace(value)

    edges: dict[tuple, dict] = {}
    for row in rows:
        for value in row:
            if isinstance(value, Edge):
                src = nodes.get(value.src_node)
                dst = nodes.get(value.dest_node)
                if not src or not dst:
                    continue
                key = (src["id"], dst["id"], value.relation)
                edges[key] = {
                    "source": src["id"],
                    "target": dst["id"],
                    "type": value.relation,
                    "props": value.properties,
                }
    return list(nodes.values()), list(edges.values())


def stats() -> dict[str, Any]:
    """Node and relationship counts by label/type."""
    node_rows = run("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c")
    rel_rows = run("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c")
    by_label = {r[0]: r[1] for r in node_rows}
    by_type = {r[0]: r[1] for r in rel_rows}
    return {
        "nodes": sum(by_label.values()),
        "edges": sum(by_type.values()),
        "nodes_by_label": by_label,
        "edges_by_type": by_type,
        "graph": config.FALKORDB_GRAPH,
    }


def ping() -> bool:
    try:
        run("RETURN 1")
        return True
    except Exception:
        return False


def clear_graph() -> None:
    graph().query("MATCH (n) DETACH DELETE n")
