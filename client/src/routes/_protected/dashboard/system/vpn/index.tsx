import { useSuspenseQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { ArrowDownIcon, ArrowUpIcon, InfoIcon, Trash2Icon } from 'lucide-react'
import type { ChangeEvent } from 'react'
import { useState } from 'react'
import { toast } from 'sonner'

import { useConfirmDialog } from '@/features/confirm/use-confirm-dialog'
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from '@/shared/components/ui/alert'
import { Badge } from '@/shared/components/ui/badge'
import { Button } from '@/shared/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import { Input } from '@/shared/components/ui/input'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemTitle,
} from '@/shared/components/ui/item'
import { Label } from '@/shared/components/ui/label'
import { Separator } from '@/shared/components/ui/separator'
import { Switch } from '@/shared/components/ui/switch'
import { Textarea } from '@/shared/components/ui/textarea'
import type {
  VpnConfigEntry,
  VpnSpeedTestResponse,
  VpnState,
  VpnTestResponse,
} from '@/shared/lib/source/vpn-client'
import { formatDateTime, parseApiError } from '@/shared/lib/utils'
import {
  getVpn,
  useDeleteVpnConfig,
  useSpeedTestVpn,
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

const numberFormat = new Intl.NumberFormat('hu-HU', {
  maximumFractionDigits: 1,
})

// Tájékoztató sávszélesség-igény (egy film, a többi torrent nélkül).
function speedHint(mbps: number): string {
  if (mbps >= 80) return '🟢 4K remuxhoz is elég.'
  if (mbps >= 25) return '🟢 4K WEB-DL-hez elég; 4K remuxhoz kevés lehet.'
  if (mbps >= 10) return '🟡 1080p-hez elég; 4K-hoz kevés lehet.'
  return '🔴 Lassú: a lejátszás akadhat. Próbálj másik szervert.'
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
  const { mutateAsync: speedTestVpn, isPending: isSpeedTesting } =
    useSpeedTestVpn()
  const confirmDialog = useConfirmDialog()
  const [configText, setConfigText] = useState('')
  const [configName, setConfigName] = useState('')
  const [testResult, setTestResult] = useState<VpnTestResponse | null>(null)
  const [speedResult, setSpeedResult] = useState<VpnSpeedTestResponse | null>(
    null,
  )

  const active =
    vpn.configs.find((config) => config.id === vpn.activeConfigId) ?? null

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
      if (configName.trim() === '') {
        setConfigName(file.name.replace(/\.conf$/i, ''))
      }
    }
  }

  const handleSave = async () => {
    await run(async () => {
      await updateVpn({
        config: configText,
        name: configName.trim() || undefined,
      })
      setConfigText('')
      setConfigName('')
      toast.success('A WireGuard-konfiguráció elmentve.')
    })
  }

  const handleMove = async (index: number, delta: number) => {
    const order = vpn.configs.map((config) => config.id)
    const [moved] = order.splice(index, 1)
    order.splice(index + delta, 0, moved)
    await run(() => updateVpn({ order }))
  }

  const handleActivate = async (config: VpnConfigEntry) => {
    await run(() => updateVpn({ activeConfigId: config.id }))
  }

  const handleDelete = async (config: VpnConfigEntry) => {
    const switches = vpn.enabled && config.id === vpn.activeConfigId
    await confirmDialog.confirm({
      title: `Törlöd a(z) „${config.name}” konfigurációt?`,
      description: switches
        ? 'A mentett WireGuard-konfiguráció véglegesen törlődik, és a StremHU a lista következő konfigurációjára vált.'
        : 'A mentett WireGuard-konfiguráció véglegesen törlődik.',
      confirmText: 'Törlés',
      onConfirm: async () => {
        await run(() => deleteConfig(config.id))
      },
    })
  }

  const handleSpeedTest = async () => {
    setSpeedResult(null)
    await run(async () => {
      setSpeedResult(await speedTestVpn())
    })
  }

  const speedConfig = speedResult
    ? vpn.configs.find((config) => config.id === speedResult.configId)
    : undefined

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
          {active && (
            <p className="text-sm text-muted-foreground">
              Aktív konfiguráció:{' '}
              <span className="font-medium text-foreground">{active.name}</span>{' '}
              · Végpont:{' '}
              <span className="font-mono">{active.metadata.endpoint}</span> ·
              Cím:{' '}
              <span className="font-mono">
                {active.metadata.addresses.join(', ')}
              </span>
            </p>
          )}
          {vpn.failoverAt && (
            <p className="text-sm text-muted-foreground">
              Utolsó automatikus átállás: {formatDateTime(vpn.failoverAt)}
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

        <div className="grid gap-1">
          <div className="flex items-center space-x-2">
            <Switch
              id="vpn-failover"
              checked={vpn.autoFailover}
              disabled={isUpdating || vpn.configs.length < 2}
              onCheckedChange={(checked) =>
                void run(() => updateVpn({ autoFailover: checked }))
              }
            />
            <Label htmlFor="vpn-failover">
              Automatikus átállás a következő konfigurációra
            </Label>
          </div>
          <p className="text-sm text-muted-foreground">
            Ha az aktív szerver kb. egy percig nem válaszol, a StremHU a lista
            következő konfigurációjára vált, és azon marad. Átálláskor a
            trackerek új IP-címet látnak. Legalább két mentett konfiguráció kell
            hozzá.
          </p>
        </div>

        <Separator />

        <div className="grid gap-2">
          <p className="font-medium">Mentett konfigurációk</p>
          {vpn.configs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Még nincs mentett konfiguráció.
            </p>
          ) : (
            <div className="grid gap-2">
              {vpn.configs.map((config, index) => (
                <Item key={config.id} variant="outline" size="sm">
                  <ItemContent>
                    <ItemTitle>
                      {index + 1}. {config.name}
                      {config.id === vpn.activeConfigId && (
                        <Badge variant="secondary">Aktív</Badge>
                      )}
                    </ItemTitle>
                    <ItemDescription className="font-mono">
                      {config.metadata.endpoint}
                    </ItemDescription>
                  </ItemContent>
                  <ItemActions>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`${config.name} feljebb`}
                      disabled={isUpdating || index === 0}
                      onClick={() => void handleMove(index, -1)}
                    >
                      <ArrowUpIcon />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`${config.name} lejjebb`}
                      disabled={isUpdating || index === vpn.configs.length - 1}
                      onClick={() => void handleMove(index, 1)}
                    >
                      <ArrowDownIcon />
                    </Button>
                    {config.id !== vpn.activeConfigId && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isUpdating}
                        onClick={() => void handleActivate(config)}
                      >
                        Használat
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`${config.name} törlése`}
                      disabled={vpn.enabled && vpn.configs.length === 1}
                      onClick={() => void handleDelete(config)}
                    >
                      <Trash2Icon />
                    </Button>
                  </ItemActions>
                </Item>
              ))}
            </div>
          )}
          <p className="text-sm text-muted-foreground">
            A sorrend az átállás sorrendje. A konfigurációk titkosítva
            tárolódnak, a privát kulcs soha nem jelenik meg újra. A teljes
            adatkötet megszerzése ellen ez nem véd.
          </p>
        </div>

        <Separator />

        <div className="grid gap-2">
          <Label htmlFor="vpn-config">Új WireGuard-konfiguráció (.conf)</Label>
          <Textarea
            id="vpn-config"
            rows={8}
            className="font-mono"
            placeholder="[Interface] ... [Peer] ..."
            value={configText}
            onChange={(event) => setConfigText(event.target.value)}
          />
          <Input
            id="vpn-config-name"
            aria-label="A konfiguráció neve"
            placeholder="Név (nem kötelező), pl. Budapest #35"
            maxLength={64}
            value={configName}
            onChange={(event) => setConfigName(event.target.value)}
          />
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
          </div>
        </div>

        <Separator />

        <div className="grid gap-2">
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={isTesting}
              onClick={() => void handleTest()}
            >
              Teszt
            </Button>
            <Button
              variant="outline"
              disabled={isSpeedTesting || vpn.state !== 'connected'}
              onClick={() => void handleSpeedTest()}
            >
              {isSpeedTesting ? 'Mérés folyamatban...' : 'Sebességteszt'}
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            A sebességteszt kapcsolódott VPN mellett, kérésre fut: legfeljebb
            kb. 25 MB-ot tölt le a VPN-en át (kb. 15 s). Futó torrentek mellett
            csak a maradék sávszélességet méri.
          </p>
          {speedResult && (
            <ul className="grid gap-1 text-sm">
              <li>
                ⬇️ Letöltés:{' '}
                <span className="font-medium">
                  {numberFormat.format(speedResult.downloadMbps)} Mbit/s
                </span>{' '}
                ({numberFormat.format(speedResult.downloadedBytes / 1_000_000)}{' '}
                MB, {numberFormat.format(speedResult.durationSeconds)} s)
                {speedConfig && <> · {speedConfig.name}</>}
              </li>
              <li>{speedHint(speedResult.downloadMbps)}</li>
              {speedResult.latencyMs !== null && (
                <li>
                  Késleltetés: {numberFormat.format(speedResult.latencyMs)} ms
                </li>
              )}
              {speedResult.exitIp && (
                <li>
                  Kilépő IP:{' '}
                  <span className="font-mono">{speedResult.exitIp}</span>
                  {speedResult.exitCountry && <> ({speedResult.exitCountry})</>}
                </li>
              )}
              <li>
                {speedResult.exitDiffersFromDirect === true &&
                  '🟢 A kilépő IP eltér a saját IP-címedtől.'}
                {speedResult.exitDiffersFromDirect === false &&
                  '🔴 A kilépő IP megegyezik a saját IP-címeddel!'}
                {speedResult.exitDiffersFromDirect === null &&
                  '⚪ A saját IP-címeddel nem volt összevethető.'}
              </li>
            </ul>
          )}
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
