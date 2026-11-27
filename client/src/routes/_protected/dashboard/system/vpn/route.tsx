import { Outlet, createFileRoute } from '@tanstack/react-router'

import { getVpn } from '@/shared/queries/vpn'

export const DASHBOARD_SYSTEM_VPN_NAME = 'VPN & Adatvédelem'

const RouteComponent = () => <Outlet />

export const Route = createFileRoute('/_protected/dashboard/system/vpn')({
  component: RouteComponent,
  beforeLoad: async ({ context }) => {
    await context.queryClient.ensureQueryData(getVpn)
  },
  loader: () => {
    return { breadcrumb: DASHBOARD_SYSTEM_VPN_NAME }
  },
})
