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
}

export interface VpnResponse extends VpnStatusResponse {
  enabled: boolean
  hasConfig: boolean
  metadata: VpnMetadata | null
}

export interface VpnUpdateRequest {
  config?: string
  enabled?: boolean
  confirmUnprotected?: boolean
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

export const vpnDeleteConfig = () =>
  sourceClientInstance<VpnResponse>({
    url: '/api/vpn/config',
    method: 'DELETE',
  })

export const vpnTest = () =>
  sourceClientInstance<VpnTestResponse>({
    url: '/api/vpn/test',
    method: 'POST',
  })
