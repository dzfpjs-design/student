from __future__ import annotations

from .service import ObservableEvent


class BehaviorEventAdapter:
    """Maps other local modules' derived signals into the safe event contract."""

    SOURCE_EVENT_MAP = {
        "vision": {"seated": "presence.seated", "away": "presence.away", "writing": "work.writing", "reading": "work.reading"},
        "audio": {"help_request": "help.request", "stall": "work.stall"},
        "application": {"switch": "interaction.switch"},
        "plan": {"started": "task.started", "finished": "task.finished", "deviation": "plan.deviation"},
        "child": {"check_in": "child.check_in", "fatigue": "fatigue.candidate"},
    }

    @classmethod
    def from_derived_signal(cls, *, child_id: str, session_id: str | None, occurred_at: str,
                            source_family: str, signal: str, confidence: float,
                            derived_event_id: str, missing_sources: tuple[str, ...] = (),
                            alternative_explanations: tuple[str, ...] = (), payload: dict | None = None) -> ObservableEvent:
        try:
            event_type = cls.SOURCE_EVENT_MAP[source_family][signal]
        except KeyError as error:
            raise ValueError("Only allow-listed locally derived signals may enter the behavior model") from error
        return ObservableEvent(child_id=child_id, session_id=session_id, occurred_at=occurred_at,
                               event_type=event_type, source=source_family, confidence=confidence,
                               source_refs=(derived_event_id,), missing_sources=missing_sources,
                               alternative_explanations=alternative_explanations, payload=payload or {})

    @classmethod
    def from_vision_signal(cls, *, frame_width: int, frame_height: int, input_fps: float, **kwargs) -> ObservableEvent:
        """Reject oversized/high-rate observation inputs before they enter state inference."""
        if frame_width > 640 or frame_height > 640 or input_fps > 2:
            raise ValueError("behavior_input_budget_exceeded: maximum is 640x640 at 2FPS")
        payload = dict(kwargs.pop("payload", {}) or {})
        payload["input_budget"] = {"width": frame_width, "height": frame_height, "input_fps": input_fps, "execution_route": "cpu"}
        return cls.from_derived_signal(source_family="vision", payload=payload, **kwargs)
