import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { assertExists } from '../lib/utils'
import { getSystemStatus } from '../queries/system'

type UseIntegrationDomainProps = {
  apiKey: string
}

export interface IntegrationManifestItem {
  id: string
  name: string
  badge: string
  baseUrl: string
  urlSuffix: string
  appEndpoint: string
  webEndpoint: string
  urlEndpoint: string
  nuvioUrl: string
}

export function useIntegrationDomain(props: UseIntegrationDomainProps) {
  const { apiKey } = props

  const { data: systemStatus } = useQuery(getSystemStatus)
  assertExists(systemStatus)

  const manifests = useMemo<IntegrationManifestItem[]>(() => {
    const rawList =
      systemStatus.manifestEndpoints ||
      (systemStatus as any).manifest_endpoints
    const rawEndpoints =
      rawList && rawList.length > 0
        ? rawList
        : [
            {
              id: 'default',
              name: 'Nyilvános',
              badge: 'Nyilvános',
              urlSuffix: 'stremio',
              baseUrl: systemStatus.appUrl || (systemStatus as any).app_url || '',
              isEnabled: true,
            },
          ]

    return rawEndpoints.map((ep: any) => {
      const urlSuffix =
        ep.urlSuffix ||
        ep.url_suffix ||
        (ep.id === 'default' ? 'stremio' : `stremio-${ep.id}`)
      const rawBase = ep.baseUrl || ep.base_url || ''
      const cleanBase = (rawBase || systemStatus.appUrl).replace(/\/+$/, '')
      let endpointHost = cleanBase
      try {
        endpointHost = new URL(cleanBase).host
      } catch {
        endpointHost = cleanBase.replace(/^https?:\/\//, '')
      }
      const manifestPath = `/api/${apiKey}/${urlSuffix}/manifest.json`
      const fullUrl = `${cleanBase}${manifestPath}`

      return {
        id: ep.id,
        name: ep.name || (ep.id === 'default' ? 'Nyilvános' : ep.id === 'lan' ? 'Helyi hálózat (LAN)' : ep.id === 'tailscale' ? 'Tailscale' : ep.id),
        badge:
          ep.badge ||
          (ep.id === 'lan'
            ? 'LAN'
            : ep.id === 'tailscale'
              ? 'Tailscale'
              : 'Nyilvános'),
        baseUrl: cleanBase,
        urlSuffix,
        appEndpoint: `stremio://${endpointHost}${manifestPath}`,
        urlEndpoint: fullUrl,
        webEndpoint: `https://web.stremio.com/#/addons?addon=${encodeURIComponent(fullUrl)}`,
        nuvioUrl: fullUrl,
      }
    })
  }, [systemStatus, apiKey])

  const [selectedId, setSelectedId] = useState<string>(
    () => manifests[0]?.id || 'default',
  )

  const activeManifest = useMemo(() => {
    return manifests.find((m) => m.id === selectedId) || manifests[0]
  }, [manifests, selectedId])

  return {
    manifests,
    selectedId: activeManifest.id,
    setSelectedId,
    activeManifest,
    stremio: {
      appEndpoint: activeManifest.appEndpoint,
      webEndpoint: activeManifest.webEndpoint,
      urlEndpoint: activeManifest.urlEndpoint,
    },
    nuvioUrl: activeManifest.nuvioUrl,
    kodiUrl: activeManifest.baseUrl,
  }
}
