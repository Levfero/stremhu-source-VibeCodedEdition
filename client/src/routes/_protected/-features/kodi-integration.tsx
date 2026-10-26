import { CopyIcon, LinkIcon } from 'lucide-react'

import { Badge } from '@/shared/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import { Field } from '@/shared/components/ui/field'
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@/shared/components/ui/input-group'
import { useCopy } from '@/shared/hooks/use-copy'
import type { IntegrationManifestItem } from '@/shared/hooks/use-integration-domain'

interface KodiIntegrationProps {
  activeManifest?: IntegrationManifestItem
  kodiUrl?: string
}

export function KodiIntegration({
  activeManifest,
  kodiUrl,
}: KodiIntegrationProps) {
  const { handleCopy } = useCopy()

  const url = kodiUrl || ''

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Kodi</CardTitle>
          {activeManifest && (
            <Badge variant="outline" className="text-xs">
              {activeManifest.name}
            </Badge>
          )}
        </div>
        <CardDescription>
          <a className="link-primary" href="https://kodi.tv/" target="_blank">
            https://kodi.tv
          </a>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <p className="text-sm text-muted-foreground">
          StremHU addon bejelentkezés folyamatánál a következő URL-t kell
          megadni.
        </p>
        <Field className="max-w-sm">
          <InputGroup>
            <InputGroupInput value={url} readOnly />
            <InputGroupAddon align="inline-start">
              <LinkIcon />
            </InputGroupAddon>
            <InputGroupAddon align="inline-end">
              <InputGroupButton onClick={() => handleCopy(url)}>
                <CopyIcon />
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </Field>
      </CardContent>
    </Card>
  )
}
