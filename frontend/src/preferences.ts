/** The effective editable settings and shared version returned by the local application. */
export type Preferences = {
  timezone: string
  source: PreferenceSource
  model_id: string | null
  model_source: PreferenceSource
  version: number
}

export type PreferenceSource = 'default' | 'preferences' | 'installation' | 'environment'

const SOURCE_LABELS: Record<PreferenceSource, string> = {
  default: 'Packaged default',
  preferences: 'Saved SQLite preference',
  installation: 'Installation TOML override',
  environment: 'Environment override',
}

/** Name the active layer and identify any configuration key that can override the setting. */
export function preferenceSource(source: PreferenceSource, key: 'TIMEZONE' | 'MODEL_ID') {
  if (source === 'environment') return `${SOURCE_LABELS[source]} (DML_${key})`
  if (source === 'installation') return `${SOURCE_LABELS[source]} (${key.toLowerCase()}). An environment override can also set DML_${key}.`
  return SOURCE_LABELS[source]
}
