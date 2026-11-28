import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useDialogsStore } from '@/routes/-features/dialogs/dialogs-store'
import type {
  VpnConfigEntry,
  VpnResponse,
} from '@/shared/lib/source/vpn-client'
import { getVpn } from '@/shared/queries/vpn'

import { VpnRoute } from './index'

const { vpnUpdate, vpnDeleteConfig } = vi.hoisted(() => ({
  vpnUpdate: vi.fn(),
  vpnDeleteConfig: vi.fn(),
}))

vi.mock('@/shared/lib/source/vpn-client', () => ({
  vpnGet: vi.fn(),
  vpnUpdate,
  vpnDeleteConfig,
  vpnTest: vi.fn(),
}))

function entry(id: string, name: string): VpnConfigEntry {
  return {
    id,
    name,
    metadata: {
      addresses: ['10.2.0.2/32'],
      dns: ['10.2.0.1'],
      endpoint: '192.0.2.10:51820',
      peerPublicKey: 'peer',
      clientPublicKeyFingerprint: 'fingerprint',
    },
  }
}

function vpn(overrides: Partial<VpnResponse>): VpnResponse {
  return {
    state: 'connected',
    errorCode: null,
    connectedAt: null,
    activeConfigId: 'a',
    failoverAt: null,
    enabled: true,
    hasConfig: true,
    metadata: null,
    configs: [entry('a', 'Budapest')],
    autoFailover: true,
    ...overrides,
  }
}

function renderPage(data: VpnResponse) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  client.setQueryData(getVpn.queryKey, data)
  render(
    <QueryClientProvider client={client}>
      <VpnRoute />
    </QueryClientProvider>,
  )
}

async function confirmOpenDialog() {
  const dialog = useDialogsStore.getState().dialogs.at(0)
  expect(dialog?.type).toBe('CONFIRM')
  await act(async () => {
    if (dialog?.type === 'CONFIRM') {
      await dialog.handleConfirm(dialog.id)
    }
  })
}

afterEach(() => {
  cleanup()
  vpnUpdate.mockReset()
  vpnDeleteConfig.mockReset()
  useDialogsStore.setState({ dialogs: [] })
})

describe('VPN page', () => {
  it('turns the VPN off only after an explicit confirmation', async () => {
    vpnUpdate.mockResolvedValue(vpn({ enabled: false, state: 'disabled' }))
    renderPage(vpn({}))

    await act(async () => {
      fireEvent.click(
        screen.getByRole('switch', { name: 'VPN a torrentforgalomhoz' }),
      )
      await Promise.resolve()
    })
    expect(vpnUpdate).not.toHaveBeenCalled()

    await confirmOpenDialog()
    expect(vpnUpdate).toHaveBeenCalledWith({
      enabled: false,
      confirmUnprotected: true,
    })
  })

  it('cannot be switched on before a configuration is saved', () => {
    renderPage(
      vpn({
        enabled: false,
        hasConfig: false,
        state: 'disabled',
        configs: [],
        activeConfigId: null,
      }),
    )
    expect(
      screen.getByRole('switch', { name: 'VPN a torrentforgalomhoz' }),
    ).toHaveProperty('disabled', true)
  })

  it('offers failover only with a second configuration', () => {
    renderPage(vpn({}))
    expect(
      screen.getByRole('switch', {
        name: 'Automatikus átállás a következő konfigurációra',
      }),
    ).toHaveProperty('disabled', true)
    cleanup()

    renderPage(vpn({ configs: [entry('a', 'Budapest'), entry('b', 'Bécs')] }))
    expect(
      screen.getByRole('switch', {
        name: 'Automatikus átállás a következő konfigurációra',
      }),
    ).toHaveProperty('disabled', false)
  })

  it('reorders, switches and deletes saved configurations', async () => {
    const two = vpn({ configs: [entry('a', 'Budapest'), entry('b', 'Bécs')] })
    vpnUpdate.mockResolvedValue(two)
    vpnDeleteConfig.mockResolvedValue(two)
    renderPage(two)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Budapest lejjebb' }))
      await Promise.resolve()
    })
    expect(vpnUpdate).toHaveBeenCalledWith({ order: ['b', 'a'] })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Használat' }))
      await Promise.resolve()
    })
    expect(vpnUpdate).toHaveBeenCalledWith({ activeConfigId: 'b' })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Bécs törlése' }))
      await Promise.resolve()
    })
    expect(vpnDeleteConfig).not.toHaveBeenCalled()
    await confirmOpenDialog()
    expect(vpnDeleteConfig).toHaveBeenCalledWith('b')
  })

  it('keeps the last configuration while the VPN is on', () => {
    renderPage(vpn({}))
    expect(
      screen.getByRole('button', { name: 'Budapest törlése' }),
    ).toHaveProperty('disabled', true)
  })
})
