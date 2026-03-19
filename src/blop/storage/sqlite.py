from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from blop.config import BLOP_DB_PATH
from blop.schemas import (
    AuthProfile,
    FailureCase,
    IncidentCluster,
    RecordedFlow,
    ReleaseSnapshot,
    RemediationDraft,
    SiteContextGraph,
    TelemetrySignal,
)


def _db_path() -> str:
    return os.environ.get("BLOP_DB_PATH", BLOP_DB_PATH)


async def init_db() -> None:
    path = _db_path()
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auth_profiles (
                profile_name TEXT PRIMARY KEY,
                auth_type TEXT NOT NULL,
                config_json TEXT NOT NULL,
                storage_state_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                refreshed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recorded_flows (
                flow_id TEXT PRIMARY KEY,
                flow_name TEXT NOT NULL,
                app_url TEXT NOT NULL,
                goal TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                assertions_json TEXT,
                entry_url TEXT,
                spa_hints_json TEXT,
                run_mode_override TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                profile_name TEXT,
                flow_ids_json TEXT,
                status TEXT DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT,
                headless INTEGER DEFAULT 1,
                artifacts_dir TEXT,
                cases_json TEXT,
                run_mode TEXT DEFAULT 'hybrid'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS run_cases (
                case_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                flow_id TEXT NOT NULL,
                status TEXT,
                severity TEXT,
                result_json TEXT,
                replay_mode TEXT,
                step_failure_index INTEGER,
                assertion_failures_json TEXT,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT,
                artifact_type TEXT,
                path TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS site_inventories (
                id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                crawled_at TEXT DEFAULT (datetime('now')),
                inventory_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS execution_plans (
                plan_id TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                intent_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS context_graphs (
                graph_id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                profile_name TEXT,
                archetype TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                graph_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS run_health_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS release_snapshots (
                release_id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                snapshot_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS incident_clusters (
                cluster_id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                first_seen TEXT,
                last_seen TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                cluster_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS remediation_drafts (
                cluster_id TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                draft_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_signals (
                signal_id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                source TEXT,
                ts TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                journey_key TEXT,
                route TEXT,
                value REAL NOT NULL,
                unit TEXT,
                tags_json TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS correlation_reports (
                report_id TEXT PRIMARY KEY,
                app_url TEXT NOT NULL,
                window TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                report_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            )
        """)
        # Telemetry index for faster time-range queries
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ts_signals ON telemetry_signals(app_url, ts)"
        )
        await db.commit()

        # Migrate existing tables to add new columns if missing
        await _migrate(db)

        # Startup recovery: mark any runs orphaned in "running" state as "failed"
        # (happens when the server process was killed mid-run)
        await db.execute(
            "UPDATE runs SET status = 'failed', completed_at = datetime('now') "
            "WHERE status = 'running'"
        )
        await db.commit()


async def _migrate(db) -> None:
    """Add columns to existing tables that may predate schema additions.

    Each migration has a version number. Only migrations above the stored
    schema_version are applied, and the version is bumped after success.
    """
    _VERSIONED_MIGRATIONS: list[tuple[int, str, str, str]] = [
        # (version, table, column, col_type)
        (1, "recorded_flows", "assertions_json", "TEXT"),
        (2, "recorded_flows", "entry_url", "TEXT"),
        (3, "recorded_flows", "spa_hints_json", "TEXT"),
        (4, "recorded_flows", "business_criticality", "TEXT DEFAULT 'other'"),
        (5, "recorded_flows", "run_mode_override", "TEXT"),
        (6, "runs", "run_mode", "TEXT DEFAULT 'hybrid'"),
        (7, "run_cases", "replay_mode", "TEXT"),
        (8, "run_cases", "step_failure_index", "INTEGER"),
        (9, "run_cases", "assertion_failures_json", "TEXT"),
        (10, "run_cases", "business_criticality", "TEXT DEFAULT 'other'"),
        (11, "auth_profiles", "user_data_dir", "TEXT"),
        (12, "context_graphs", "profile_name", "TEXT"),
        (13, "context_graphs", "archetype", "TEXT"),
    ]

    current_version = await _get_schema_version(db)

    for version, table, column, col_type in _VERSIONED_MIGRATIONS:
        if version <= current_version:
            continue
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception:
            pass  # Column already exists
        await _set_schema_version(db, version)

    await db.commit()


async def save_auth_profile(profile: AuthProfile, storage_state_path: str | None = None) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO auth_profiles
            (profile_name, auth_type, config_json, storage_state_path, refreshed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (
                profile.profile_name,
                profile.auth_type,
                profile.model_dump_json(),
                storage_state_path,
            ),
        )
        await db.commit()


async def get_auth_profile(profile_name: str) -> AuthProfile | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT config_json FROM auth_profiles WHERE profile_name = ?",
            (profile_name,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return AuthProfile.model_validate_json(row[0])
    return None


async def save_flow(flow: RecordedFlow) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO recorded_flows
            (flow_id, flow_name, app_url, goal, steps_json, created_at, assertions_json, entry_url,
             spa_hints_json, business_criticality, run_mode_override)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                flow.flow_id,
                flow.flow_name,
                flow.app_url,
                flow.goal,
                json.dumps([s.model_dump() for s in flow.steps]),
                flow.created_at,
                json.dumps(flow.assertions_json),
                flow.entry_url,
                flow.spa_hints.model_dump_json() if getattr(flow, "spa_hints", None) else None,
                flow.business_criticality,
                flow.run_mode_override,
            ),
        )
        await db.commit()


async def get_flow(flow_id: str) -> RecordedFlow | None:
    from blop.schemas import FlowStep, SpaHints
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """SELECT flow_id, flow_name, app_url, goal, steps_json, created_at,
                      assertions_json, entry_url, spa_hints_json, business_criticality, run_mode_override
               FROM recorded_flows WHERE flow_id = ?""",
            (flow_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                steps_data = json.loads(row[4])
                steps = []
                for s in steps_data:
                    # Handle old records that may lack new fields
                    s.setdefault("target_text", None)
                    s.setdefault("dom_fingerprint", None)
                    s.setdefault("url_before", None)
                    s.setdefault("url_after", None)
                    s.setdefault("screenshot_path", None)
                    # Semantic locator fields (added in v2)
                    s.setdefault("aria_role", None)
                    s.setdefault("aria_name", None)
                    s.setdefault("aria_snapshot", None)
                    s.setdefault("testid_selector", None)
                    s.setdefault("label_text", None)
                    s.setdefault("structured_assertion", None)
                    steps.append(FlowStep(**s))

                assertions_json: list[str] = []
                if row[6]:
                    try:
                        assertions_json = json.loads(row[6])
                    except Exception:
                        pass
                spa_hints = SpaHints()
                if row[8]:
                    try:
                        spa_hints = SpaHints.model_validate_json(row[8])
                    except Exception:
                        pass

                return RecordedFlow(
                    flow_id=row[0],
                    flow_name=row[1],
                    app_url=row[2],
                    goal=row[3],
                    steps=steps,
                    created_at=row[5],
                    assertions_json=assertions_json,
                    entry_url=row[7],
                    spa_hints=spa_hints,
                    business_criticality=row[9] or "other",
                    run_mode_override=row[10] if len(row) > 10 else None,
                )
    return None


async def list_flows() -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT flow_id, flow_name, app_url, goal, created_at, run_mode_override FROM recorded_flows ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "flow_id": r[0],
                    "flow_name": r[1],
                    "app_url": r[2],
                    "goal": r[3],
                    "created_at": r[4],
                    "run_mode_override": r[5] if len(r) > 5 else None,
                }
                for r in rows
            ]


async def create_run(
    run_id: str,
    app_url: str,
    profile_name: str | None,
    flow_ids: list[str],
    headless: bool,
    artifacts_dir: str,
    run_mode: str = "hybrid",
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO runs (run_id, app_url, profile_name, flow_ids_json, status, started_at, headless, artifacts_dir, run_mode)
            VALUES (?, ?, ?, ?, 'queued', datetime('now'), ?, ?, ?)
            """,
            (
                run_id,
                app_url,
                profile_name,
                json.dumps(flow_ids),
                1 if headless else 0,
                artifacts_dir,
                run_mode,
            ),
        )
        await db.commit()


async def update_run_status(run_id: str, status: str) -> None:
    """Update only the status of a run (lightweight state-machine transition)."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (status, run_id),
        )
        await db.commit()


async def update_run(run_id: str, status: str, cases: list[FailureCase], completed_at: str | None = None) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            UPDATE runs SET status = ?, cases_json = ?, completed_at = ?
            WHERE run_id = ?
            """,
            (
                status,
                json.dumps([c.model_dump() for c in cases]),
                completed_at,
                run_id,
            ),
        )
        await db.commit()


async def get_run(run_id: str) -> dict | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """SELECT run_id, app_url, profile_name, flow_ids_json, status,
                      started_at, completed_at, headless, artifacts_dir, cases_json, run_mode
               FROM runs WHERE run_id = ?""",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "run_id": row[0],
                    "app_url": row[1],
                    "profile_name": row[2],
                    "flow_ids": json.loads(row[3]) if row[3] else [],
                    "status": row[4],
                    "started_at": row[5],
                    "completed_at": row[6],
                    "headless": bool(row[7]),
                    "artifacts_dir": row[8] or "",
                    "cases": json.loads(row[9]) if row[9] else [],
                    "run_mode": row[10] or "hybrid",
                }
    return None


async def list_runs(limit: int = 20, status: str | None = None) -> list[dict]:
    safe_limit = max(1, min(limit, 200))
    async with aiosqlite.connect(_db_path()) as db:
        if status:
            query = """SELECT run_id, app_url, profile_name, status, started_at, completed_at,
                              headless, artifacts_dir, run_mode
                       FROM runs
                       WHERE status = ?
                       ORDER BY started_at DESC
                       LIMIT ?"""
            params = (status, safe_limit)
        else:
            query = """SELECT run_id, app_url, profile_name, status, started_at, completed_at,
                              headless, artifacts_dir, run_mode
                       FROM runs
                       ORDER BY started_at DESC
                       LIMIT ?"""
            params = (safe_limit,)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "run_id": row[0],
                    "app_url": row[1],
                    "profile_name": row[2],
                    "status": row[3],
                    "started_at": row[4],
                    "completed_at": row[5],
                    "headless": bool(row[6]),
                    "artifacts_dir": row[7] or "",
                    "run_mode": row[8] or "hybrid",
                }
                for row in rows
            ]


async def save_case(case: FailureCase) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO run_cases
            (case_id, run_id, flow_id, status, severity, result_json,
             replay_mode, step_failure_index, assertion_failures_json, business_criticality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case.case_id,
                case.run_id,
                case.flow_id,
                case.status,
                case.severity,
                case.model_dump_json(),
                case.replay_mode,
                case.step_failure_index,
                json.dumps(case.assertion_failures),
                case.business_criticality,
            ),
        )
        await db.commit()


async def list_cases_for_run(run_id: str) -> list[FailureCase]:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT result_json FROM run_cases WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cases = []
            for row in rows:
                try:
                    cases.append(FailureCase.model_validate_json(row[0]))
                except Exception:
                    pass
            return cases


async def get_case(case_id: str) -> FailureCase | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT result_json FROM run_cases WHERE case_id = ?",
            (case_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return FailureCase.model_validate_json(row[0])
            except Exception:
                return None


async def save_artifact(run_id: str, case_id: str | None, artifact_type: str, path: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO artifacts (artifact_id, run_id, case_id, artifact_type, path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), run_id, case_id, artifact_type, path),
        )
        await db.commit()


async def save_site_inventory(app_url: str, inventory_dict: dict) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO site_inventories (id, app_url, inventory_json)
            VALUES (?, ?, ?)
            """,
            (str(uuid.uuid4()), app_url, json.dumps(inventory_dict)),
        )
        await db.commit()


async def get_latest_site_inventory(app_url: str) -> dict | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT id, app_url, crawled_at, inventory_json
            FROM site_inventories
            WHERE app_url = ?
            ORDER BY crawled_at DESC
            LIMIT 1
            """,
            (app_url,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                inventory = json.loads(row[3])
            except Exception:
                inventory = {}
            return {
                "id": row[0],
                "app_url": row[1],
                "crawled_at": row[2],
                "inventory": inventory,
            }


async def list_artifacts_for_run(run_id: str) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT artifact_id, run_id, case_id, artifact_type, path, created_at
            FROM artifacts
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "artifact_id": row[0],
                    "run_id": row[1],
                    "case_id": row[2],
                    "artifact_type": row[3],
                    "path": row[4],
                    "created_at": row[5],
                }
                for row in rows
            ]


async def list_cases_for_flow(flow_id: str, limit: int = 50) -> list[FailureCase]:
    safe_limit = max(1, min(limit, 500))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT result_json
            FROM run_cases
            WHERE flow_id = ?
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (flow_id, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cases: list[FailureCase] = []
            for row in rows:
                try:
                    cases.append(FailureCase.model_validate_json(row[0]))
                except Exception:
                    pass
            return cases


async def save_execution_plan(plan_id: str, intent_dict: dict) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO execution_plans (plan_id, intent_json)
            VALUES (?, ?)
            """,
            (plan_id, json.dumps(intent_dict)),
        )
        await db.commit()


async def save_context_graph(graph: SiteContextGraph) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO context_graphs
            (graph_id, app_url, profile_name, archetype, created_at, graph_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                graph.graph_id,
                graph.app_url,
                graph.profile_name,
                graph.archetype,
                graph.created_at,
                graph.model_dump_json(),
            ),
        )
        await db.commit()


async def get_latest_context_graph(app_url: str, profile_name: str | None = None) -> SiteContextGraph | None:
    async with aiosqlite.connect(_db_path()) as db:
        if profile_name:
            query = """
                SELECT graph_json
                FROM context_graphs
                WHERE app_url = ? AND profile_name = ?
                ORDER BY created_at DESC
                LIMIT 1
            """
            params = (app_url, profile_name)
        else:
            query = """
                SELECT graph_json
                FROM context_graphs
                WHERE app_url = ?
                ORDER BY created_at DESC
                LIMIT 1
            """
            params = (app_url,)

        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return SiteContextGraph.model_validate_json(row[0])
                except Exception:
                    return None
    return None


async def get_context_graph(graph_id: str) -> SiteContextGraph | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT graph_json FROM context_graphs WHERE graph_id = ?",
            (graph_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return SiteContextGraph.model_validate_json(row[0])
                except Exception:
                    return None
    return None


async def list_context_graphs(app_url: str, limit: int = 5) -> list[dict]:
    safe_limit = max(1, min(limit, 100))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT graph_id, app_url, profile_name, archetype, created_at
            FROM context_graphs
            WHERE app_url = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (app_url, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "graph_id": row[0],
                    "app_url": row[1],
                    "profile_name": row[2],
                    "archetype": row[3],
                    "created_at": row[4],
                }
                for row in rows
            ]


async def save_run_health_event(run_id: str, event_type: str, payload: dict) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO run_health_events (event_id, run_id, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                run_id,
                event_type,
                json.dumps(payload),
            ),
        )
        await db.commit()


async def list_run_health_events(run_id: str, limit: int = 500) -> list[dict]:
    safe_limit = max(1, min(limit, 2000))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT event_id, run_id, event_type, payload_json, created_at
            FROM run_health_events
            WHERE run_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (run_id, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            out: list[dict] = []
            for row in rows:
                try:
                    payload = json.loads(row[3])
                except Exception:
                    payload = {}
                out.append(
                    {
                        "event_id": row[0],
                        "run_id": row[1],
                        "event_type": row[2],
                        "payload": payload,
                        "created_at": row[4],
                    }
                )
            return out


async def save_release_snapshot(snapshot: ReleaseSnapshot) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO release_snapshots
            (release_id, app_url, created_at, snapshot_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                snapshot.release_id,
                snapshot.app_url,
                snapshot.created_at,
                snapshot.model_dump_json(),
            ),
        )
        await db.commit()


async def get_release_snapshot(release_id: str) -> ReleaseSnapshot | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT snapshot_json FROM release_snapshots WHERE release_id = ?",
            (release_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return ReleaseSnapshot.model_validate_json(row[0])
            except Exception:
                return None


async def list_release_snapshots(app_url: str, limit: int = 20) -> list[dict]:
    safe_limit = max(1, min(limit, 200))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT release_id, app_url, created_at
            FROM release_snapshots
            WHERE app_url = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (app_url, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "release_id": row[0],
                    "app_url": row[1],
                    "created_at": row[2],
                }
                for row in rows
            ]


async def save_incident_cluster(cluster: IncidentCluster) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO incident_clusters
            (cluster_id, app_url, status, first_seen, last_seen, cluster_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cluster.cluster_id,
                cluster.app_url,
                cluster.status,
                cluster.first_seen,
                cluster.last_seen,
                cluster.model_dump_json(),
            ),
        )
        await db.commit()


async def get_incident_cluster(cluster_id: str) -> IncidentCluster | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT cluster_json FROM incident_clusters WHERE cluster_id = ?",
            (cluster_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return IncidentCluster.model_validate_json(row[0])
            except Exception:
                return None


async def list_open_incident_clusters(app_url: str, limit: int = 100) -> list[IncidentCluster]:
    safe_limit = max(1, min(limit, 500))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT cluster_json
            FROM incident_clusters
            WHERE app_url = ? AND status = 'open'
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (app_url, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            clusters: list[IncidentCluster] = []
            for row in rows:
                try:
                    clusters.append(IncidentCluster.model_validate_json(row[0]))
                except Exception:
                    pass
            return clusters


async def save_remediation_draft(draft: RemediationDraft) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO remediation_drafts
            (cluster_id, created_at, draft_json)
            VALUES (?, ?, ?)
            """,
            (
                draft.cluster_id,
                draft.created_at,
                draft.model_dump_json(),
            ),
        )
        await db.commit()


async def get_remediation_draft(cluster_id: str) -> RemediationDraft | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT draft_json FROM remediation_drafts WHERE cluster_id = ?",
            (cluster_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return RemediationDraft.model_validate_json(row[0])
            except Exception:
                return None


async def save_telemetry_signals(signals: list[TelemetrySignal]) -> tuple[int, int]:
    ingested = 0
    rejected = 0
    async with aiosqlite.connect(_db_path()) as db:
        for signal in signals:
            try:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO telemetry_signals
                    (signal_id, app_url, source, ts, signal_type, journey_key, route, value, unit, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.signal_id,
                        signal.app_url,
                        signal.source,
                        signal.ts,
                        signal.signal_type,
                        signal.journey_key,
                        signal.route,
                        signal.value,
                        signal.unit,
                        json.dumps(signal.tags),
                    ),
                )
                ingested += 1
            except Exception:
                rejected += 1
        await db.commit()
    return ingested, rejected


async def list_telemetry_signals(app_url: str, limit: int = 1000) -> list[dict]:
    safe_limit = max(1, min(limit, 5000))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT signal_id, app_url, source, ts, signal_type, journey_key, route, value, unit, tags_json
            FROM telemetry_signals
            WHERE app_url = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (app_url, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            out: list[dict] = []
            for row in rows:
                try:
                    tags = json.loads(row[9]) if row[9] else {}
                except Exception:
                    tags = {}
                out.append(
                    {
                        "signal_id": row[0],
                        "app_url": row[1],
                        "source": row[2],
                        "ts": row[3],
                        "signal_type": row[4],
                        "journey_key": row[5],
                        "route": row[6],
                        "value": row[7],
                        "unit": row[8],
                        "tags": tags,
                    }
                )
            return out


async def save_correlation_report(app_url: str, window: str, report: dict) -> str:
    report_id = str(uuid.uuid4())
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO correlation_reports (report_id, app_url, window, report_json)
            VALUES (?, ?, ?, ?)
            """,
            (report_id, app_url, window, json.dumps(report)),
        )
        await db.commit()
    return report_id


async def _get_schema_version(db) -> int:
    try:
        async with db.execute("SELECT version FROM schema_version LIMIT 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


async def _set_schema_version(db, version: int) -> None:
    await db.execute("DELETE FROM schema_version")
    await db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


async def list_cases_for_flow_since(flow_id: str, since_iso: str, limit: int = 500) -> list[FailureCase]:
    """Return cases for a flow where the parent run started at or after since_iso."""
    safe_limit = max(1, min(limit, 500))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT rc.result_json FROM run_cases rc
            JOIN runs r ON rc.run_id = r.run_id
            WHERE rc.flow_id = ? AND r.started_at >= ?
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            (flow_id, since_iso, safe_limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cases: list[FailureCase] = []
            for row in rows:
                try:
                    cases.append(FailureCase.model_validate_json(row[0]))
                except Exception:
                    pass
            return cases


async def archive_old_runs(older_than_days: int = 30, keep_failed: bool = True) -> dict:
    """Delete runs (and associated cases/artifacts/events) older than older_than_days.

    If keep_failed is True, failed runs are retained regardless of age.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        if keep_failed:
            async with db.execute(
                "SELECT run_id FROM runs WHERE started_at < ? AND status != 'failed'",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT run_id FROM runs WHERE started_at < ?",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()

        run_ids = [r[0] for r in rows]
        for run_id in run_ids:
            await db.execute("DELETE FROM run_cases WHERE run_id = ?", (run_id,))
            await db.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
            await db.execute("DELETE FROM run_health_events WHERE run_id = ?", (run_id,))
            await db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        await db.commit()

    return {
        "archived_runs": len(run_ids),
        "cutoff": cutoff,
        "kept_failed": keep_failed,
    }


async def archive_old_telemetry(older_than_days: int = 90) -> dict:
    """Delete telemetry signals older than older_than_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM telemetry_signals WHERE ts < ?",
            (cutoff,),
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
        await db.execute("DELETE FROM telemetry_signals WHERE ts < ?", (cutoff,))
        await db.commit()

    return {"archived_signals": count, "cutoff": cutoff}


async def get_latest_correlation_report(app_url: str, window: str) -> dict | None:
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT report_id, app_url, window, created_at, report_json
            FROM correlation_reports
            WHERE app_url = ? AND window = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (app_url, window),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                report = json.loads(row[4])
            except Exception:
                report = {}
            return {
                "report_id": row[0],
                "app_url": row[1],
                "window": row[2],
                "created_at": row[3],
                "report": report,
            }
