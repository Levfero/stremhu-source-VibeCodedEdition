import { CopyIcon, LinkIcon } from 'lucide-react'

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

interface NuvioIntegrationProps {
  activeManifest?: IntegrationManifestItem
  nuvioUrl?: string
}

export function NuvioIntegration({
  activeManifest,
  nuvioUrl,
}: NuvioIntegrationProps) {
  const { handleCopy } = useCopy()

  const url = nuvioUrl || ''

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Nuvio</CardTitle>
          {activeManifest && (
            <Badge variant="outline" className="text-xs">
              {activeManifest.name}
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
        <Item variant="default" className="p-0 flex-nowrap">
          <ItemMedia variant="icon">
            <LinkIcon />
          </ItemMedia>
          <ItemContent className="min-w-0">
            <ItemTitle>URL másolása</ItemTitle>
            <ItemDescription>
              A kimásolt URL-t illeszd be az addonok közé.
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button
              size="icon-sm"
              className="rounded-full shrink-0"
              onClick={() => handleCopy(url)}
            >
              <CopyIcon />
            </Button>
          </ItemActions>
        </Item>
      </CardContent>
    </Card>
  )
}
