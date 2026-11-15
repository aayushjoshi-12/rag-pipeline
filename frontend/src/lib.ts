/** Error -> display string. */
export const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** Add ``x`` to ``list`` unless present, in which case remove it. */
export const toggle = <T>(list: T[], x: T): T[] =>
  list.includes(x) ? list.filter((y) => y !== x) : [...list, x]

/** The API returns naive UTC timestamps; show them in the viewer's locale. */
export function when(value: string | null, empty = ''): string {
  if (!value) return empty
  const iso = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}
