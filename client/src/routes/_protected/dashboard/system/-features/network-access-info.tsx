import { useQuery, useSuspenseQueries } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { SettingsIcon } from 'lucide-react'

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
  ItemTitle,
} from '@/shared/components/ui/item'
import { Separator } from '@/shared/components/ui/separator'
import { formatDateTime } from '@/shared/lib/utils'
import { getHealth } from '@/shared/queries/app'
import { getNetworkSettings } from '@/shared/queries/network'
import { getSystemStatus } from '@/shared/queries/system'

const networkCheckMap = {
  idle: {
    title: '🔎 Elérés ellenőrzése...',
  },
  pending: {
    title: '🔎 Elérés ellenőrzése...',
  },
  success: {
    title: '🟢 Elérés rendben',
  },
  error: {
    title: '🔴 Nem érhető el a megadott címen',
  },
}

export function NetworkAccessInfo() {
  const [{ data: systemStatus }, { data: networkSettings }] =
    useSuspenseQueries({
      queries: [getSystemStatus, getNetworkSettings],
    })

  const { status: healthStatus } = useQuery(getHealth(systemStatus.appUrl))

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Hálózati elérés</CardTitle>
            <CardDescription>
              Hálózati eléréssel kapcsolatos információk
            </CardDescription>
          </div>
          <Button
            size="icon-sm"
            variant="outline"
            className="rounded-full shrink-0"
            asChild
          >
            <Link
              to="/dashboard/system/network"
              title="Hálózati elérések és manifestek kezelése"
            >
              <SettingsIcon />
            </Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-2">
          <Item variant="default" className="p-0">
            <ItemContent>
              <ItemTitle>{networkCheckMap[healthStatus].title}</ItemTitle>
              <ItemDescription className="font-bold font-mono break-all">
                {systemStatus.appUrl}
              </ItemDescription>
            </ItemContent>
          </Item>
          {networkSettings.mode === 'auto' && (
            <>
              <Item className="p-0">
                <ItemContent>
                  <ItemTitle>Tanúsítvány lejárata / frissítése</ItemTitle>
                  <ItemDescription className="font-bold font-mono break-all">
                    {formatDateTime(networkSettings.expiresAt)}
                  </ItemDescription>
                </ItemContent>
              </Item>
              <Item className="p-0">
                <ItemContent>
                  <ItemTitle>Utolsó IP szinkronizáció</ItemTitle>
                  <ItemDescription className="font-bold font-mono break-all">
                    {formatDateTime(networkSettings.lastIpSyncAt)}
                  </ItemDescription>
                </ItemContent>
              </Item>
            </>
          )}
        </div>

        {systemStatus.manifestEndpoints &&
          systemStatus.manifestEndpoints.length > 0 && (
            <>
              <Separator />
              <div className="grid gap-2">
                <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                  Konfigurált kliens elérések
                </span>
                <div className="grid gap-1.5 text-xs font-mono">
                  {systemStatus.manifestEndpoints.map((ep) => (
                    <div
                      key={ep.id}
                      className="flex items-center justify-between gap-2 p-2 rounded-lg bg-muted/40 border border-border/40"
                    >
                      <span className="font-medium font-sans">{ep.name}:</span>
                      <span className="text-muted-foreground truncate">
                        {ep.baseUrl}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}

        <Separator />
        <Item className="p-0 flex-nowrap">
          <ItemContent className="min-w-0">
            <ItemTitle>
              {systemStatus.isReverseProxy
                ? 'Kliens elérések és manifestek kezelése'
                : 'Hálózati elérés frissítése vagy új konfiguráció alkalmazása.'}
            </ItemTitle>
            <ItemDescription>
              {systemStatus.isReverseProxy
                ? 'A fő elérést a REVERSE_PROXY_DOMAIN környezeti változó vezérli, de a LAN, Tailscale és egyedi elérések itt módosíthatók.'
                : 'Állítsd be a szerver hálózati elérését és a kliens manifesteket.'}
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button size="icon-sm" className="rounded-full shrink-0" asChild>
              <Link to="/dashboard/system/network">
                <SettingsIcon />
              </Link>
            </Button>
          </ItemActions>
        </Item>
      </CardContent>
    </Card>
  )
}
