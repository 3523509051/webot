"""Local group batches; waiting never calls the model."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MessageBatch:
    started: float
    quiet_seconds: float
    max_seconds: float
    last_related: float = field(init=False)
    items: list[dict] = field(default_factory=list)
    participants: set[str] = field(default_factory=set)
    closed: bool = False

    def __post_init__(self):
        self.last_related = self.started

    @property
    def deadline(self) -> float:
        return min(self.started + self.max_seconds,
                   self.last_related + self.quiet_seconds)

    def collecting(self, now: float) -> bool:
        return not self.closed and now < self.deadline

    def add(self, item: dict, now: float) -> None:
        self.items.append(item)
        self.participants.add(item["sender_id"])
        self.last_related = now
