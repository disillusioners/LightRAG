"""
Workspace management routes for the LightRAG API.

This module exposes workspace-level endpoints following the router
factory pattern used by the other routers in ``lightrag.api.routers``.
It is intentionally decoupled from the concrete ``WorkspaceManager``
implementation: the factory accepts any object that exposes
``list_workspaces()`` and ``get_default_workspace()`` (duck typing) so
the router can be imported without dragging in the (potentially
heavy) manager module and without risking circular imports.
"""

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.utils import logger


class WorkspaceInfo(BaseModel):
    """Public representation of a single registered workspace.

    Mirrors the dict shape returned by ``WorkspaceManager.list_workspaces``
    so the handler can ``**``-unpack each entry directly into this model.
    ``first_seen`` is the immutable ISO timestamp of the workspace's first
    registration; ``last_seen`` is updated on every subsequent registration
    call. ``document_count`` is optional because workspaces that exist in
    the registry but have never ingested any documents have no meaningful
    count to report.
    """

    name: str
    first_seen: str
    last_seen: str
    document_count: Optional[int] = None


class WorkspacesResponse(BaseModel):
    """Response body for ``GET /workspaces``.

    Always includes ``default_workspace`` so clients can render the
    implicit workspace name (e.g. in a UI selector) even when only a
    single workspace exists.
    """

    workspaces: List[WorkspaceInfo]
    default_workspace: str


def create_workspace_routes(
    workspace_mgr: Any, api_key: Optional[str] = None
) -> APIRouter:
    """Create workspace management routes.

    Args:
        workspace_mgr: Any object exposing ``list_workspaces()`` (async)
            and ``get_default_workspace()`` (sync) — typically a
            ``WorkspaceManager`` instance. Typed as ``Any`` to avoid a
            hard dependency on the manager module.
        api_key: Optional API key forwarded to the auth dependency.
            When ``None`` (or empty), API-key auth is disabled and only
            OAuth2/whitelist rules apply.

    Returns:
        APIRouter: A router mounted at ``/workspaces`` exposing the
        ``GET /workspaces`` endpoint.
    """
    router = APIRouter(prefix="/workspaces", tags=["workspaces"])

    # Same combined-auth dependency shape as document/query/graph routes.
    combined_auth = get_combined_auth_dependency(api_key)

    @router.get("", dependencies=[Depends(combined_auth)])
    async def list_workspaces() -> WorkspacesResponse:
        """List all known workspaces.

        Returns the list of registered workspaces along with the name of
        the default workspace. The default workspace is always present
        in ``default_workspace`` — callers may treat the field as a
        fallback when ``workspaces`` is empty (defensive) or when the
        user has not selected an explicit workspace.

        Raises:
            HTTPException: 500 if the underlying manager fails. The raw
                error message is returned in ``detail`` for visibility;
                callers should not rely on its exact wording.
        """
        try:
            workspaces = await workspace_mgr.list_workspaces()
            default_ws = workspace_mgr.get_default_workspace()
        except Exception as exc:
            # Log full traceback server-side; surface a single-line
            # message to the client.
            logger.error("Failed to list workspaces via WorkspaceManager: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        return WorkspacesResponse(
            workspaces=[WorkspaceInfo(**ws) for ws in workspaces],
            default_workspace=default_ws,
        )

    return router
