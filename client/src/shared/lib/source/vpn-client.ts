// A VPN admin-végpontjai (VPN-terv Rev. 5, 6.5). A generált source-client.ts jelenleg
// el van csúszva a szerverhez képest, ezért ezek a hívások kézzel írtak; a kliens
// újragenerálásakor ide kerülhetnek az orval-típusok.
import { sourceClientInstance } from './source-client-instance'

export type VpnState =
  'disabled' | 'starting' | 'connected' | 'reconnecting' | 'error'

export interface VpnMetadata {
  addresses: string[]
  dns: string[]
  endpoint: string
  peerPublicKey: string
  clientPublicKeyFingerprint: string
}

export interface VpnStatusResponse {
  state: VpnState
  errorCode: string | null
  connectedAt: string | null
  activeConfigId: string | null
  failoverAt: string | null
}

export interface VpnConfigEntry {
  id: string
  name: string
  metadata: VpnMetadata
}

export interface VpnResponse extends VpnStatusResponse {
  enabled: boolean
  hasConfig: boolean
  metadata: VpnMetadata | null
  configs: VpnConfigEntry[]
  autoFailover: boolean
}

export interface VpnUpdateRequest {
  config?: string
  name?: string
  enabled?: boolean
  confirmUnprotected?: boolean
  autoFailover?: boolean
  activeConfigId?: string
  order?: string[]
}

export interface VpnCheck {
  name: string
  ok: boolean | null
  code: string | null
}

export interface VpnTestResponse {
  state: VpnState
  checks: VpnCheck[]
}

export const vpnGet = () =>
  sourceClientInstance<VpnResponse>({ url: '/api/vpn', method: 'GET' })

export const vpnUpdate = (payload: VpnUpdateRequest) =>
  sourceClientInstance<VpnResponse>({
    url: '/api/vpn',
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    data: payload,
  })

export const vpnDeleteConfig = (configId: string) =>
  sourceClientInstance<VpnResponse>({
    url: `/api/vpn/configs/${encodeURIComponent(configId)}`,
    method: 'DELETE',
  })

export const vpnTest = () =>
  sourceClientInstance<VpnTestResponse>({
    url: '/api/vpn/test',
    method: 'POST',
  })
