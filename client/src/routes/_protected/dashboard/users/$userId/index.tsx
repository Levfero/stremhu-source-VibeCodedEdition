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

  const { manifests, nuvioUrl } = useIntegrationDomain({
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
        <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          Kliens integrációs linkek
        </span>
        <div className="grid md:grid-cols-2 gap-4">
          {manifests.map((m) => (
            <Item key={m.id} variant="muted">
              <ItemContent>
                <div className="flex items-center gap-2">
                  <ItemTitle>{m.name} (Stremio)</ItemTitle>
                  <Badge variant="outline" className="text-[10px] shrink-0">
                    {m.badge}
                  </Badge>
                </div>
                <ItemDescription className="font-mono text-xs truncate max-w-[280px]">
                  {m.urlEndpoint}
                </ItemDescription>
              </ItemContent>
              <ItemActions>
                <Button
                  size="icon-sm"
                  className="rounded-full"
                  onClick={() => handleCopy(m.urlEndpoint)}
                  title={`${m.name} másolása`}
                >
                  <CopyIcon />
                </Button>
              </ItemActions>
            </Item>
          ))}
          <Item variant="muted">
            <ItemContent>
              <ItemTitle>Nuvio URL</ItemTitle>
              <ItemDescription className="font-mono text-xs truncate max-w-[280px]">
                {nuvioUrl}
              </ItemDescription>
            </ItemContent>
            <ItemActions>
              <Button
                size="icon-sm"
                className="rounded-full"
                onClick={() => handleCopy(nuvioUrl)}
                title="Nuvio URL másolása"
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
