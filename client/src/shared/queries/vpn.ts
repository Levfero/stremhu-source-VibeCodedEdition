import {
  queryOptions,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'

import type { VpnUpdateRequest } from '../lib/source/vpn-client'
import {
  vpnDeleteConfig,
  vpnGet,
  vpnSpeedTest,
  vpnTest,
  vpnUpdate,
} from '../lib/source/vpn-client'
import { getRelaySettings } from './relay'

export const getVpn = queryOptions({
  queryKey: ['vpn'],
  refetchIntervalInBackground: false,
  // Átmeneti állapotban gyakrabban frissül, hogy a felület kövesse a kapcsolódást.
  refetchInterval: (query) => {
    const state = query.state.data?.state
    return state === 'starting' || state === 'reconnecting' ? 2000 : 10000
  },
  queryFn: async () => {
    const response = await vpnGet()
    return response
  },
})

export function useUpdateVpn() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (payload: VpnUpdateRequest) => {
      const response = await vpnUpdate(payload)
      return response
    },
    onSuccess: async (response) => {
      queryClient.setQueryData(getVpn.queryKey, response)
      // VPN módban a relay port-, UPnP- és uTP-mezője zárolt.
      await queryClient.invalidateQueries({
        queryKey: getRelaySettings.queryKey,
      })
    },
  })
}

export function useDeleteVpnConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (configId: string) => {
      const response = await vpnDeleteConfig(configId)
      return response
    },
    onSuccess: async (response) => {
      queryClient.setQueryData(getVpn.queryKey, response)
      await queryClient.invalidateQueries({
        queryKey: getRelaySettings.queryKey,
      })
    },
  })
}

export function useSpeedTestVpn() {
  return useMutation({
    mutationFn: async () => {
      const response = await vpnSpeedTest()
      return response
    },
  })
}

export function useTestVpn() {
  return useMutation({
    mutationFn: async () => {
      const response = await vpnTest()
      return response
    },
  })
}
