import { useQueries } from '@tanstack/react-query'
import { createFileRoute, useParams } from '@tanstack/react-router'
import { CopyIcon } from 'lucide-react'

import { TokenRegenerate } from '@/features/token-regenerate/token-regenerate'
import { Badge } from '@/shared/components/ui/badge'
import { Button } from '@/shared/components/ui/button'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemTitle,
} from '@/shared/components/ui/item'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/shared/components/ui/select'
import { Separator } from '@/shared/components/ui/separator'
import { useCopy } from '@/shared/hooks/use-copy'
import { useIntegrationDomain } from '@/shared/hooks/use-integration-domain'
import { assertExists } from '@/shared/lib/utils'
import { getUser, useRegenerateUserToken } from '@/shared/queries/users'

import { MaxConcurrentStreams } from './-features/max-concurrent-streams'
import { UserProfile } from './-features/user-profile'

export const Route = createFileRoute('/_protected/dashboard/users/$userId/')({
  component: UserRoute,
})

function UserRoute() {
  const { userId } = useParams({ from: '/_protected/dashboard/users/$userId/' })

  const [{ data: user }] = useQueries({
    queries: [getUser(userId)],
  })
  assertExists(user)

  const integration = useIntegrationDomain({
    apiKey: user.apiKey,
  })

  const { handleCopy } = useCopy()

  const { mutateAsync: regenerateUserToken } = useRegenerateUserToken()

  const handleRegenerateToken = async () => {
    await regenerateUserToken(user.id)
  }

  return (
    <div className="grid gap-8">
      <div className="columns-1 md:columns-2 gap-4">
        <div className="break-inside-avoid mb-4">
          <UserProfile user={user} />
        </div>
        <div className="break-inside-avoid mb-4">
          <TokenRegenerate onSubmit={handleRegenerateToken} />
        </div>
        <div className="break-inside-avoid mb-4">
          <MaxConcurrentStreams user={user} />
        </div>
      </div>
      <Separator />
      <div className="grid gap-3">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
              Kliens integrációs linkek
            </span>
          </div>
          {integration.manifests.length > 1 && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground whitespace-nowrap">
                Elérés:
              </span>
              <Select
                value={integration.selectedId}
                onValueChange={integration.setSelectedId}
              >
                <SelectTrigger className="w-[190px] h-9 font-medium text-xs">
                  <SelectValue placeholder="Válassz elérést" />
                </SelectTrigger>
                <SelectContent align="end">
                  {integration.manifests.map((m) => (
                    <SelectItem key={m.id} value={m.id} className="text-xs">
                      {m.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          <Item variant="muted" className="flex-nowrap">
            <ItemContent className="min-w-0">
              <div className="flex items-center gap-2">
                <ItemTitle>Stremio</ItemTitle>
                <Badge variant="outline" className="text-[10px] shrink-0">
                  {integration.activeManifest.badge}
                </Badge>
              </div>
              <ItemDescription className="font-mono text-xs truncate">
                {integration.stremio.urlEndpoint}
              </ItemDescription>
            </ItemContent>
            <ItemActions>
              <Button
                size="icon-sm"
                className="rounded-full shrink-0"
                onClick={() => handleCopy(integration.stremio.urlEndpoint)}
                title="Stremio URL másolása"
              >
                <CopyIcon />
              </Button>
            </ItemActions>
          </Item>
          <Item variant="muted" className="flex-nowrap">
            <ItemContent className="min-w-0">
              <div className="flex items-center gap-2">
                <ItemTitle>Nuvio</ItemTitle>
                <Badge variant="outline" className="text-[10px] shrink-0">
                  {integration.activeManifest.badge}
                </Badge>
              </div>
              <ItemDescription className="font-mono text-xs truncate">
                {integration.nuvioUrl}
              </ItemDescription>
            </ItemContent>
            <ItemActions>
              <Button
                size="icon-sm"
                className="rounded-full shrink-0"
                onClick={() => handleCopy(integration.nuvioUrl)}
                title="Nuvio URL másolása"
              >
                <CopyIcon />
              </Button>
            </ItemActions>
          </Item>
          <Item variant="muted" className="flex-nowrap">
            <ItemContent className="min-w-0">
              <div className="flex items-center gap-2">
                <ItemTitle>Kodi</ItemTitle>
                <Badge variant="outline" className="text-[10px] shrink-0">
                  {integration.activeManifest.badge}
                </Badge>
              </div>
              <ItemDescription className="font-mono text-xs truncate">
                {integration.kodiUrl}
              </ItemDescription>
            </ItemContent>
            <ItemActions>
              <Button
                size="icon-sm"
                className="rounded-full shrink-0"
                onClick={() => handleCopy(integration.kodiUrl)}
                title="Kodi URL másolása"
              >
                <CopyIcon />
              </Button>
            </ItemActions>
          </Item>
        </div>
      </div>
    </div>
  )
}
