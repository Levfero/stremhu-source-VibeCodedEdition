import { useForm } from '@tanstack/react-form'
import { useSuspenseQuery } from '@tanstack/react-query'
import { isEmpty } from 'lodash'
import { toast } from 'sonner'
import * as z from 'zod'

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/shared/components/ui/card'
import { Field, FieldError, FieldLabel } from '@/shared/components/ui/field'
import { InputGroup, InputGroupInput } from '@/shared/components/ui/input-group'
import { Label } from '@/shared/components/ui/label'
import { Switch } from '@/shared/components/ui/switch'
import { parseApiError } from '@/shared/lib/utils'
import { getRelaySettings, useUpdateRelaySetting } from '@/shared/queries/relay'

const schema = z.object({
  port: z.coerce.number<string>().min(1),
  enableUpnpAndNatpmp: z.boolean(),
})

export function Port() {
  const { data: relaySettings } = useSuspenseQuery(getRelaySettings)
  const vpnManaged = relaySettings.vpnManaged === true

  const { mutateAsync: updateSetting } = useUpdateRelaySetting()

  const form = useForm({
    defaultValues: {
      port: relaySettings.port.toString(),
      enableUpnpAndNatpmp: relaySettings.enableUpnpAndNatpmp,
    },
    validators: {
      onChange: schema,
    },
    listeners: {
      onChangeDebounceMs: 1000,
      onChange: ({ formApi }) => {
        if (formApi.state.isValid) {
          formApi.handleSubmit()
        }
      },
    },
    onSubmit: async ({ value, formApi }) => {
      try {
        await updateSetting({
          ...value,
          port: Number(value.port),
        })
      } catch (error) {
        formApi.reset()
        const message = parseApiError(error)
        toast.error(message)
      }
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Használt port</CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-6">
        <form.Field name="port">
          {(field) => (
            <Field>
              <FieldLabel>Port a bejövő kapcsolatokhoz</FieldLabel>
              <InputGroup>
                <InputGroupInput
                  placeholder="Nincs limitálva"
                  disabled={vpnManaged}
                  inputMode="numeric"
                  id={field.name}
                  name={field.name}
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={(e) => {
                    const value = e.target.value

                    if (isEmpty(value)) {
                      field.handleChange('0')
                    } else {
                      field.handleChange(e.target.value)
                    }
                  }}
                />
              </InputGroup>
              {field.state.meta.isTouched && (
                <FieldError errors={field.state.meta.errors} />
              )}
            </Field>
          )}
        </form.Field>
        <form.Field name="enableUpnpAndNatpmp">
          {(field) => (
            <div className="flex items-center space-x-2">
              <Switch
                id={field.name}
                checked={field.state.value}
                disabled={vpnManaged}
                onCheckedChange={field.handleChange}
              />
              <Label htmlFor="airplane-mode">
                UPnP / NAT-PMP használata a routeren a porttovábbításhoz
              </Label>
            </div>
          )}
        </form.Field>
        {vpnManaged && (
          <p className="text-sm text-muted-foreground">
            VPN által kezelt: VPN módban nincs bejövő port és porttovábbítás.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
