import { useQuery } from '@tanstack/react-query'
import {
  CloudIcon,
  CopyIcon,
  HardDriveIcon,
  LinkIcon,
  ShieldIcon,
} from 'lucide-react'

import { Badge } from '@/shared/components/ui/badge'
import { Button } from '@/shared/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from '@/shared/components/ui/item'
import { useCopy } from '@/shared/hooks/use-copy'
import { useIntegrationDomain } from '@/shared/hooks/use-integration-domain'
import { assertExists, cn } from '@/shared/lib/utils'
import { getMe } from '@/shared/queries/me'

export function NuvioIntegration() {
  const { data: me } = useQuery(getMe())
  assertExists(me)

  const { manifests, selectedId, setSelectedId, activeManifest, nuvioUrl } =
    useIntegrationDomain({
      apiKey: me.apiKey,
    })

  const { handleCopy } = useCopy()

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Nuvio</CardTitle>
          {activeManifest && (
            <Badge variant="outline" className="text-xs">
              {activeManifest.badge}
            </Badge>
          )}
        </div>
        <CardDescription>
          <a className="link-primary" href="https://nuvio.tv/" target="_blank">
            https://nuvio.tv
          </a>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {manifests.length > 1 && (
          <div className="flex flex-wrap gap-1.5 p-1 bg-muted/80 rounded-xl border border-border/50">
            {manifests.map((m) => {
              const isSelected = m.id === selectedId
              const icon =
                m.id === 'lan' ? (
                  <HardDriveIcon className="size-3.5" />
                ) : m.id === 'tailscale' ? (
                  <ShieldIcon className="size-3.5" />
                ) : (
                  <CloudIcon className="size-3.5" />
                )
              return (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => setSelectedId(m.id)}
                  className={cn(
                    'flex-1 min-w-[100px] px-3 py-1.5 text-xs font-medium rounded-lg transition-all flex items-center justify-center gap-1.5 cursor-pointer',
                    isSelected
                      ? 'bg-background text-foreground shadow-sm font-semibold'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  {icon}
                  <span>{m.name}</span>
                </button>
              )
            })}
          </div>
        )}

        <Item variant="default" className="p-0">
          <ItemMedia variant="icon">
            <LinkIcon />
          </ItemMedia>
          <ItemContent>
            <ItemTitle>URL másolása ({activeManifest.name})</ItemTitle>
            <ItemDescription className="truncate max-w-[260px] font-mono text-xs">
              {nuvioUrl}
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button
              size="icon-sm"
              className="rounded-full"
              onClick={() => handleCopy(nuvioUrl)}
            >
              <CopyIcon />
            </Button>
          </ItemActions>
        </Item>
      </CardContent>
    </Card>
  )
}
