export type HouseholdSetupDefaults = {
  smtp: { host: string; port: number; username: string; from_address: string;
    use_ssl: boolean; starttls: boolean; allow_external_delivery: true }
  recipient: string
}
export type SmtpSetupFields = { host: string; port: string; username: string; fromAddress: string; security: 'ssl' | 'starttls' | 'none' }

/** The IPC carries addresses/settings only. Unknown or secret fields are rejected. */
export function householdSetupDefaults(value: unknown): HouseholdSetupDefaults | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  if (Object.keys(raw).sort().join(',') !== 'recipient,smtp' || !raw.smtp || typeof raw.smtp !== 'object' || Array.isArray(raw.smtp)) return null
  const smtp = raw.smtp as Record<string, unknown>
  if (Object.keys(smtp).sort().join(',') !== 'allow_external_delivery,from_address,host,port,starttls,use_ssl,username') return null
  if (typeof raw.recipient !== 'string' || raw.recipient.length > 254 || !raw.recipient.includes('@') || raw.recipient !== raw.recipient.trim() || /[\r\n\0]/.test(raw.recipient)) return null
  for (const key of ['host', 'username', 'from_address']) {
    if (typeof smtp[key] !== 'string' || !(smtp[key] as string).trim() || smtp[key] !== (smtp[key] as string).trim() || (smtp[key] as string).length > 254 || /[\r\n\0]/.test(smtp[key] as string)) return null
  }
  if (!(smtp.from_address as string).includes('@') || !Number.isSafeInteger(smtp.port) || (smtp.port as number) < 1 || (smtp.port as number) > 65535
    || typeof smtp.use_ssl !== 'boolean' || typeof smtp.starttls !== 'boolean' || (smtp.use_ssl && smtp.starttls) || smtp.allow_external_delivery !== true) return null
  if (!smtp.use_ssl && !smtp.starttls && !['localhost', '127.0.0.1', '::1'].includes(smtp.host as string)) return null
  return { smtp: { host: smtp.host as string, port: smtp.port as number, username: smtp.username as string,
    from_address: smtp.from_address as string, use_ssl: smtp.use_ssl, starttls: smtp.starttls, allow_external_delivery: true }, recipient: raw.recipient }
}

export function canReuseSetupSmtpSecret(defaults: HouseholdSetupDefaults | null, secretConfigured: boolean, fields: SmtpSetupFields): boolean {
  if (!defaults || !secretConfigured) return false
  const smtp = defaults.smtp
  const security = smtp.use_ssl ? 'ssl' : smtp.starttls ? 'starttls' : 'none'
  return fields.host === smtp.host && fields.port === String(smtp.port) && fields.username === smtp.username
    && fields.fromAddress === smtp.from_address && fields.security === security
}
