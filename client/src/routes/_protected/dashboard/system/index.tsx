import { createFileRoute } from '@tanstack/react-router'

import { SubtitleSettingsCard } from '@/features/subtitle-settings/subtitle-settings'
import { Separator } from '@/shared/components/ui/separator'

import { Indexers } from './-features/indexers'
import { KeepSeeding } from './-features/keep-seeding'
import { NetworkAccessInfo } from './-features/network-access-info'
import { Restart } from './-features/restart'
import { TorrentFilesCache } from './-features/torrent-files-cache'

export const Route = createFileRoute('/_protected/dashboard/system/')({
  component: SystemRoute,
})

function SystemRoute() {
  return (
    <div className="grid gap-8">
      <Indexers />
      <Separator />
      <div className="columns-1 md:columns-2 gap-4">
        <div className="break-inside-avoid mb-4">
          <NetworkAccessInfo />
        </div>
        <div className="break-inside-avoid mb-4">
          <TorrentFilesCache />
        </div>
        <div className="break-inside-avoid mb-4">
          <KeepSeeding />
        </div>
        <div className="break-inside-avoid mb-4">
          <SubtitleSettingsCard />
        </div>
        <div className="break-inside-avoid mb-4">
          <Restart />
        </div>
      </div>
    </div>
  )
}
