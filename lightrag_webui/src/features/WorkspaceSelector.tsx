import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Layers } from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/Select'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/Tooltip'
import { useSettingsStore } from '@/stores/settings'
import { getWorkspaces, WorkspaceInfo } from '@/api/lightrag'

// Radix SelectItem rejects empty-string values; map the empty-string
// "no workspace" state to a sentinel string for the dropdown.
const DEFAULT_WORKSPACE_VALUE = '__default__'

const POLL_INTERVAL_MS = 30_000

export function WorkspaceSelector() {
  const { t } = useTranslation()
  const currentWorkspace = useSettingsStore.use.currentWorkspace()
  const setCurrentWorkspace = useSettingsStore.use.setCurrentWorkspace()
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([])

  useEffect(() => {
    let cancelled = false

    const fetchWorkspaces = async () => {
      try {
        const response = await getWorkspaces()
        if (!cancelled) {
          setWorkspaces(response.workspaces)
          // Reconcile: if the persisted workspace is no longer present on the
          // server, reset to default. Otherwise the axios interceptor would keep
          // injecting the stale header and re-materialize a deleted workspace.
          // Read/write imperatively to avoid stale closure capture from the
          // `[]`-dep useEffect above.
          const currentWs = useSettingsStore.getState().currentWorkspace
          if (
            currentWs !== '' &&
            !response.workspaces.some((ws) => ws.name === currentWs)
          ) {
            useSettingsStore.getState().setCurrentWorkspace('')
          }
        }
      } catch (error) {
        // Keep last known list on transient errors — better stale than blank.
        console.error('Failed to fetch workspaces:', error)
      }
    }

    fetchWorkspaces()

    const onFocus = () => {
      fetchWorkspaces()
    }
    window.addEventListener('focus', onFocus)
    const intervalId = window.setInterval(fetchWorkspaces, POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      window.removeEventListener('focus', onFocus)
      window.clearInterval(intervalId)
    }
  }, [])

  if (workspaces.length <= 1) {
    return null
  }

  const triggerValue = currentWorkspace === '' ? DEFAULT_WORKSPACE_VALUE : currentWorkspace

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Select
            value={triggerValue}
            onValueChange={(v) =>
              setCurrentWorkspace(v === DEFAULT_WORKSPACE_VALUE ? '' : v)
            }
          >
            <SelectTrigger
              className="h-8 w-[140px] cursor-pointer focus:ring-0 focus:ring-offset-0 focus:outline-0"
              aria-label={t('workspace.select')}
            >
              <Layers className="mr-1 h-3.5 w-3.5 opacity-60" aria-hidden="true" />
              <SelectValue placeholder={t('workspace.default')} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_WORKSPACE_VALUE}>
                {t('workspace.default')}
              </SelectItem>
              {workspaces.map((ws) => (
                <SelectItem key={ws.name} value={ws.name}>
                  {ws.name}
                  {ws.document_count != null ? ` (${ws.document_count} docs)` : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </TooltipTrigger>
        <TooltipContent side="bottom">{t('workspace.title')}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
