from pydantic import BaseModel, Field
from typing import Optional, Literal
import uuid
import time


class TestPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    app_url: str
    repo_path: Optional[str] = None
    focus: Optional[str] = None
    flows: list[str]          # 3-8 plain-English flow strings
    created_at: float = Field(default_factory=time.time)


class AuthConfig(BaseModel):
    auth_config_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    auth_type: Literal["env_login", "storage_state", "cookie_json"]
    login_url: Optional[str] = None
    username_env: str = "TEST_USERNAME"   # name of the env var, not value
    password_env: str = "TEST_PASSWORD"
    storage_state_path: Optional[str] = None
    cookie_json_path: Optional[str] = None
    created_at: float = Field(default_factory=time.time)


class FailureCase(BaseModel):
    case_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    flow: str
    status: Literal["passed", "failed", "error"]
    severity: Literal["blocker", "high", "medium", "low", "none"] = "none"
    repro_steps: list[str] = []
    console_errors: list[str] = []
    network_errors: list[str] = []
    screenshots: list[str] = []   # absolute paths to PNG files
    trace_path: Optional[str] = None
    raw_result: str = ""


class RunResult(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plan_id: str
    status: Literal["running", "completed", "failed"]
    started_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None
    headless: bool = True
    artifacts_dir: str = ""
    cases: list[FailureCase] = []

    @property
    def summary(self) -> dict:
        counts = {"blocker": 0, "high": 0, "medium": 0, "low": 0, "passed": 0, "error": 0}
        for c in self.cases:
            if c.status == "passed":
                counts["passed"] += 1
            elif c.status == "error":
                counts["error"] += 1
            else:
                counts[c.severity] = counts.get(c.severity, 0) + 1
        return counts
