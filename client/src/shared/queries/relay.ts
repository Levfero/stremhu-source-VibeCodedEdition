import {
  queryOptions,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'

import type {
  RelaySettingsResponse,
  RelaySettingsUpdateRequest,
} from '../lib/source/source-client'
import {
  relayGetSettings,
  relayUpdateSettings,
} from '../lib/source/source-client'

// A vpnManaged mező a generált kliensben még nem szerepel (VPN-terv 7. fejezet).
export type RelaySettingsWithVpn = RelaySettingsResponse & {
  vpnManaged?: boolean
}

export const getRelaySettings = queryOptions({
  queryKey: ['relay', 'settings'],
  queryFn: async (): Promise<RelaySettingsWithVpn> => {
    const settings = await relayGetSettings()
    return settings
  },
})

export function useUpdateRelaySetting() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (payload: RelaySettingsUpdateRequest) => {
      const setting = await relayUpdateSettings(payload)
      return setting
    },
    onSuccess: async () => {
      await queryClient.fetchQuery({ ...getRelaySettings, staleTime: 0 })
    },
  })
}
