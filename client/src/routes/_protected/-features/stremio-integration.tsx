import {
  AppWindowIcon,
  CodeIcon,
  CopyIcon,
  ExternalLinkIcon,
  LinkIcon,
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
import type { IntegrationManifestItem } from '@/shared/hooks/use-integration-domain'

interface StremioIntegrationProps {
  activeManifest?: IntegrationManifestItem
  stremio?: {
    appEndpoint: string
    webEndpoint: string
    urlEndpoint: string
  }
}

export function StremioIntegration({
  activeManifest,
  stremio,
}: StremioIntegrationProps) {
  const { handleCopy } = useCopy()

  const appEndpoint = stremio?.appEndpoint || ''
  const webEndpoint = stremio?.webEndpoint || ''
  const urlEndpoint = stremio?.urlEndpoint || ''

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Stremio</CardTitle>
          {activeManifest && (
            <Badge variant="outline" className="text-xs">
              {activeManifest.name}
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
              <a href={appEndpoint} target="_blank">
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
              <a href={webEndpoint} target="_blank">
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
              {urlEndpoint}
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button
              size="icon-sm"
              variant="default"
              className="rounded-full"
              onClick={() => handleCopy(urlEndpoint)}
            >
              <CopyIcon />
            </Button>
          </ItemActions>
        </Item>
      </CardContent>
    </Card>
  )
}
