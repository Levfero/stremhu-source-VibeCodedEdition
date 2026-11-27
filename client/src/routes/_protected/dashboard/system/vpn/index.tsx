import { useSuspenseQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { InfoIcon } from 'lucide-react'
import type { ChangeEvent } from 'react'
import { useState } from 'react'
import { toast } from 'sonner'

import { useConfirmDialog } from '@/features/confirm/use-confirm-dialog'
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from '@/shared/components/ui/alert'
import { Button } from '@/shared/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import { Label } from '@/shared/components/ui/label'
import { Separator } from '@/shared/components/ui/separator'
import { Switch } from '@/shared/components/ui/switch'
import { Textarea } from '@/shared/components/ui/textarea'
import type { VpnState, VpnTestResponse } from '@/shared/lib/source/vpn-client'
import { formatDateTime, parseApiError } from '@/shared/lib/utils'
import {
  getVpn,
  useDeleteVpnConfig,
  useTestVpn,
  useUpdateVpn,
} from '@/shared/queries/vpn'

import { DASHBOARD_SYSTEM_VPN_NAME } from './route'

export const Route = createFileRoute('/_protected/dashboard/system/vpn/')({
  component: VpnRoute,
})

const stateLabels: Record<VpnState, string> = {
  disabled: '⚪ Kikapcsolva',
  starting: '🟡 Kapcsolódás...',
  connected: '🟢 Kapcsolódva',
  reconnecting: '🟡 Újrakapcsolódás...',
  error: '🔴 Hiba: a torrentforgalom áll',
}

const checkLabels: Record<string, string> = {
  'vpn-enabled': 'VPN bekapcsolva',
  'relay-proxy-mode': 'A torrentforgalom proxyn megy',
  'relay-settings': 'DHT, uTP, UPnP és bejövő port tiltva',
  'proxy-listening': 'A helyi VPN-proxy fut',
  handshake: 'WireGuard-kapcsolat ellenőrzése',
}

export function VpnRoute() {
  const { data: vpn } = useSuspenseQuery(getVpn)
  const { mutateAsync: updateVpn, isPending: isUpdating } = useUpdateVpn()
  const { mutateAsync: deleteConfig } = useDeleteVpnConfig()
  const { mutateAsync: testVpn, isPending: isTesting } = useTestVpn()
  const confirmDialog = useConfirmDialog()
  const [configText, setConfigText] = useState('')
  const [testResult, setTestResult] = useState<VpnTestResponse | null>(null)

  const run = async (action: () => Promise<unknown>) => {
    try {
      await action()
    } catch (error) {
      toast.error(parseApiError(error))
    }
  }

  const handleToggle = async (checked: boolean) => {
    if (checked) {
      await run(() => updateVpn({ enabled: true }))
      return
    }
    await confirmDialog.confirm({
      title: 'Kikapcsolod a VPN-t?',
      description:
        'Kikapcsolás után a torrentek le- és feltöltése a saját IP-címedről, VPN nélkül megy.',
      confirmText: 'Kikapcsolás',
      onConfirm: async () => {
        await run(() => updateVpn({ enabled: false, confirmUnprotected: true }))
      },
    })
  }

  const handleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file) {
      setConfigText(await file.text())
    }
  }

  const handleSave = async () => {
    await run(async () => {
      await updateVpn({ config: configText })
      setConfigText('')
      toast.success('A WireGuard-konfiguráció elmentve.')
    })
  }

  const handleDelete = async () => {
    await confirmDialog.confirm({
      title: 'Törlöd a VPN-konfigurációt?',
      description: 'A mentett WireGuard-konfiguráció véglegesen törlődik.',
      confirmText: 'Törlés',
      onConfirm: async () => {
        await run(() => deleteConfig())
      },
    })
  }

  const handleTest = async () => {
    await run(async () => {
      setTestResult(await testVpn())
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{DASHBOARD_SYSTEM_VPN_NAME}</CardTitle>
        <CardDescription>
          Beépített WireGuard VPN a torrentforgalomhoz (pl. Proton VPN, P2P
          szerver).
        </CardDescription>
      </CardHeader>
      <Separator />
      <CardContent className="grid gap-6 pt-6">
        <Alert>
          <InfoIcon />
          <AlertTitle>Mi megy a VPN-en?</AlertTitle>
          <AlertDescription>
            Csak a torrent le- és feltöltése megy a VPN-en; a streamelés és a
            weboldal nem. A lejátszás adata viszont a torrentből jön, ezért a
            VPN sebessége hat az indulásra. VPN módban nincs porttovábbítás, a
            DHT és az uTP ki van kapcsolva.
          </AlertDescription>
        </Alert>

        <div className="grid gap-2">
          <p className="font-medium">{stateLabels[vpn.state]}</p>
          {vpn.errorCode && (
            <p className="text-sm text-muted-foreground">
              Hibakód: <span className="font-mono">{vpn.errorCode}</span>
            </p>
          )}
          {vpn.connectedAt && (
            <p className="text-sm text-muted-foreground">
              Kapcsolódva: {formatDateTime(vpn.connectedAt)}
            </p>
          )}
          {vpn.metadata && (
            <p className="text-sm text-muted-foreground">
              Végpont:{' '}
              <span className="font-mono">{vpn.metadata.endpoint}</span> · Cím:{' '}
              <span className="font-mono">
                {vpn.metadata.addresses.join(', ')}
              </span>
            </p>
          )}
        </div>

        <div className="flex items-center space-x-2">
          <Switch
            id="vpn-enabled"
            checked={vpn.enabled}
            disabled={isUpdating || (!vpn.enabled && !vpn.hasConfig)}
            onCheckedChange={(checked) => void handleToggle(checked)}
          />
          <Label htmlFor="vpn-enabled">VPN a torrentforgalomhoz</Label>
        </div>

        <Separator />

        <div className="grid gap-2">
          <Label htmlFor="vpn-config">WireGuard-konfiguráció (.conf)</Label>
          <Textarea
            id="vpn-config"
            rows={8}
            className="font-mono"
            placeholder={
              vpn.hasConfig
                ? 'Mentett konfiguráció van; újat ide illeszthetsz be.'
                : '[Interface] ... [Peer] ...'
            }
            value={configText}
            onChange={(event) => setConfigText(event.target.value)}
          />
          <p className="text-sm text-muted-foreground">
            A konfiguráció titkosítva tárolódik, a privát kulcs soha nem jelenik
            meg újra. A teljes adatkötet megszerzése ellen ez nem véd.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline">
              <label>
                Fájl kiválasztása
                <input
                  type="file"
                  accept=".conf,text/plain"
                  className="hidden"
                  onChange={(event) => void handleFile(event)}
                />
              </label>
            </Button>
            <Button
              disabled={isUpdating || configText.trim() === ''}
              onClick={() => void handleSave()}
            >
              Konfiguráció mentése
            </Button>
            {vpn.hasConfig && !vpn.enabled && (
              <Button variant="destructive" onClick={() => void handleDelete()}>
                Konfiguráció törlése
              </Button>
            )}
          </div>
        </div>

        <Separator />

        <div className="grid gap-2">
          <div>
            <Button
              variant="outline"
              disabled={isTesting}
              onClick={() => void handleTest()}
            >
              Teszt
            </Button>
          </div>
          {testResult && (
            <ul className="grid gap-1 text-sm">
              {testResult.checks.map((check) => (
                <li key={check.name}>
                  {check.ok === null ? '⚪' : check.ok ? '🟢' : '🔴'}{' '}
                  {checkLabels[check.name] ?? check.name}
                  {check.code && (
                    <span className="font-mono text-muted-foreground">
                      {' '}
                      ({check.code})
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
