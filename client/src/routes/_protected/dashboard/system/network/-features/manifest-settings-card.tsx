import { useQuery } from '@tanstack/react-query'
import {
  GlobeIcon,
  HardDriveIcon,
  NetworkIcon,
  PlusIcon,
  SaveIcon,
  ShieldIcon,
  Trash2Icon,
} from 'lucide-react'
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
import { Label } from '@/shared/components/ui/label'
import { Separator } from '@/shared/components/ui/separator'
import { Switch } from '@/shared/components/ui/switch'
import { parseApiError } from '@/shared/lib/utils'
import {
  getSystemManifests,
  useSystemManifestsUpdate,
} from '@/shared/queries/system'

interface EndpointItem {
  id: string
  name: string
  baseUrl: string
  isEnabled: boolean
  urlSuffix?: string
  badge?: string
}

export function ManifestSettingsCard() {
  const { data: manifestsData, isLoading } = useQuery(getSystemManifests)
  const { mutateAsync: updateManifests, isPending } = useSystemManifestsUpdate()

  const [endpoints, setEndpoints] = useState<EndpointItem[]>([])

  useEffect(() => {
    if (manifestsData) {
      const data = manifestsData as any
      if (Array.isArray(data.endpoints) && data.endpoints.length > 0) {
        setEndpoints(
          data.endpoints.map((ep: any) => ({
            id: ep.id,
            name: ep.name,
            baseUrl: ep.baseUrl || ep.base_url || '',
            isEnabled: ep.isEnabled ?? ep.is_enabled ?? true,
            urlSuffix: ep.urlSuffix || ep.url_suffix,
            badge: ep.badge,
          })),
        )
      } else {
        const defaultList: EndpointItem[] = [
          {
            id: 'default',
            name: 'Nyilvános',
            baseUrl: '',
            isEnabled: true,
            urlSuffix: 'stremio',
          },
        ]
        if (data.lanUrl || data.lan_url) {
          defaultList.push({
            id: 'lan',
            name: 'Helyi hálózat (LAN)',
            baseUrl: data.lanUrl || data.lan_url,
            isEnabled: data.lanEnabled ?? data.lan_enabled ?? true,
            urlSuffix: 'stremio-lan',
          })
        }
        if (data.tailscaleUrl || data.tailscale_url) {
          defaultList.push({
            id: 'tailscale',
            name: 'Tailscale',
            baseUrl: data.tailscaleUrl || data.tailscale_url,
            isEnabled: data.tailscaleEnabled ?? data.tailscale_enabled ?? true,
            urlSuffix: 'stremio-tailscale',
          })
        }
        setEndpoints(defaultList)
      }
    }
  }, [manifestsData])

  const handleUpdate = (id: string, updates: Partial<EndpointItem>) => {
    setEndpoints((prev) =>
      prev.map((ep) => (ep.id === id ? { ...ep, ...updates } : ep)),
    )
  }

  const handleDelete = (id: string) => {
    setEndpoints((prev) => prev.filter((ep) => ep.id !== id))
  }

  const handleAddEndpoint = () => {
    const randomSlug = Math.random().toString(36).substring(2, 7)
    const newId = `custom-${randomSlug}`
    setEndpoints((prev) => [
      ...prev,
      {
        id: newId,
        name: 'Egyedi elérés',
        baseUrl: '',
        isEnabled: true,
        urlSuffix: `stremio-${newId}`,
      },
    ])
  }

  const handleSave = async () => {
    try {
      await updateManifests({
        endpoints: endpoints.map((ep) => ({
          id: ep.id,
          name: ep.name.trim() || ep.id,
          baseUrl: ep.baseUrl.trim(),
          isEnabled: ep.isEnabled,
          urlSuffix:
            ep.urlSuffix ||
            (ep.id === 'default' ? 'stremio' : `stremio-${ep.id}`),
          badge: ep.badge || ep.name.trim().slice(0, 12),
        })),
        lanEnabled: endpoints.find((e) => e.id === 'lan')?.isEnabled ?? false,
        lanUrl: endpoints.find((e) => e.id === 'lan')?.baseUrl || '',
        tailscaleEnabled:
          endpoints.find((e) => e.id === 'tailscale')?.isEnabled ?? false,
        tailscaleUrl:
          endpoints.find((e) => e.id === 'tailscale')?.baseUrl || '',
      })
      toast.success('A hálózati elérések sikeresen mentve!')
    } catch (error) {
      toast.error(parseApiError(error))
    }
  }

  const getEndpointIcon = (id: string) => {
    if (id === 'default') return <GlobeIcon className="size-4 text-primary" />
    if (id === 'lan') return <HardDriveIcon className="size-4 text-primary" />
    if (id === 'tailscale')
      return <ShieldIcon className="size-4 text-primary" />
    return <NetworkIcon className="size-4 text-primary" />
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <NetworkIcon className="size-5 text-primary" />
              Kliens elérések és manifestek
            </CardTitle>
            <CardDescription className="mt-1.5">
              Állíts be több hálózati elérést (Nyilvános, LAN, Tailscale vagy
              egyedi VPN). A bekapcsolt elérések a kezdőlapon egyetlen lenyíló
              listából közvetlenül választhatók ki.
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleAddEndpoint}
            className="gap-1.5 shrink-0 self-start sm:self-auto cursor-pointer"
          >
            <PlusIcon className="size-4" />
            Új elérés hozzáadása
          </Button>
        </div>
      </CardHeader>

      <Separator />

      <CardContent className="grid gap-4 pt-6">
        {endpoints.map((ep) => (
          <div
            key={ep.id}
            className="grid gap-3 p-4 rounded-xl border border-border/60 bg-muted/20"
          >
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-2">
                {getEndpointIcon(ep.id)}
                <Input
                  value={ep.name}
                  onChange={(e) =>
                    handleUpdate(ep.id, { name: e.target.value })
                  }
                  placeholder="Elnevezés (pl. Nyilvános, LAN, Tailscale)"
                  className="font-semibold text-sm h-8 w-48 sm:w-60"
                  disabled={isLoading}
                />
                <Badge variant={ep.isEnabled ? 'default' : 'secondary'}>
                  {ep.isEnabled ? 'Aktív' : 'Inaktív'}
                </Badge>
              </div>

              <div className="flex items-center gap-3">
                <Switch
                  checked={ep.isEnabled}
                  onCheckedChange={(checked) =>
                    handleUpdate(ep.id, { isEnabled: checked })
                  }
                  disabled={isLoading}
                />
                {ep.id !== 'default' && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => handleDelete(ep.id)}
                    className="text-destructive hover:text-destructive hover:bg-destructive/10 rounded-full"
                    title="Elérés törlése"
                  >
                    <Trash2Icon className="size-4" />
                  </Button>
                )}
              </div>
            </div>

            <div className="grid gap-1.5 sm:pl-6">
              <Label className="text-xs text-muted-foreground">
                Elérés címe (Base URL):
              </Label>
              <Input
                value={ep.baseUrl}
                onChange={(e) =>
                  handleUpdate(ep.id, { baseUrl: e.target.value })
                }
                placeholder="pl. http://192.168.1.5:7070 vagy http://100.66.1.85:7070"
                className="font-mono text-xs max-w-lg"
                disabled={isLoading}
              />
              <p className="text-[11px] text-muted-foreground">
                Végpont:{' '}
                <code className="text-foreground">
                  /api/[kulcs]/
                  {ep.id === 'default' ? 'stremio' : `stremio-${ep.id}`}
                  /manifest.json
                </code>
              </p>
            </div>
          </div>
        ))}
      </CardContent>

      <CardFooter className="justify-end gap-3 pt-2">
        <Button
          onClick={handleSave}
          disabled={isLoading || isPending}
          className="gap-2 cursor-pointer"
        >
          <SaveIcon className="size-4" />
          Hálózati elérések mentése
        </Button>
      </CardFooter>
    </Card>
  )
}
