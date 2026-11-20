from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Recipient:
    kind: str
    identifier: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.identifier}


class RecipientDirectory(Protocol):
    def manager_group(self, org_id: UUID, site_id: UUID) -> Recipient: ...
    def site_lead(self, org_id: UUID, site_id: UUID) -> Recipient: ...
    def consultant_supervisor(self, org_id: UUID, site_id: UUID, consultant_id: UUID) -> Recipient: ...


@dataclass
class StaticRecipientDirectory:
    """Explicit fictional directory adapter; keys include org and site, never global roles."""
    groups: dict[tuple[UUID, UUID], str] = field(default_factory=dict)
    leads: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    supervisors: dict[tuple[UUID, UUID, UUID], UUID] = field(default_factory=dict)

    def manager_group(self, org_id, site_id):
        return Recipient("group", self.groups[(org_id, site_id)])

    def site_lead(self, org_id, site_id):
        return Recipient("user", str(self.leads[(org_id, site_id)]))

    def consultant_supervisor(self, org_id, site_id, consultant_id):
        return Recipient("user", str(self.supervisors[(org_id, site_id, consultant_id)]))

