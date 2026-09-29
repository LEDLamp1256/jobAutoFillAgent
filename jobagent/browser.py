"""Small asynchronous browser boundary for one application session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jobagent.domain import (
    ActionOutcome, Advance, ApplicationObservation, ApplicationSession,
    ChooseOption, FillText, GoBack,
)


@dataclass(frozen=True)
class BrowserActionResult:
    outcome: ActionOutcome
    observation: ApplicationObservation


class BrowserPort(Protocol):
    async def navigate(self, url: str) -> ApplicationObservation: ...
    async def observe(self) -> ApplicationObservation: ...
    async def fill_text(self, action: FillText, session: ApplicationSession) -> BrowserActionResult: ...
    async def choose_option(self, action: ChooseOption, session: ApplicationSession) -> BrowserActionResult: ...
    async def activate_navigation(self, action: Advance | GoBack,
                                  session: ApplicationSession) -> BrowserActionResult: ...
    async def annotate_field(self, target_ref: str, observation_id: str,
                             state: str) -> None: ...
    async def close(self) -> None: ...
