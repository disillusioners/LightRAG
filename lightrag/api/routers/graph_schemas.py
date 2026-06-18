"""
Shared Pydantic schemas for graph operations.

These request models are referenced by both ``graph_routes.py`` and
``document_routes.py``. Keeping them in a dedicated leaf module avoids a
circular import: ``graph_routes.py`` already imports
``check_pipeline_busy_or_raise`` from ``document_routes.py``, so a reverse
import for these schemas would deadlock at module load. A neutral third
module lets both routers depend on it without depending on each other.
"""

from pydantic import BaseModel, Field, field_validator


class DeleteEntityRequest(BaseModel):
    entity_name: str = Field(..., description="The name of the entity to delete.")

    @field_validator("entity_name", mode="after")
    @classmethod
    def validate_entity_name(cls, entity_name: str) -> str:
        if not entity_name or not entity_name.strip():
            raise ValueError("Entity name cannot be empty")
        return entity_name.strip()


class DeleteRelationRequest(BaseModel):
    source_entity: str = Field(..., description="The name of the source entity.")
    target_entity: str = Field(..., description="The name of the target entity.")

    @field_validator("source_entity", "target_entity", mode="after")
    @classmethod
    def validate_entity_names(cls, entity_name: str) -> str:
        if not entity_name or not entity_name.strip():
            raise ValueError("Entity name cannot be empty")
        return entity_name.strip()
