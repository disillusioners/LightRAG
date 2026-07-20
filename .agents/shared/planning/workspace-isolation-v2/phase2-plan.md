# Phase 2: Frontend — UI Workspace Switching

> **Revision 2** — Addresses C5 (sanitization regex alignment), W1 (registry deferral note), W3 (`/health` auth), W4 (document_count nullable).

## Objective

Add a workspace selector dropdown to the web UI header, inject the `LIGHTRAG-WORKSPACE` header into all outgoing API requests (both axios REST and fetch streaming), and persist the selected workspace in the settings store with proper migration.

## Coupling

- **Depends on**: Phase 1 (needs `GET /workspaces` endpoint to exist)
- **Coupling type**: **loose** — Frontend only needs the API response contract defined in `phase1-plan.md`. Can be developed against a mock. Does NOT need Phase 1's internal implementation.
- **Shared files with other phases**: None (frontend files are independent of backend)
- **Shared APIs/interfaces**: `GET /workspaces` response shape (from Phase 1)
- **Why this coupling**: The frontend consumes the `/workspaces` API. As long as the response shape matches the contract, the frontend is decoupled from the backend implementation.

**Parallelization opportunity**: Phase 2 can begin immediately against the documented API contract. The `/workspaces` path is already proxied in `lightrag_webui/.env` (`VITE_API_ENDPOINTS=/api,/docs,/redoc,/openapi.json,/static,/workspaces`).

---

## Tasks

### Task 1: Add `currentWorkspace` to Settings Store

**File**: `lightrag_webui/src/stores/settings.ts` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 1a | Add `currentWorkspace` field to `SettingsState` interface | `currentWorkspace: string` + `setCurrentWorkspace(workspace: string): void` | In the SettingsState type definition |
| 1b | Add default value in store factory | `currentWorkspace: ''` in the initial state object | In the `create<SettingsState>()(persist(...))` call |
| 1c | Add `setCurrentWorkspace` action | `setCurrentWorkspace: (workspace) => set({ currentWorkspace: workspace })` | In the actions section |
| 1d | Bump store version | `version: 20` → `version: 21` | In persist config |
| 1e | Add migration | `if (version < 21) { state.currentWorkspace = '' }` | In `migrate` function, after the v20 block |

**Migration pattern** (following existing convention):
```ts
// Version 20 → 21: Add currentWorkspace field
if (version < 21) {
  state.currentWorkspace = ''
}
```

### Task 2: Add `LIGHTRAG-WORKSPACE` Header to API Client

**File**: `lightrag_webui/src/api/lightrag.ts` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 2a | Add header to axios request interceptor | After the auth headers, add: `const ws = useSettingsStore.getState().currentWorkspace; if (ws) { config.headers['LIGHTRAG-WORKSPACE'] = sanitizeWorkspaceHeader(ws) }` | L419-437 (request interceptor) |
| 2b | Add header to `_buildStreamHeaders()` | After auth headers, add same logic: `const ws = useSettingsStore.getState().currentWorkspace; if (ws) { headers['LIGHTRAG-WORKSPACE'] = sanitizeWorkspaceHeader(ws) }` | L691-705 |
| 2c | Add `sanitizeWorkspaceHeader(ws: string)` helper | Client-side sanitization aligned with backend (C5). See below. | New helper function |

#### C5 Sanitization Alignment (VERIFIED)

**Backend regex (after Phase 1 fix):** `re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)`
**Frontend regex must match:** `/[^a-zA-Z0-9_-]/g`

> **C5 FIX:** The original plan used `/[^a-z0-9_-]/g` (lowercase only) which would break for uppercase workspace names. The backend regex accepts both upper and lowercase (`a-zA-Z`). The frontend regex MUST use `/[^a-zA-Z0-9_-]/g` to match exactly.

**Sanitization helper:**
```ts
/**
 * Sanitize workspace name for the LIGHTRAG-WORKSPACE header.
 * Must match backend regex: re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)
 * (C5: aligned to accept hyphens, matching validate_workspace())
 */
function sanitizeWorkspaceHeader(workspace: string): string {
  // Match backend: allow alphanumeric, underscores, and hyphens
  const sanitized = workspace.replace(/[^a-zA-Z0-9_-]/g, '_')
  return sanitized.substring(0, 64)
}
```

> **Why no lowercase conversion:** The backend regex `[^a-zA-Z0-9_-]` accepts uppercase letters. Workspace names like `TenantA` and `tenantA` are different namespaces on the backend (via `get_final_namespace()`). Converting to lowercase on the frontend would cause a mismatch.

### Task 3: Add `getWorkspaces()` API Function

**File**: `lightrag_webui/src/api/lightrag.ts` (MODIFY)

| # | Sub-task | Details |
|---|----------|---------|
| 3a | Define response types | `WorkspaceInfo { name: string; created_at?: string; document_count?: number | null }` and `WorkspacesResponse { workspaces: WorkspaceInfo[]; default_workspace: string }` — note `document_count` is nullable per W4 |
| 3b | Add `getWorkspaces()` function | `export async function getWorkspaces(): Promise<WorkspacesResponse> { const response = await axiosInstance.get('/workspaces'); return response.data }` |
| 3c | Export types | Add to module exports |

**Location**: Add near the other GET functions (e.g., after `checkHealth()` at ~L390).

### Task 4: Create Workspace Selector Component

**File**: `lightrag_webui/src/features/WorkspaceSelector.tsx` (NEW)

| # | Sub-task | Details |
|---|----------|---------|
| 4a | Define component | React component using Radix `Select` from `components/ui/Select.tsx` |
| 4b | Fetch workspace list | Call `getWorkspaces()` on mount via `useEffect`. Store in local state. |
| 4c | Render dropdown | `Select` trigger (compact, icon-style to match header height h-10) with workspace list as options |
| 4d | Handle selection | On change: `useSettingsStore.getState().setCurrentWorkspace(selectedWorkspace)` |
| 4e | Show current workspace | `SelectValue` displays current workspace or "Default" if empty |
| 4f | Refresh capability | Re-fetch on window focus + 30s interval (simple approach; see Task 7) |
| 4g | Empty state | If only default workspace exists, don't render the selector (return `null`) |

**Component structure:**
```tsx
export function WorkspaceSelector() {
  const currentWorkspace = useSettingsStore.use.currentWorkspace()
  const setCurrentWorkspace = useSettingsStore.use.setCurrentWorkspace()
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([])

  useEffect(() => {
    getWorkspaces().then(data => setWorkspaces(data.workspaces)).catch(console.error)
  }, [])

  // If only default workspace exists, don't show selector
  if (workspaces.length <= 1) return null

  return (
    <Select value={currentWorkspace} onValueChange={setCurrentWorkspace}>
      <SelectTrigger className="h-8 w-[140px]">
        <SelectValue placeholder="Default" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="">Default</SelectItem>
        {workspaces.map(ws => (
          <SelectItem key={ws.name} value={ws.name}>{ws.name}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
```

### Task 5: Integrate Workspace Selector into SiteHeader

**File**: `lightrag_webui/src/features/SiteHeader.tsx` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 5a | Import WorkspaceSelector | `import { WorkspaceSelector } from './WorkspaceSelector'` | Top imports |
| 5b | Insert component in header layout | Place in the right-side nav, before `<AppSettings />` | In the `<nav>` element (L77) |

**Target layout (right nav):**
```tsx
<nav className="w-[200px] flex items-center justify-end gap-2">
  <WorkspaceSelector />     {/* NEW */}
  <Button variant="ghost" ... GitHub />
  <AppSettings />
  {!isGuestMode && <Button ... LogOutIcon />}
</nav>
```

### Task 6: Add i18n Translation Keys

**Files**: `lightrag_webui/src/locales/*.json` (MODIFY — all 11 locale files)

| # | Sub-task | Details |
|---|----------|---------|
| 6a | Add `header.workspace` key | `"header": { "workspace": "Workspace" }` |
| 6b | Add `header.defaultWorkspace` key | `"header": { "defaultWorkspace": "Default" }` |
| 6c | Update all locale files | `en.json`, `zh.json`, `fr.json`, `ar.json`, `zh_TW.json`, `ru.json`, `ja.json`, `de.json`, `uk.json`, `ko.json`, `vi.json` |

### Task 7: Handle Workspace Refresh Triggers

**File**: `lightrag_webui/src/features/WorkspaceSelector.tsx` (MODIFY — extends Task 4)

| # | Sub-task | Details |
|---|----------|---------|
| 7a | Refresh on window focus | Re-fetch workspace list when browser tab regains focus |
| 7b | Periodic refresh | Re-fetch every 30s (simple, low-overhead) |
| 7c | (Future) Event-driven refresh | When `/documents/upload` response indicates a new workspace was registered, trigger refresh via custom event |

**Recommendation**: Start with window focus + 30s interval. Move to event-driven in a follow-up.

---

## Key Files

### New Files
- `lightrag_webui/src/features/WorkspaceSelector.tsx` — Workspace dropdown selector component

### Modified Files
- `lightrag_webui/src/stores/settings.ts` — Add `currentWorkspace` field + migration to v21
- `lightrag_webui/src/api/lightrag.ts` — Add header injection (interceptor + stream headers) + `getWorkspaces()` function + `sanitizeWorkspaceHeader()` helper
- `lightrag_webui/src/features/SiteHeader.tsx` — Add WorkspaceSelector to header
- `lightrag_webui/src/locales/*.json` — Add translation keys (11 files)

### Reference Files (existing UI components to reuse)
- `lightrag_webui/src/components/ui/Select.tsx` — Radix Select wrapper (primary choice)
- `lightrag_webui/src/components/ui/Popover.tsx` — Alternative for custom dropdown
- `lightrag_webui/src/components/ui/AsyncSelect.tsx` — For async-loaded workspace list
- `lightrag_webui/src/stores/settings.ts` — Migration pattern reference (v2-v20)

---

## Existing Infrastructure to Leverage

| Resource | Location | Why |
|----------|----------|-----|
| Radix `Select` component | `components/ui/Select.tsx` | Standard dropdown — matches existing UI patterns |
| `createSelectors` wrapper | `stores/settings.ts` | Store access via `.use.fieldName()` |
| `useSettingsStore.getState()` | Used in `lightrag.ts` interceptor | Non-reactive state access for header injection |
| Vite proxy `/workspaces` | `.env: VITE_API_ENDPOINTS` | Already configured — no proxy changes needed |
| `LightragStatus.configuration.workspace` | `api/lightrag.ts` type | Already typed — can display default workspace |
| `createJSONStorage(() => localStorage)` | `stores/settings.ts` | Persistence mechanism for `currentWorkspace` |

---

## Constraints

1. **Follow store migration pattern** — linear `if (version < N)` blocks, no breaking changes
2. **Use existing UI components** — Radix `Select` from `components/ui/Select.tsx`
3. **Header height is h-10 (40px)** — selector must be compact (h-8 max)
4. **Empty workspace = no header sent** — backward compatible. Only send `LIGHTRAG-WORKSPACE` when `currentWorkspace` is non-empty.
5. **Both axios and fetch paths** — the header must be in the request interceptor AND `_buildStreamHeaders()`
6. **Client-side sanitization MUST match backend regex** — `/[^a-zA-Z0-9_-]/g` (C5). No lowercase conversion — backend accepts both cases.
7. **No new dependencies** — use existing Radix UI, zustand, axios, etc.
8. **`document_count` is nullable** (W4) — type it as `number | null`, handle null in UI

---

## Deliverables

- [ ] `lightrag_webui/src/stores/settings.ts` — `currentWorkspace` field + v21 migration
- [ ] `lightrag_webui/src/api/lightrag.ts` — Header injection (C5-aligned regex) + `getWorkspaces()` + `sanitizeWorkspaceHeader()`
- [ ] `lightrag_webui/src/features/WorkspaceSelector.tsx` — Workspace selector component
- [ ] `lightrag_webui/src/features/SiteHeader.tsx` — Workspace selector integrated
- [ ] `lightrag_webui/src/locales/*.json` — Translation keys (11 files)
- [ ] Manual test: select workspace in UI → verify header sent in DevTools Network tab
- [ ] Manual test: refresh page → workspace selection persists (localStorage)
- [ ] Manual test: empty workspace → no header sent (backward compatible)
- [ ] Manual test: workspace name with hyphens (e.g., `my-tenant`) → not rewritten by backend (C5 verified)
