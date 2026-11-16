"""Local, evidence-led longitudinal behavior modelling.

This package intentionally models observable study-session signals, not emotion,
personality, medical conditions, or facial inferences.
"""

from .service import BehaviorModelService, ObservableEvent
from .adapter import BehaviorEventAdapter
from .app_integration import BehaviorAppIntegration

__all__ = ["BehaviorModelService", "ObservableEvent", "BehaviorEventAdapter", "BehaviorAppIntegration"]
