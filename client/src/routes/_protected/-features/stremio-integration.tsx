import { useQuery } from '@tanstack/react-query'
import {
  AppWindowIcon,
  CloudIcon,
  CodeIcon,
  CopyIcon,
  ExternalLinkIcon,
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

export function StremioIntegration() {
  const { data: me } = useQuery(getMe())
  assertExists(me)

  const { manifests, selectedId, setSelectedId, activeManifest, stremio } =
    useIntegrationDomain({
      apiKey: me.apiKey,
    })

  const { handleCopy } = useCopy()

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Stremio</CardTitle>
          {activeManifest && (
            <Badge variant="outline" className="text-xs">
              {activeManifest.badge}
            </Badge>
          )}
        </div>
        <CardDescription>
          <a
            className="link-primary"
            href="https://www.stremio.com"
            target="_blank"
          >
            https://www.stremio.com
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
                    'flex-1 min-w-[80px] px-3 py-1.5 text-xs font-medium rounded-lg transition-all flex items-center justify-center gap-1.5 cursor-pointer',
                    isSelected
                      ? 'bg-background text-foreground shadow-sm font-semibold'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  {icon}
                  <span>{m.badge || (m.id === 'lan' ? 'LAN' : m.id === 'tailscale' ? 'Tailscale' : 'Cloudflare')}</span>
                </button>
              )
            })}
          </div>
        )}

        <Item variant="default" className="p-0">
          <ItemMedia variant="icon">
            <AppWindowIcon />
          </ItemMedia>
          <ItemContent>
            <ItemTitle>Telepítés appban</ItemTitle>
            <ItemDescription>
              Megnyitja az alkalmazást és hozzáadja az addont.
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button
              size="icon-sm"
              variant="default"
              className="rounded-full"
              asChild
            >
              <a href={stremio.appEndpoint} target="_blank">
                <ExternalLinkIcon />
              </a>
            </Button>
          </ItemActions>
        </Item>
        <Item variant="default" className="p-0">
          <ItemMedia variant="icon">
            <CodeIcon />
          </ItemMedia>
          <ItemContent>
            <ItemTitle>Telepítés weben</ItemTitle>
            <ItemDescription>
              A weboldalon jóváhagyással telepítheted az addont.
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button
              size="icon-sm"
              variant="default"
              className="rounded-full"
              asChild
            >
              <a href={stremio.webEndpoint} target="_blank">
                <ExternalLinkIcon />
              </a>
            </Button>
          </ItemActions>
        </Item>
        <Item variant="default" className="p-0">
          <ItemMedia variant="icon">
            <LinkIcon />
          </ItemMedia>
          <ItemContent>
            <ItemTitle>URL másolása</ItemTitle>
            <ItemDescription className="truncate max-w-[260px] font-mono text-xs">
              {stremio.urlEndpoint}
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button
              size="icon-sm"
              variant="default"
              className="rounded-full"
              onClick={() => handleCopy(stremio.urlEndpoint)}
            >
              <CopyIcon />
            </Button>
          </ItemActions>
        </Item>
      </CardContent>
    </Card>
  )
}
