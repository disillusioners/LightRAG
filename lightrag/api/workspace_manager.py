"""
WorkspaceManager — multi-tenant LRU cache of LightRAG instances.

Purpose
-------
The LightRAG API server serves per-workspace data. This module is the
authoritative owner of the live ``LightRAG`` instances used to serve requests:
it constructs them, holds them in an LRU cache, hands them to request
handlers on ``acquire()``, and tears them down on ``release()`` or
``finalize()``. It also owns the :class:`WorkspaceRegistry`, a complementary
data structure that records which workspace names the server knows about,
independent of which instances happen to be live at any given moment.

A single :class:`WorkspaceManager` is shared by the FastAPI server and
guards all cache + refcount mutations with an :class:`asyncio.Lock` so
concurrent requests do not race on the cache.

Locking pattern
---------------
The manager uses **two** :class:`asyncio.Lock` instances:

* ``self._cache_lock`` — guards all cache + refcount operations
  (``acquire``, ``release``, ``_evict_if_needed``, ``finalize``'s
  dict-touching portion). ``acquire()`` holds this lock for the entire
  duration of the call. This is fully correct (no races on the cache or
  refcount dicts) at the cost of serializing acquires under contention;
  the trade-off favors correctness and simplicity over peak concurrency
  because LightRAG storage initialization is itself expensive and the
  per-request ``await`` of ``_create_rag_instance`` dwarfs lock-acquire
  latency in practice.

* ``self._init_lock`` — guards :meth:`_create_rag_instance`. Storage
  initialization must be serialized across concurrent ``LightRAG`` builds
  (see the warning at ``lightrag/lightrag.py:1277``: *"Storage
  initialization must be called one by one to prevent deadlock."*).
  ``_init_lock`` therefore serializes the entire body of
  ``_create_rag_instance``. Callers of ``_create_rag_instance`` MUST
  already hold ``_init_lock`` before invoking it — the method body does
  NOT re-acquire it, which both documents the contract and avoids the
  non-reentrant-across-await misuse that ``asyncio.Lock`` would create
  if naively nested.

In ``release()`` and ``finalize()`` the dict mutation is short, so the
manager holds ``_cache_lock`` only for the dict read/mutation, snapshots
the ``LightRAG`` instance(s) to tear down, releases the lock, and then
awaits :meth:`LightRAG.finalize_storages` outside the lock. This keeps
lock-hold time short so an in-progress teardown of one workspace cannot
stall ``acquire`` / ``release`` for unrelated workspaces.

``_evict_if_needed`` runs while ``acquire`` already holds
``_cache_lock`` and cannot release it mid-call without breaking the
acquire invariant, so it uses a **tombstone + background finalize**
pattern: the cache slot is replaced with a ``None`` sentinel (and the
refcount popped) inside the lock, and the actual
``await rag.finalize_storages()`` is scheduled as an
:func:`asyncio.create_task` that runs OUTSIDE the lock. The background
task re-acquires the lock once the await completes to remove the
tombstone. This solves three problems at once:

* Slow teardown of one workspace (closing PostgreSQL connections,
  flushing Redis) no longer stalls ``acquire`` / ``release`` for every
  other workspace.
* A failed ``finalize_storages`` does not leave a permanent tombstone:
  the background task pops the tombstone in its ``except`` branch, so
  the entry cannot be re-selected on every subsequent eviction.
* The tombstone makes the slot un-selectable while finalization is in
  flight, so a second ``acquire``-miss cannot start a duplicate
  finalize for the same workspace.

Eviction policy
---------------
The cache is a least-recently-used (LRU) cache capped at
``max_instances`` entries (default 8). Each instance carries an integer
refcount counting outstanding ``acquire`` calls that have not yet been
matched by a ``release``. Eviction targets the LRU entry (first item of
the :class:`collections.OrderedDict`) whose refcount is zero — i.e., a
workspace nobody is currently reading. If the cache is full AND every
entry has a positive refcount, ``_evict_if_needed`` raises
:class:`WorkspaceCacheFullError` and the caller surfaces it to the user
(typically as HTTP 503). ``release()`` does NOT evict proactively;
eviction happens lazily on the next ``acquire()`` that misses, or
implicitly on ``finalize()``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, Optional, TYPE_CHECKING

from lightrag import LightRAG
from lightrag.utils import logger
from lightrag.api.workspace_registry import WorkspaceRegistry
from lightrag.api.llm_factory import register_role_llm_builder

if TYPE_CHECKING:
    from lightrag.utils import EmbeddingFunc
    from lightrag.base import OllamaServerInfos


class WorkspaceCacheFullError(Exception):
    """Raised when the LRU cache is full and all instances are in use.

    Surfaces to the API as ``HTTP 503 Service Unavailable`` (or its
    equivalent for the requested verb). Callers should generally retry
    after a short backoff so the request lands once an in-flight caller
    has released its instance.
    """

    def __init__(self, max_instances: int) -> None:
        self.max_instances: int = max_instances
        super().__init__(
            f"Workspace cache is full (max_instances={max_instances}) "
            "and every cached instance currently has refcount > 0; "
            "cannot evict any in-use workspace."
        )


class WorkspaceManager:
    """Multi-tenant LRU cache of :class:`LightRAG` instances, one per workspace.

    The manager owns a single :class:`WorkspaceRegistry` (used to track
    which workspace names the server knows about) and a process-local
    LRU cache mapping workspace name to a fully-constructed,
    storage-initialized :class:`LightRAG` instance.

    Lifecycle::

        manager = WorkspaceManager(args, embedding_func, ...)
        await manager.initialize()           # eagerly create default workspace
        rag = await manager.acquire(ws)      # request handler entry
        try:
            ...                             # serve request
        finally:
            await manager.release(ws)        # dec refcount; lazy eviction
        await manager.finalize()             # server shutdown

    Concurrency: all methods are ``async`` and safe to call from any
    coroutine on the manager's event loop. See the module-level
    docstring for the locking pattern.
    """

    def __init__(
        self,
        args: Any,
        embedding_func: "EmbeddingFunc",
        llm_model_func: Any,
        llm_model_kwargs: dict,
        llm_timeout: int,
        embedding_timeout: int,
        rerank_model_func: Optional[Any],
        role_llm_configs: dict,
        ollama_server_infos: "OllamaServerInfos",
        max_instances: int = 8,
    ) -> None:
        """Initialize the manager and store the per-instance construction kwargs.

        Args:
            args: Parsed CLI / API configuration namespace. Each
                ``LightRAG``-level field on ``args`` is re-read via
                ``getattr(self.args, "<name>", None)`` inside
                :meth:`_create_rag_instance` so the manager tolerates
                namespaces that omit fields a particular deployment
                doesn't use.
            embedding_func: Embedding function wrapper, used as the
                ``embedding_func`` constructor argument for every cached
                instance.
            llm_model_func: Main (default-role) LLM function. Stored and
                passed verbatim into each :class:`LightRAG` instance.
            llm_model_kwargs: Dict forwarded as ``llm_model_kwargs`` to
                :class:`LightRAG`.
            llm_timeout: Default LLM timeout in seconds; also forwarded
                to :func:`register_role_llm_builder`.
            embedding_timeout: Default embedding timeout in seconds,
                passed as ``default_embedding_timeout`` to
                :class:`LightRAG`.
            rerank_model_func: Optional rerank function (``None`` disables
                rerank on every cached instance).
            role_llm_configs: Already-built role-LLM config mapping. Passed
                as ``role_llm_configs`` to :class:`LightRAG`.
            ollama_server_infos: Shared :class:`OllamaServerInfos` (holds
                the *simulated* model name/tag for the Ollama-compat
                surfaces).
            max_instances: LRU cache capacity. Defaults to 8. Storing the
                cap on the instance (``self._max_instances``) keeps it
                adjustable per-deployment without changing call sites.
        """
        self.args: Any = args
        self.embedding_func: "EmbeddingFunc" = embedding_func
        self.llm_model_func: Any = llm_model_func
        self.llm_model_kwargs: dict = llm_model_kwargs
        self.llm_timeout: int = llm_timeout
        self.embedding_timeout: int = embedding_timeout
        self.rerank_model_func: Optional[Any] = rerank_model_func
        self.role_llm_configs: dict = role_llm_configs
        self.ollama_server_infos: "OllamaServerInfos" = ollama_server_infos
        self._max_instances: int = max_instances

        self.default_workspace: str = getattr(args, "workspace", "") or ""

        self.registry: WorkspaceRegistry = WorkspaceRegistry(
            default_workspace=self.default_workspace
        )

        # OrderedDict: insertion order = LRU order. First item is the
        # next eviction candidate. Values are normally a LightRAG
        # instance; a transient ``None`` value is a tombstone set by
        # :meth:`_evict_if_needed` whose background finalization is
        # still in flight (see the module docstring "tombstone +
        # background finalize" section).
        self._cache: "OrderedDict[str, Optional[LightRAG]]" = OrderedDict()

        # Refcount of outstanding acquire() calls per workspace. Always
        # has an entry for every key in self._cache.
        self._refcounts: dict[str, int] = {}

        self._cache_lock: asyncio.Lock = asyncio.Lock()
        # Serializes _create_rag_instance bodies — see module docstring.
        self._init_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """Eagerly create the default-workspace :class:`LightRAG` instance.

        Called once at server startup. Builds the default-workspace
        instance outside the cache lock (the cache is empty at this
        point, so no contention to serialize) and inserts it into the
        cache with refcount 0. The default workspace is also registered
        in the registry so it appears in ``list_workspaces`` immediately.
        """
        async with self._cache_lock:
            async with self._init_lock:
                rag = await self._create_rag_instance(self.default_workspace)
            self._cache[self.default_workspace] = rag
            self._refcounts[self.default_workspace] = 0
        await self.registry.register(self.default_workspace)
        logger.info(
            "WorkspaceManager initialized: default workspace %r ready "
            "(cache size 1 / max %d)",
            self.default_workspace,
            self._max_instances,
        )

    async def finalize(self) -> None:
        """Tear down every cached :class:`LightRAG` instance.

        Iterates a snapshot of the cache (to avoid mutation during
        iteration), then drops the cache-lock while awaiting each
        instance's :meth:`LightRAG.finalize_storages` so an in-progress
        teardown does not block unrelated acquires on the same loop.
        Safe to call multiple times; the second and subsequent calls
        short-circuit because the cache is empty.

        Tombstoned entries (``self._cache[ws] is None``) — left behind by
        :meth:`_evict_if_needed` whose background finalization is still
        in flight — are skipped here. The background :meth:`_safe_finalize`
        task that owns the tombstone will pop it on completion; we do not
        double-finalize the underlying rag.
        """
        async with self._cache_lock:
            snapshot: list[tuple[str, Optional[LightRAG]]] = list(self._cache.items())
            self._cache.clear()
            self._refcounts.clear()

        for workspace, rag in snapshot:
            if rag is None:
                # Tombstone — a background _safe_finalize task owns the
                # actual storage teardown. Our local cache entry was
                # already cleared above; nothing more to do here.
                logger.debug(
                    "finalize(): tombstoned workspace %r deferred to "
                    "background _safe_finalize task",
                    workspace,
                )
                continue
            try:
                await rag.finalize_storages()
            except Exception:  # noqa: BLE001 — best-effort teardown
                logger.exception(
                    "Error finalizing storages for workspace %r", workspace
                )
        logger.info(
            "WorkspaceManager finalized: tore down %d cached instance(s)",
            len(snapshot),
        )

    # ------------------------------------------------------------------ #
    # Public accessors                                                   #
    # ------------------------------------------------------------------ #

    def get_default_workspace(self) -> str:
        """Return the configured default workspace name.

        Used by callers that need to resolve a ``None`` / empty workspace
        identifier to its canonical form (e.g. the Ollama-compat API when
        the client did not supply an ``X-Workspace`` header).
        """
        return self.default_workspace

    async def get_default_instance(self) -> LightRAG:
        """Return the default-workspace :class:`LightRAG` instance.

        The default instance is created eagerly by :meth:`initialize`
        and is always expected to be present in the cache. Reads go
        through ``_cache_lock`` so the dict access is safe against a
        concurrent :meth:`finalize` that clears the cache; if the entry
        is missing (e.g. ``finalize`` already ran), this raises a
        :class:`RuntimeError` instead of leaking an opaque
        :class:`KeyError`. Callers that use this handle are expected to
        keep it scoped to a short-lived operation (e.g. the Ollama-compat
        API uses it once at module import to seed ``OllamaServerInfos``).

        Returns:
            The fully-initialized default-workspace :class:`LightRAG`
            instance.

        Raises:
            RuntimeError: If the default-workspace entry is missing from
                the cache (caller likely invoked this after
                :meth:`finalize` started its teardown) or is currently a
                tombstone (``None``) because its background finalization
                from :meth:`_evict_if_needed` is still in flight.
        """
        async with self._cache_lock:
            cached = self._cache.get(self.default_workspace)
            if cached is None:
                # Treat both "never initialized" and "tombstoned" as the
                # same RuntimeError so callers can't accidentally get a
                # None back when they asked for a fully-built instance.
                raise RuntimeError(
                    f"Default workspace '{self.default_workspace}' instance not initialized. "
                    "Call initialize() first."
                )
            return cached

    async def list_workspaces(self) -> list[dict]:
        """Return a snapshot of registered workspaces.

        Delegates to :meth:`WorkspaceRegistry.list_workspaces`. The
        cache contents (live instances) may differ from the registry
        (known names) when the cache is evicting or warming up.
        """
        return await self.registry.list_workspaces()

    async def get_registry(self) -> WorkspaceRegistry:
        """Return the underlying :class:`WorkspaceRegistry`.

        Exposed so routers can auto-register a workspace when they see
        a previously-unseen ``X-Workspace`` header, without coupling the
        routers to the manager's locking internals.
        """
        return self.registry

    # ------------------------------------------------------------------ #
    # Acquire / release                                                  #
    # ------------------------------------------------------------------ #

    async def acquire(self, workspace: Optional[str]) -> LightRAG:
        """Return a :class:`LightRAG` instance for ``workspace``, building one if needed.

        On **cache hit**: bumps the refcount, moves the entry to the
        MRU end of the LRU list, and returns the existing instance.

        On **cache miss**: tries to evict an unused entry to make room;
        if no unused entry exists, raises :class:`WorkspaceCacheFullError`.
        Otherwise, calls :meth:`_create_rag_instance` (under
        ``_init_lock``) and inserts the new instance with refcount 1.

        ``_cache_lock`` is held for the entire duration of this method —
        see the module-level docstring for the trade-off.

        Args:
            workspace: Workspace name. ``None``, empty string, and
                whitespace-only values are normalized to the configured
                default workspace.

        Returns:
            The :class:`LightRAG` instance for ``workspace`` with its
            refcount incremented by one.

        Raises:
            WorkspaceCacheFullError: If the cache is at capacity and no
                entry has refcount 0.
        """
        name = self._normalize_workspace(workspace)
        async with self._cache_lock:
            cached = self._cache.get(name)
            if cached is not None:
                self._refcounts[name] = self._refcounts.get(name, 0) + 1
                self._cache.move_to_end(name)
                logger.debug("acquire(%r): refcount=%d", name, self._refcounts[name])
                logger.info(
                    "acquire(%r): cache hit, refcount=%d",
                    name,
                    self._refcounts[name],
                )
                return cached

            # Cache miss: check capacity BEFORE building a new instance,
            # so we fail fast and don't waste a storage init on a doomed
            # workspace.
            if len(self._cache) >= self._max_instances:
                await self._evict_if_needed()

            # _init_lock serializes storage initialization across
            # concurrent acquires that miss on different workspaces.
            async with self._init_lock:
                rag = await self._create_rag_instance(name)
            self._cache[name] = rag
            self._refcounts[name] = 1
            await self.registry.register(name)
            logger.info(
                "acquire(%r): cache miss, built new instance (cache size %d / %d)",
                name,
                len(self._cache),
                self._max_instances,
            )
            return rag

    async def release(self, workspace: Optional[str]) -> None:
        """Decrement the refcount for ``workspace``.

        Refcount is clamped at zero — a release that would push it
        negative is logged as a warning (indicates a caller double-release
        or an out-of-order handler) but does not raise, so an exception
        in the finally-block of a request handler cannot crash the
        server.

        ``_cache_lock`` guards only the dict mutation; the cache lock
        is released before any storage teardown (which doesn't happen
        here anyway — eviction is lazy), so a misbehaving handler cannot
        stall ``acquire`` on this workspace.

        Args:
            workspace: Workspace name. ``None`` / empty / whitespace
                values are normalized to the default workspace.
        """
        name = self._normalize_workspace(workspace)
        async with self._cache_lock:
            current = self._refcounts.get(name, 0)
            if current <= 0:
                logger.warning(
                    "release(%r): refcount already 0; ignoring (possible "
                    "double-release or out-of-order handler)",
                    name,
                )
                return
            self._refcounts[name] = current - 1
            logger.debug("release(%r): refcount=%d", name, self._refcounts[name])

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _normalize_workspace(self, workspace: Optional[str]) -> str:
        """Collapse ``None`` / empty / whitespace ``workspace`` to the default.

        Centralizes the normalization rule so ``acquire`` and ``release``
        agree on which key they look up.
        """
        if workspace is None:
            return self.default_workspace
        normalized = workspace.strip()
        return normalized if normalized else self.default_workspace

    async def _create_rag_instance(self, workspace: str) -> LightRAG:
        """Construct, initialize, and migrate a :class:`LightRAG` for ``workspace``.

        Contract:
            * Callers MUST already hold ``self._init_lock`` before
              calling. The body does not (re)acquire it.
            * The order of operations is fixed: build the instance,
              register the role-LLM builder, initialize storages, then
              run :meth:`LightRAG.check_and_migrate_data`. The
              registered builder must be in place *before*
              ``initialize_storages`` so role-LLM dispatch works during
              any internal initialization calls.

        All args-derived kwargs are read via ``getattr`` with a ``None``
        default so a deployment that omits fields (or a test harness
        that stubs ``args``) can still construct an instance. Fields that
        are always expected (``embedding_func``, ``llm_model_func``, ...)
        are passed verbatim from the manager attributes.

        Args:
            workspace: Workspace name passed as ``workspace=`` to the
                :class:`LightRAG` constructor.

        Returns:
            A fully initialized, migration-checked :class:`LightRAG`
            instance.
        """
        args = self.args

        # Numeric conversion mirrors lightrag_server.py which calls int()
        # on chunk_size / chunk_overlap_size. Guard against None so a
        # missing-attr fallback does not blow up here.
        _chunk_size = getattr(args, "chunk_size", None)
        _chunk_overlap_size = getattr(args, "chunk_overlap_size", None)

        rag = LightRAG(
            working_dir=getattr(args, "working_dir", None),
            workspace=workspace,
            llm_model_func=self.llm_model_func,
            llm_model_name=getattr(args, "llm_model", None),
            llm_model_max_async=getattr(args, "max_async", None),
            summary_max_tokens=getattr(args, "summary_max_tokens", None),
            summary_context_size=getattr(args, "summary_context_size", None),
            chunk_token_size=int(_chunk_size) if _chunk_size is not None else None,
            chunk_overlap_token_size=(
                int(_chunk_overlap_size) if _chunk_overlap_size is not None else None
            ),
            llm_model_kwargs=self.llm_model_kwargs,
            embedding_func=self.embedding_func,
            default_llm_timeout=self.llm_timeout,
            default_embedding_timeout=self.embedding_timeout,
            kv_storage=getattr(args, "kv_storage", None),
            graph_storage=getattr(args, "graph_storage", None),
            vector_storage=getattr(args, "vector_storage", None),
            doc_status_storage=getattr(args, "doc_status_storage", None),
            vector_db_storage_cls_kwargs={
                "cosine_better_than_threshold": getattr(args, "cosine_threshold", None)
            },
            enable_llm_cache_for_entity_extract=getattr(
                args, "enable_llm_cache_for_extract", None
            ),
            enable_llm_cache=getattr(args, "enable_llm_cache", None),
            vlm_process_enable=getattr(args, "vlm_process_enable", None),
            rerank_model_func=self.rerank_model_func,
            rerank_model_max_async=getattr(args, "rerank_max_async", None),
            default_rerank_timeout=getattr(args, "rerank_timeout", None),
            max_parallel_insert=getattr(args, "max_parallel_insert", None),
            max_graph_nodes=getattr(args, "max_graph_nodes", None),
            addon_params={
                "language": getattr(args, "summary_language", None),
            },
            ollama_server_infos=self.ollama_server_infos,
            role_llm_configs=self.role_llm_configs,
        )

        # Register role LLM builder BEFORE initialize_storages so any
        # role-LLM dispatch that happens during storage init (e.g. on
        # first-touch warmup of a graph query) routes correctly.
        register_role_llm_builder(rag, self.args, self.llm_timeout)

        await rag.initialize_storages()
        await rag.check_and_migrate_data()
        return rag

    async def _evict_if_needed(self) -> None:
        """Evict the LRU entry with refcount 0, or raise if none can be evicted.

        Called from :meth:`acquire` while ``_cache_lock`` is already
        held. Walks the LRU list from the oldest to the newest end,
        skipping any tombstoned entry (``self._cache[ws] is None`` —
        a previous eviction whose background finalization is still in
        flight), and stops at the first non-tombstoned entry whose
        refcount is zero.

        The eviction itself uses a **tombstone + background finalize**
        pattern (see the module docstring):

        1. Inside the lock, replace the cache slot with ``None`` and
           pop the refcount entry. The slot is now un-selectable, so a
           concurrent ``acquire``-miss that hits the same capacity check
           cannot start a duplicate finalize for the same workspace.
        2. Schedule :meth:`_safe_finalize` via :func:`asyncio.create_task`
           to run the actual ``await rag.finalize_storages()`` OUTSIDE
           ``_cache_lock``. A slow teardown (closing PostgreSQL
           connections, flushing Redis) therefore no longer stalls
           ``acquire`` / ``release`` for unrelated workspaces.
        3. If every non-tombstoned entry has ``refcount > 0``, raises
           :class:`WorkspaceCacheFullError`.

        Failure mode: if ``rag.finalize_storages`` raises inside the
        background task, the tombstone is still popped (W2 fix). Without
        that, a permanently-failed eviction would leave a tombstoned
        entry that gets re-selected on every subsequent eviction and,
        if every other entry has ``refcount > 0``, deadlock the cache.

        Raises:
            WorkspaceCacheFullError: If every non-tombstoned entry has
                ``refcount > 0``.
        """
        for workspace, rag in self._cache.items():
            # Skip tombstoned entries. _refcounts.pop(ws, None) already
            # ran, so refcounts.get(ws, 0) would return 0 here; without
            # this guard the same tombstone would be re-selected on
            # every eviction attempt.
            if rag is None:
                continue
            if self._refcounts.get(workspace, 0) == 0:
                # Tombstone so re-entrant eviction and capacity checks
                # skip this slot until _safe_finalize pops it.
                self._cache[workspace] = None
                self._refcounts.pop(workspace, None)
                logger.info(
                    "Evicting workspace %r (finalize scheduled in "
                    "background); cache at %d / %d",
                    workspace,
                    len(self._cache),
                    self._max_instances,
                )
                # Run the storage teardown OUTSIDE the cache lock so a
                # slow backend shutdown cannot stall acquire/release on
                # the rest of the workspaces. The background task
                # removes the tombstone once it finishes (success OR
                # failure) — see _safe_finalize.
                asyncio.create_task(self._safe_finalize(workspace, rag))
                return

        logger.warning(
            "Cache full (size %d / %d) and every non-tombstoned entry has refcount > 0",
            len(self._cache),
            self._max_instances,
        )
        raise WorkspaceCacheFullError(self._max_instances)

    async def _safe_finalize(self, workspace: str, rag: LightRAG) -> None:
        """Finalize an evicted :class:`LightRAG` instance outside ``_cache_lock``.

        Background task spawned by :meth:`_evict_if_needed`. Awaits
        :meth:`LightRAG.finalize_storages` without holding the cache
        lock so a slow teardown does not stall other workspaces.

        Re-acquires the lock afterwards to remove the tombstone. The
        tombstone is popped only if the entry is still our ``None``
        sentinel — this guards against a race where
        :meth:`WorkspaceManager.finalize` cleared the cache (and
        possibly :meth:`acquire` rebuilt a fresh instance for the same
        workspace) while this task was awaiting ``finalize_storages``;
        in that case we must NOT remove the fresh instance.

        Failure handling: even if ``finalize_storages`` raises, the
        tombstone is still popped. Leaving the tombstone in place after
        a failed finalize would cause the entry to be re-selected on
        every subsequent eviction attempt and, if every other entry has
        ``refcount > 0``, deadlock the cache. The exception is logged
        so operators can diagnose storage shutdown problems.

        Args:
            workspace: Workspace name whose tombstone should be cleared.
            rag: The :class:`LightRAG` instance to finalize. The
                ``rag`` reference is owned by this background task;
                neither ``_cache`` nor ``_refcounts`` is touched except
                for the tombstone pop below.
        """
        try:
            await rag.finalize_storages()
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.exception(
                "Failed to finalize evicted workspace %r; clearing "
                "tombstone to avoid eviction-re-selection deadlock",
                workspace,
            )
        else:
            logger.debug(
                "Finalized evicted workspace %r (cache slot released)",
                workspace,
            )

        # Always pop the tombstone — but only if it's still OUR
        # tombstone. The conditional pop protects against finalize() +
        # rebuild races (see docstring above).
        async with self._cache_lock:
            if self._cache.get(workspace) is None:
                self._cache.pop(workspace, None)
