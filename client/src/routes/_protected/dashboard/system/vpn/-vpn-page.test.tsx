import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useDialogsStore } from '@/routes/-features/dialogs/dialogs-store'
import type { VpnResponse } from '@/shared/lib/source/vpn-client'
import { getVpn } from '@/shared/queries/vpn'

import { VpnRoute } from './index'

const { vpnUpdate } = vi.hoisted(() => ({ vpnUpdate: vi.fn() }))

vi.mock('@/shared/lib/source/vpn-client', () => ({
  vpnGet: vi.fn(),
  vpnUpdate,
  vpnDeleteConfig: vi.fn(),
  vpnTest: vi.fn(),
}))

function vpn(overrides: Partial<VpnResponse>): VpnResponse {
  return {
    state: 'connected',
    errorCode: null,
    connectedAt: null,
    enabled: true,
    hasConfig: true,
    metadata: null,
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

afterEach(() => {
  cleanup()
  vpnUpdate.mockReset()
  useDialogsStore.setState({ dialogs: [] })
})

describe('VPN page', () => {
  it('turns the VPN off only after an explicit confirmation', async () => {
    vpnUpdate.mockResolvedValue(vpn({ enabled: false, state: 'disabled' }))
    renderPage(vpn({}))

    await act(async () => {
      fireEvent.click(screen.getByRole('switch'))
      await Promise.resolve()
    })
    expect(vpnUpdate).not.toHaveBeenCalled()

    const dialog = useDialogsStore.getState().dialogs.at(0)
    expect(dialog?.type).toBe('CONFIRM')
    await act(async () => {
      if (dialog?.type === 'CONFIRM') {
        await dialog.handleConfirm(dialog.id)
      }
    })
    expect(vpnUpdate).toHaveBeenCalledWith({
      enabled: false,
      confirmUnprotected: true,
    })
  })

  it('cannot be switched on before a configuration is saved', () => {
    renderPage(vpn({ enabled: false, hasConfig: false, state: 'disabled' }))
    expect(screen.getByRole('switch')).toHaveProperty('disabled', true)
  })
})
