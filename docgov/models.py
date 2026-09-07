from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Evidence:
    path: str
    kind: str = "source"
    sha256: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class DocumentRecord:
    path: str
    type: str
    owner: str = "unassigned"
    status: str = "current"
    depends_on: List[str] = field(default_factory=list)
    ttl_days: Optional[int] = None
    approval: str = "auto"
    last_verified_at: Optional[str] = None
    authority: str = "canonical"
    canonical_key: Optional[str] = None
    # Deployment environments this document describes. Declaring one binds the
    # document's trust to that environment matching the state Git produced.
    environments: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "DocumentRecord":
        return cls(
            path=str(value["path"]),
            type=str(value.get("type", "contract")),
            owner=str(value.get("owner", "unassigned")),
            status=str(value.get("status", "current")),
            depends_on=[str(item) for item in value.get("depends_on", [])],
            ttl_days=value.get("ttl_days"),
            approval=str(value.get("approval", "auto")),
            last_verified_at=(
                str(value["last_verified_at"])
                if value.get("last_verified_at") is not None
                else None
            ),
            authority=str(value.get("authority", "canonical")),
            canonical_key=value.get("canonical_key"),
            environments=[str(item) for item in value.get("environments", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        return {key: item for key, item in value.items() if item is not None and item != []}


@dataclass
class Finding:
    kind: str
    risk: str
    action: str
    documents: List[str]
    reason: str
    evidence: List[Evidence] = field(default_factory=list)
    proposed_patch: Optional[str] = None
    human_decision: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return {key: item for key, item in value.items() if item is not None}


@dataclass
class TrustResult:
    path: str
    type: str
    status: str
    scope: str
    reason: str
    evidence: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return value


@dataclass
class GovernanceDecision:
    run_id: str
    mode: str
    result: str
    changed: bool
    findings: List[Finding] = field(default_factory=list)
    trust_results: List[TrustResult] = field(default_factory=list)
    modified_paths: List[str] = field(default_factory=list)
    head_sha: Optional[str] = None
    model_used: bool = False
    model_requested: bool = False
    model_trace: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def high_risk_findings(self) -> List[Finding]:
        return [finding for finding in self.findings if finding.risk == "high"]

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "run_id": self.run_id,
            "mode": self.mode,
            "result": self.result,
            "changed": self.changed,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
            "trust_results": [result.to_dict() for result in self.trust_results],
            "modified_paths": sorted(set(self.modified_paths)),
            "model_used": self.model_used,
            "model_requested": self.model_requested,
            "model_trace": self.model_trace,
        }
        if self.head_sha:
            value["head_sha"] = self.head_sha
        if self.error:
            value["error"] = self.error
        return value
