import { useQuery } from '@tanstack/react-query'
import { HardDriveIcon, NetworkIcon, SaveIcon, ShieldIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'

import { Badge } from '@/shared/components/ui/badge'
import { Button } from '@/shared/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import { Input } from '@/shared/components/ui/input'
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from '@/shared/components/ui/item'
import { Label } from '@/shared/components/ui/label'
import { Separator } from '@/shared/components/ui/separator'
import { Switch } from '@/shared/components/ui/switch'
import { parseApiError } from '@/shared/lib/utils'
import {
  getSystemManifests,
  useSystemManifestsUpdate,
} from '@/shared/queries/system'

export function ManifestSettingsCard() {
  const { data: manifestsData, isLoading } = useQuery(getSystemManifests)
  const { mutateAsync: updateManifests, isPending } = useSystemManifestsUpdate()

  const [lanEnabled, setLanEnabled] = useState(true)
  const [lanUrl, setLanUrl] = useState('')
  const [tailscaleEnabled, setTailscaleEnabled] = useState(false)
  const [tailscaleUrl, setTailscaleUrl] = useState('')

  useEffect(() => {
    if (manifestsData) {
      setLanEnabled(manifestsData.lanEnabled)
      setLanUrl(manifestsData.lanUrl || '')
      setTailscaleEnabled(manifestsData.tailscaleEnabled)
      setTailscaleUrl(manifestsData.tailscaleUrl || '')
    }
  }, [manifestsData])

  const handleSave = async () => {
    try {
      await updateManifests({
        lanEnabled,
        lanUrl: lanUrl.trim(),
        tailscaleEnabled,
        tailscaleUrl: tailscaleUrl.trim(),
      })
      toast.success('A manifest beállítások sikeresen mentve!')
    } catch (error) {
      toast.error(parseApiError(error))
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <NetworkIcon className="size-5 text-primary" />
              Több Kliens Manifest (LAN, Tailscale)
            </CardTitle>
            <CardDescription className="mt-1.5">
              Állíts be különálló Stremio és Nuvio manifest végpontokat a helyi
              hálózathoz (LAN) és VPN-hez (Tailscale), hogy otthon közvetlenül,
              Cloudflare sávszélesség-korlátok nélkül streamelhess.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <Separator />

      <CardContent className="grid gap-6 pt-6">
        {/* LAN beállítások */}
        <div className="grid gap-3">
          <Item variant="default" className="p-0">
            <ItemMedia variant="icon">
              <HardDriveIcon />
            </ItemMedia>
            <ItemContent>
              <div className="flex items-center gap-2">
                <ItemTitle>Helyi hálózat (LAN) manifest</ItemTitle>
                <Badge variant={lanEnabled ? 'default' : 'secondary'}>
                  {lanEnabled ? 'Aktív' : 'Inaktív'}
                </Badge>
              </div>
              <ItemDescription>
                A helyi hálózaton (Wi-Fi, Ethernet) lévő TV-khez és eszközökhöz.
                A videó stream közvetlenül a belső IP-ről indul el.
              </ItemDescription>
            </ItemContent>
            <ItemActions>
              <Switch
                checked={lanEnabled}
                onCheckedChange={setLanEnabled}
                disabled={isLoading}
              />
            </ItemActions>
          </Item>

          <div className="grid gap-2 pl-12">
            <Label htmlFor="lan-url-input" className="text-xs text-muted-foreground">
              Belső hálózati elérés (Base URL)
            </Label>
            <Input
              id="lan-url-input"
              value={lanUrl}
              onChange={(e) => setLanUrl(e.target.value)}
              placeholder="pl. http://192.168.1.5:7070"
              disabled={!lanEnabled || isLoading}
              className="max-w-md font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">
              Végpont elérési útja: <code className="text-foreground">/api/[api_key]/stremio-lan/manifest.json</code>
            </p>
          </div>
        </div>

        <Separator />

        {/* Tailscale beállítások */}
        <div className="grid gap-3">
          <Item variant="default" className="p-0">
            <ItemMedia variant="icon">
              <ShieldIcon />
            </ItemMedia>
            <ItemContent>
              <div className="flex items-center gap-2">
                <ItemTitle>Tailscale (VPN) manifest</ItemTitle>
                <Badge variant={tailscaleEnabled ? 'default' : 'secondary'}>
                  {tailscaleEnabled ? 'Aktív' : 'Inaktív'}
                </Badge>
              </div>
              <ItemDescription>
                Tailscale WireGuard privát hálózaton keresztüli közvetlen elérés,
                mobil és távoli eszközökhöz.
              </ItemDescription>
            </ItemContent>
            <ItemActions>
              <Switch
                checked={tailscaleEnabled}
                onCheckedChange={setTailscaleEnabled}
                disabled={isLoading}
              />
            </ItemActions>
          </Item>

          <div className="grid gap-2 pl-12">
            <Label htmlFor="ts-url-input" className="text-xs text-muted-foreground">
              Tailscale elérés (Base URL)
            </Label>
            <Input
              id="ts-url-input"
              value={tailscaleUrl}
              onChange={(e) => setTailscaleUrl(e.target.value)}
              placeholder="pl. http://100.66.1.85:7070"
              disabled={!tailscaleEnabled || isLoading}
              className="max-w-md font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">
              Végpont elérési útja: <code className="text-foreground">/api/[api_key]/stremio-tailscale/manifest.json</code>
            </p>
          </div>
        </div>
      </CardContent>

      <CardFooter className="justify-end gap-3 pt-2">
        <Button
          onClick={handleSave}
          disabled={isLoading || isPending}
          className="gap-2"
        >
          <SaveIcon className="size-4" />
          Manifest beállítások mentése
        </Button>
      </CardFooter>
    </Card>
  )
}
