from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from pydantic import BaseModel, Field
import uuid


class StructuredAssertion(BaseModel):
    """Machine-evaluable assertion captured during recording."""
    assertion_type: Literal[
        "text_present",     # expected text is present in element or page body
        "element_visible",  # element matching target selector/role is visible
        "url_contains",     # current URL contains expected substring
        "page_title",       # document.title contains expected substring
        "count",            # element count equals expected (integer string)
        "semantic",         # requires LLM/vision evaluation
    ]
    target: str | None = None      # CSS selector, ARIA role name, or URL substring
    expected: str | None = None    # expected text/value/count
    description: str = ""          # original natural-language form (always kept)
    negated: bool = False          # if True, assert that condition does NOT hold


class AuthProfile(BaseModel):
    profile_name: str
    auth_type: Literal["env_login", "storage_state", "cookie_json"]
    login_url: str | None = None
    username_env: str | None = "TEST_USERNAME"
    password_env: str | None = "TEST_PASSWORD"
    storage_state_path: str | None = None
    cookie_json_path: str | None = None
    user_data_dir: str | None = None  # Persistent Chromium profile dir (for anti-bot OAuth)


class SpaHints(BaseModel):
    """Per-flow hints for navigating complex SPAs and web-component apps."""
    wait_for_selector: str | None = None        # CSS selector that signals page is ready
    wait_for_shadow_selector: str | None = None # CSS selector to search inside shadow roots
    entry_url_pattern: str | None = None        # URL substring indicating we're in the right view
    settle_ms: int = 1500                       # Extra settle wait after navigation (ms)
    has_web_components: bool = False            # App uses shadow DOM web components
    push_state_navigation: bool = False         # SPA uses pushState (not full page loads)
    # Canvas/WebGL-heavy app fields (populated from context graph archetype == "editor_heavy")
    is_editor_heavy: bool = False               # App requires extended canvas/WebGL init waits
    editor_ready_selector: str | None = None   # DOM element that confirms the heavy view is ready
    editor_ready_js: str | None = None         # JS expression that resolves true when view is ready
    editor_settle_ms: int = 8000               # Settle time for canvas/WebGL views (ms)


class FlowStep(BaseModel):
    step_id: int
    action: Literal["navigate", "click", "fill", "select", "upload", "drag", "assert", "wait"]
    selector: str | None = None
    value: str | None = None
    description: str = ""
    wait_after_secs: float = 0.5
    # Hybrid replay fields
    target_text: str | None = None
    dom_fingerprint: str | None = None
    url_before: str | None = None
    url_after: str | None = None
    screenshot_path: str | None = None
    # Semantic locator fields (captured at record time for stable replay)
    aria_role: str | None = None           # ARIA role, e.g. "button", "textbox", "link"
    aria_name: str | None = None           # accessible name at record time
    aria_snapshot: str | None = None       # compact ARIA subtree JSON (depth 2, max 30 nodes)
    testid_selector: str | None = None     # e.g. "[data-testid='submit-btn']"
    label_text: str | None = None          # associated label/placeholder for fill steps
    # Structured assertion (for assert steps only)
    structured_assertion: StructuredAssertion | None = None


class RecordedFlow(BaseModel):
    flow_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    flow_name: str
    app_url: str
    goal: str
    steps: list[FlowStep]
    created_at: str
    assertions_json: list[str] = []
    structured_assertions: list[StructuredAssertion] = []
    entry_url: str | None = None
    business_criticality: Literal["revenue", "activation", "retention", "support", "other"] = "other"
    spa_hints: SpaHints = Field(default_factory=SpaHints)
    # When set, overrides the run_mode passed to run_regression_test for this flow.
    # Useful for editor-heavy flows whose selectors don't survive replay (goal_fallback)
    # or for flows that must use strict step ordering (strict_steps).
    run_mode_override: Literal["hybrid", "strict_steps", "goal_fallback"] | None = None


@dataclass
class SiteInventory:
    app_url: str
    routes: list[str]
    buttons: list[dict]
    links: list[dict]
    forms: list[dict]
    headings: list[str]
    auth_signals: list[str]
    business_signals: list[str]
    page_structures: dict[str, list[dict]] = field(default_factory=dict)
    crawled_pages: int = 0

    def to_dict(self) -> dict:
        return {
            "app_url": self.app_url,
            "routes": self.routes,
            "buttons": self.buttons,
            "links": self.links,
            "forms": self.forms,
            "headings": self.headings,
            "auth_signals": self.auth_signals,
            "business_signals": self.business_signals,
            "page_structures": self.page_structures,
            "crawled_pages": self.crawled_pages,
        }


class ContextNode(BaseModel):
    node_id: str
    node_type: Literal["route", "intent", "element_cluster"]
    label: str
    confidence: float = 0.5
    freshness_ts: str | None = None
    metadata: dict = Field(default_factory=dict)


class ContextEdge(BaseModel):
    source_id: str
    target_id: str
    edge_type: Literal["transitions_to", "supports_intent", "interacts_with"]
    weight: float = 1.0
    confidence: float = 0.5
    metadata: dict = Field(default_factory=dict)


class SiteContextGraph(BaseModel):
    graph_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    app_url: str
    profile_name: str | None = None
    archetype: Literal["marketing_site", "saas_app", "editor_heavy", "checkout_heavy"] = "saas_app"
    created_at: str
    nodes: list[ContextNode] = Field(default_factory=list)
    edges: list[ContextEdge] = Field(default_factory=list)
    source_run_id: str | None = None
    source_inventory_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class ContextGraphDiff(BaseModel):
    app_url: str
    previous_graph_id: str | None = None
    current_graph_id: str
    added_nodes: list[str] = Field(default_factory=list)
    removed_nodes: list[str] = Field(default_factory=list)
    added_edges: list[str] = Field(default_factory=list)
    removed_edges: list[str] = Field(default_factory=list)
    confidence_delta: float = 0.0


class ContextGraphVersion(BaseModel):
    graph_id: str
    app_url: str
    profile_name: str | None = None
    archetype: Literal["marketing_site", "saas_app", "editor_heavy", "checkout_heavy"] = "saas_app"
    created_at: str
    node_count: int = 0
    edge_count: int = 0
    metadata: dict = Field(default_factory=dict)


class ReleaseReference(BaseModel):
    graph_id: str | None = None
    run_id: str | None = None


class ReleaseSnapshot(BaseModel):
    release_id: str
    app_url: str
    created_at: str
    baseline_ref: ReleaseReference = Field(default_factory=ReleaseReference)
    candidate_ref: ReleaseReference = Field(default_factory=ReleaseReference)
    risk_score: float = 0.0
    risk_level: Literal["low", "medium", "high", "blocker"] = "low"
    top_risks: list[dict] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class JourneyHealth(BaseModel):
    journey_id: str
    journey_name: str
    criticality: Literal["revenue", "activation", "retention", "support", "other"] = "other"
    pass_rate: float | None = None
    p95_duration_ms: int | None = None
    stability_score: float | None = None
    trend: Literal["improving", "flat", "degrading"] = "flat"
    run_count: int = 0
    metadata: dict = Field(default_factory=dict)


class RiskAssessment(BaseModel):
    release_id: str
    app_url: str
    risk_score: float = 0.0
    risk_level: Literal["low", "medium", "high", "blocker"] = "low"
    top_risks: list[dict] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    created_at: str


class IncidentCluster(BaseModel):
    cluster_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    app_url: str
    title: str
    severity: Literal["low", "medium", "high", "blocker"] = "medium"
    affected_flows: int = 0
    affected_criticality: list[str] = Field(default_factory=list)
    first_seen: str
    last_seen: str
    evidence_refs: list[str] = Field(default_factory=list)
    member_case_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"
    metadata: dict = Field(default_factory=dict)


class RemediationDraft(BaseModel):
    cluster_id: str
    incident_title: str
    severity: Literal["low", "medium", "high", "blocker"] = "medium"
    issue_draft: str
    repro_steps: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    owner_hints: list[str] = Field(default_factory=list)
    fix_hypotheses: list[str] = Field(default_factory=list)
    created_at: str


class TelemetrySignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    app_url: str
    source: Literal["sentry", "datadog", "ga4", "custom"] = "custom"
    ts: str
    signal_type: Literal["error_rate", "latency_p95", "conversion", "custom"] = "custom"
    journey_key: str | None = None
    route: str | None = None
    value: float
    unit: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class CorrelationMatch(BaseModel):
    match_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    cluster_id: str
    telemetry_signal: str
    confidence: float
    business_impact_estimate: str


class StabilityFingerprint(BaseModel):
    selector_entropy: float = 0.0       # higher means selector likely brittle
    aria_consistency: float = 0.0       # higher means semantic locator looked stable
    latency_ms: int = 0                 # observed step latency
    retry_count: int = 0
    drift_score: float = 0.0            # heuristic [0,1] drift indicator


@dataclass
class ReplayStepResult:
    step_id: int
    action: str
    status: str  # pass | fail | skip | repaired
    replay_mode: str  # selector | text_lookup | vision_repair | agent_repair | skipped
    error: str | None = None
    screenshot_path: str | None = None
    elapsed_ms: int = 0
    retry_count: int = 0
    selector_entropy: float = 0.0
    aria_consistency: float = 0.0
    repair_confidence: float = 0.0
    failure_reason: str | None = None


@dataclass
class ReplayTrace:
    flow_id: str
    flow_name: str
    run_mode: str  # strict_steps | hybrid_repair | goal_fallback
    step_results: list[ReplayStepResult] = field(default_factory=list)
    assertion_results: list[dict] = field(default_factory=list)
    step_failure_index: int | None = None
    console_errors: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    raw_result: str = ""
    trace_path: str | None = None


class FailureCase(BaseModel):
    case_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    flow_id: str
    flow_name: str
    status: Literal["pass", "fail", "error", "blocked"]
    severity: Literal["blocker", "high", "medium", "low", "none"] = "none"
    failure_class: Literal["product_bug", "test_fragility", "auth_failure", "env_issue"] | None = None
    failure_reason_codes: list[str] = []
    repro_steps: list[str] = []
    console_errors: list[str] = []
    network_errors: list[str] = []
    screenshots: list[str] = []
    raw_result: str = ""
    replay_mode: str = "goal_fallback"
    step_failure_index: int | None = None
    assertion_failures: list[str] = []
    assertion_results: list[dict] = []
    business_criticality: Literal["revenue", "activation", "retention", "support", "other"] = "other"
    trace_path: str | None = None
    failure_class_confidence: float = 0.0
    repair_confidence: float = 0.0
    stability_fingerprints: list[StabilityFingerprint] = Field(default_factory=list)
    healing_decision: Literal["auto_heal", "propose_patch", "none"] = "none"


class DiscoverResult(BaseModel):
    app_url: str
    flows: list[dict]
    flow_count: int
    inventory_summary: dict = {}
    quality: dict = {}


class AuthProfileResult(BaseModel):
    profile_name: str
    auth_type: str
    status: str
    note: str


class RecordedFlowResult(BaseModel):
    flow_id: str
    flow_name: str
    step_count: int
    status: str
    artifacts_dir: str


RunStatus = Literal["queued", "running", "waiting_auth", "completed", "failed", "cancelled"]


class RunStartedResult(BaseModel):
    run_id: str
    status: str
    flow_count: int
    artifacts_dir: str


class RunResult(BaseModel):
    run_id: str
    status: str
    started_at: str
    completed_at: str | None
    cases: list[FailureCase]
    severity_counts: dict[str, int]
    next_actions: list[str]
    artifacts_dir: str


class RecordedTestsResult(BaseModel):
    flows: list[dict]
    total: int


class DebugResult(BaseModel):
    case_id: str
    run_id: str
    status: str
    screenshots: list[str]
    console_log: str
    repro_steps: list[str]
    step_failure_index: int | None = None
    replay_mode: str = ""
    assertion_failures: list[str] = []
    why_failed: str = ""
