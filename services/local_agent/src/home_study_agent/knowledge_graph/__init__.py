"""Local, provenance-preserving knowledge graph primitives."""

from .service import KnowledgeGraphService
from .application_service import KnowledgeGraphApplicationService

__all__ = ["KnowledgeGraphApplicationService", "KnowledgeGraphService"]
