from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Action(BaseModel):
    kind: Literal[
        "click", "fill", "press", "select", "goto", "back", "reload", "scroll", "drag", "upload", "wait"
    ]
    selector: str = ""
    value: str = ""
    label: str = ""
    x: float | None = None
    y: float | None = None
    end_x: float | None = None
    end_y: float | None = None
    button: Literal["left", "right", "middle"] = "left"
    clear: bool = True


class Assertion(BaseModel):
    name: str
    kind: Literal["visible", "hidden", "text", "url", "attribute", "plane_archive"]
    selector: str = ""
    expected: str = ""
    attribute: str = ""
    after_action: str = ""


class Target(BaseModel):
    name: str
    url: str
    issue_url: str = ""
    objective: str = ""
    setup: list[Action] = []
    assertions: list[Assertion] = []
    failure_selector: str = ""
    state_probe: str = ""
    # Isolated backend/account setup is owned by the target. No global database reset.
    storage_state: str | None = None
    isolation: Literal["browser", "account", "fixture"] = "browser"
    allowed_domains: list[str] = []
    seed_ready: bool = True
    notes: str = ""
    fixture_adapter: Literal["", "plane"] = ""
    exploration_objectives: list[str] = []


class RunConfig(BaseModel):
    target: Target
    engine: Literal["fixture", "browser-use", "gemma"] = "browser-use"
    workers: int = Field(default=3, ge=1, le=256)
    max_jobs: int = Field(default=24, ge=1, le=10000)
    max_depth: int = Field(default=10, ge=1, le=100)
    budget_seconds: int = Field(default=180, ge=5, le=7200)
    branch_steps: int = Field(default=3, ge=1, le=20)
    use_gemma: bool = False
    cloud_browser: bool = False
    execution: Literal["local", "ray"] = "local"

    @model_validator(mode="after")
    def isolation_check(self):
        if self.execution == "local" and self.workers > 8:
            raise ValueError("More than 8 browser workers requires the Ray fleet")
        if self.workers > 1 and self.target.isolation == "browser" and self.engine != "fixture":
            raise ValueError(
                "Parallel stateful targets need isolated accounts or a fixture; choose one worker until configured."
            )
        return self
