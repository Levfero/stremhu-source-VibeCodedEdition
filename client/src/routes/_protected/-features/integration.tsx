import { useQuery } from '@tanstack/react-query'

import {
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/shared/components/ui/select'
import { useIntegrationDomain } from '@/shared/hooks/use-integration-domain'
import { assertExists } from '@/shared/lib/utils'
import { getMe } from '@/shared/queries/me'

import { KodiIntegration } from './kodi-integration'
import { NuvioIntegration } from './nuvio-integration'
import { StremioIntegration } from './stremio-integration'

export function Integration() {
  const { data: me } = useQuery(getMe())
  assertExists(me)

  const integration = useIntegrationDomain({
    apiKey: me.apiKey,
  })

  return (
    <div className="grid gap-4">
      <CardHeader className="px-0">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <CardTitle>Támogatott kliensek</CardTitle>
            <CardDescription>
              Csatlakoztasd a StremHU Source-ot az általad használt kliensekhez.
            </CardDescription>
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
      </CardHeader>
      <div className="columns-1 md:columns-2 gap-4">
        <div className="break-inside-avoid mb-4">
          <StremioIntegration
            activeManifest={integration.activeManifest}
            stremio={integration.stremio}
          />
        </div>
        <div className="break-inside-avoid mb-4">
          <NuvioIntegration
            activeManifest={integration.activeManifest}
            nuvioUrl={integration.nuvioUrl}
          />
        </div>
        <div className="break-inside-avoid mb-4">
          <KodiIntegration
            activeManifest={integration.activeManifest}
            kodiUrl={integration.kodiUrl}
          />
        </div>
      </div>
    </div>
  )
}
