"""Global decision-model settings passed into harness."""

from __future__ import annotations

import json
from dataclasses import dataclass

from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.errors import ErrorCode, OctopError

_SETTINGS_KEY = "decision_model"
_SECRET_KEY = "decision_api_key"
_PROVIDERS = frozenset({"laya", "jev", "microsoft", "custom"})


@dataclass(frozen=True)
class DecisionSettings:
    enabled: bool
    provider: str
    endpoint: str
    model: str
    skills: bool
    tools: bool
    team: bool
    threshold: float
    tool_min_count: int
    api_key_set: bool

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.endpoint and (self.skills or self.tools or self.team))


class DecisionSettingsStore:
    """Persist the decision-model switch. The secret never goes back to the UI."""

    def __init__(self, *, settings_repo: SettingsRepo, secret_repo: SecretRepo) -> None:
        self._settings = settings_repo
        self._secrets = secret_repo

    def load(self) -> DecisionSettings:
        raw = self._settings.get(_SETTINGS_KEY)
        data: dict[str, object] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
        provider = str(data.get("provider") or "laya").strip().lower()
        if provider not in _PROVIDERS:
            provider = "laya"
        return DecisionSettings(
            enabled=bool(data.get("enabled", False)),
            provider=provider,
            endpoint=str(data.get("endpoint") or "").strip(),
            model=str(data.get("model") or "").strip(),
            skills=bool(data.get("skills", True)),
            tools=bool(data.get("tools", True)),
            team=bool(data.get("team", True)),
            threshold=_threshold(data.get("threshold", 0.6)),
            tool_min_count=_tool_min_count(data.get("tool_min_count", 8)),
            api_key_set=bool(self._api_key()),
        )

    def save(
        self,
        *,
        enabled: bool,
        provider: str,
        endpoint: str,
        model: str,
        skills: bool,
        tools: bool,
        team: bool,
        threshold: float,
        tool_min_count: int,
        api_key: str | None = None,
    ) -> DecisionSettings:
        previous = self.load()
        provider = provider.strip().lower()
        endpoint = endpoint.strip()
        if provider not in _PROVIDERS:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "unknown decision provider")
        if enabled:
            if not endpoint.startswith(("http://", "https://")):
                raise OctopError(
                    ErrorCode.SLASH_BAD_ARGS,
                    "endpoint is required when the decision model is enabled",
                )
            if provider in {"jev", "microsoft"} and not model.strip():
                raise OctopError(
                    ErrorCode.SLASH_BAD_ARGS,
                    "model is required for this decision provider",
                )
            if not (skills or tools or team):
                raise OctopError(
                    ErrorCode.SLASH_BAD_ARGS,
                    "enable at least one of skills, tools, or team",
                )
        if not 0 < float(threshold) < 1:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "threshold must be between 0 and 1")
        if int(tool_min_count) < 1:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "tool_min_count must be at least 1")
        payload = {
            "enabled": enabled,
            "provider": provider,
            "endpoint": endpoint,
            "model": model.strip(),
            "skills": skills,
            "tools": tools,
            "team": team,
            "threshold": float(threshold),
            "tool_min_count": int(tool_min_count),
        }
        self._settings.set(_SETTINGS_KEY, json.dumps(payload, ensure_ascii=False))
        if api_key is not None:
            self._write_secret(api_key)
        elif previous.endpoint and previous.endpoint != endpoint:
            # Credentials belong to an endpoint. Silently forwarding the old
            # secret after an address change can leak it to another service.
            self._write_secret("")
        return self.load()

    def harness_config(self) -> object | None:
        """Build a harness ``DecisionConfig``, or ``None`` when the switch is off."""
        view = self.load()
        if not view.active:
            return None
        from octop_harness.decision.config import DecisionConfig

        return DecisionConfig(
            enabled=True,
            provider=view.provider,  # type: ignore[arg-type]
            endpoint=view.endpoint,
            api_key=self._api_key(),
            model=view.model,
            skills=view.skills,
            tools=view.tools,
            team=view.team,
            threshold=view.threshold,
            tool_min_count=view.tool_min_count,
        )

    async def probe(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        model: str | None,
        provider: str | None,
    ) -> dict[str, object]:
        view = self.load()
        target = (endpoint if endpoint is not None else view.endpoint).strip()
        if not target.startswith(("http://", "https://")):
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "decision endpoint is required")
        from octop_harness.decision.client import HttpDecisionClient

        saved_key = self._api_key()
        probe_key = (
            api_key
            if api_key is not None
            else saved_key
            if endpoint is None or target == view.endpoint
            else ""
        )
        client = HttpDecisionClient(
            endpoint=target,
            api_key=probe_key,
            model=(model if model is not None else view.model).strip(),
            provider=(provider or view.provider).strip(),
        )
        try:
            probabilities = await client.score_async(
                state="连接测试",
                statements=[("ping", "这是一次连接测试。")],
                strict=True,
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return {"ok": False, "error": f"could not reach {target}: {detail[:240]}"}
        probability = probabilities.get("ping")
        if probability is None:
            return {"ok": False, "error": "no probability returned"}
        return {"ok": True, "probability": probability}

    def _api_key(self) -> str:
        raw = self._secrets.get(_SECRET_KEY)
        if not raw:
            return ""
        return raw.decode("utf-8").strip()

    def _write_secret(self, api_key: str) -> None:
        encoded = api_key.strip().encode("utf-8")
        existing = self._secrets.get(_SECRET_KEY)
        if existing is not None:
            self._secrets.rotate(_SECRET_KEY, encoded)
            return
        self._secrets.get_or_create(_SECRET_KEY, lambda: encoded)


def _threshold(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.6
    if not 0 < number < 1:
        return 0.6
    return number


def _tool_min_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 8
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 8
    return number if number >= 1 else 8
