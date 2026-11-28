import { useQuery } from '@tanstack/react-query'
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
import { getVpn } from '@/shared/queries/vpn'

export function VpnInfo() {
  const { data: vpn } = useQuery(getVpn)

  const summary = !vpn
    ? 'Állapot lekérdezése...'
    : vpn.enabled
      ? vpn.state === 'connected'
        ? '🟢 A torrentforgalom VPN-en megy'
        : '🟡 VPN bekapcsolva, a kapcsolat nem él: a torrentforgalom áll'
      : '⚪ Kikapcsolva'

  return (
    <Card>
      <CardHeader>
        <CardTitle>VPN & Adatvédelem</CardTitle>
        <CardDescription>
          WireGuard VPN a torrent le- és feltöltéséhez
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Item className="p-0">
          <ItemContent>
            <ItemTitle>{summary}</ItemTitle>
            <ItemDescription>
              A streamelés és a weboldal nem megy a VPN-en.
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Button size="icon-sm" className="rounded-full" asChild>
              <Link to="/dashboard/system/vpn">
                <SettingsIcon />
              </Link>
            </Button>
          </ItemActions>
        </Item>
      </CardContent>
    </Card>
  )
}
