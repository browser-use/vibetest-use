import os
import json
import time
import uuid
import aiosqlite

DB_PATH = os.environ.get("VIBETEST_DB_PATH", ".vibetest/runs.db")


async def _ensure_dir():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


async def init_db():
    await _ensure_dir()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                test_id TEXT PRIMARY KEY,
                url TEXT,
                agents INTEGER,
                start_time REAL,
                end_time REAL,
                duration REAL,
                status TEXT,
                results_json TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                app_url TEXT,
                repo_path TEXT,
                focus TEXT,
                flows_json TEXT,
                created_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auth_configs (
                auth_config_id TEXT PRIMARY KEY,
                config_json TEXT,
                created_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sprite_runs (
                run_id TEXT PRIMARY KEY,
                plan_id TEXT,
                status TEXT,
                started_at REAL,
                completed_at REAL,
                headless INTEGER,
                artifacts_dir TEXT,
                cases_json TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT,
                case_id TEXT,
                type TEXT,
                path TEXT,
                created_at REAL
            )
        """)
        await db.commit()


async def save_run(test_data: dict):
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO runs
            (test_id, url, agents, start_time, end_time, duration, status, results_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                test_data["test_id"],
                test_data.get("url"),
                test_data.get("agents"),
                test_data.get("start_time"),
                test_data.get("end_time"),
                test_data.get("duration"),
                test_data.get("status"),
                json.dumps(test_data),
            ),
        )
        await db.commit()


async def get_run(test_id: str) -> dict | None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT results_json FROM runs WHERE test_id = ?", (test_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
    return None


async def list_runs(limit: int = 20) -> list:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT results_json FROM runs ORDER BY start_time DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [json.loads(row[0]) for row in rows]


# === Sprite (TestSprite) helpers ===

async def save_plan(plan) -> None:
    """Save a TestPlan to the plans table."""
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO plans
            (plan_id, app_url, repo_path, focus, flows_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                plan.app_url,
                plan.repo_path,
                plan.focus,
                json.dumps(plan.flows),
                plan.created_at,
            ),
        )
        await db.commit()


async def get_plan(plan_id: str):
    """Return a TestPlan or None."""
    from .models import TestPlan
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT plan_id, app_url, repo_path, focus, flows_json, created_at FROM plans WHERE plan_id = ?",
            (plan_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return TestPlan(
                    plan_id=row[0],
                    app_url=row[1],
                    repo_path=row[2],
                    focus=row[3],
                    flows=json.loads(row[4]),
                    created_at=row[5],
                )
    return None


async def save_auth_config(config) -> None:
    """Save an AuthConfig to the auth_configs table."""
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO auth_configs
            (auth_config_id, config_json, created_at)
            VALUES (?, ?, ?)
            """,
            (
                config.auth_config_id,
                config.model_dump_json(),
                config.created_at,
            ),
        )
        await db.commit()


async def get_latest_auth_config():
    """Return the most recently saved AuthConfig or None."""
    from .models import AuthConfig
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT config_json FROM auth_configs ORDER BY created_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return AuthConfig.model_validate_json(row[0])
    return None


async def save_sprite_run(run) -> None:
    """Save a RunResult to the sprite_runs table."""
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO sprite_runs
            (run_id, plan_id, status, started_at, completed_at, headless, artifacts_dir, cases_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.plan_id,
                run.status,
                run.started_at,
                run.completed_at,
                1 if run.headless else 0,
                run.artifacts_dir,
                json.dumps([c.model_dump() for c in run.cases]),
            ),
        )
        await db.commit()


async def get_sprite_run(run_id: str):
    """Return a RunResult or None."""
    from .models import RunResult, FailureCase
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT run_id, plan_id, status, started_at, completed_at,
                      headless, artifacts_dir, cases_json
               FROM sprite_runs WHERE run_id = ?""",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                cases_data = json.loads(row[7]) if row[7] else []
                cases = [FailureCase(**c) for c in cases_data]
                return RunResult(
                    run_id=row[0],
                    plan_id=row[1],
                    status=row[2],
                    started_at=row[3],
                    completed_at=row[4],
                    headless=bool(row[5]),
                    artifacts_dir=row[6] or "",
                    cases=cases,
                )
    return None


async def save_artifact(run_id: str, case_id: str, artifact_type: str, path: str) -> None:
    """Save an artifact record."""
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO artifacts (artifact_id, run_id, case_id, type, path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), run_id, case_id, artifact_type, path, time.time()),
        )
        await db.commit()
