import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '..')
const read = (p: string) => readFileSync(resolve(root, p), 'utf8')

describe('static assets', () => {
  it('ships the favicon the page links to (no /favicon.ico 404)', () => {
    const href = /<link rel="icon"[^>]*href="([^"]+)"/.exec(read('index.html'))?.[1]
    expect(href).toBe('/favicon.svg')
    expect(existsSync(resolve(root, 'public', href!.slice(1)))).toBe(true)
  })

  it('truncated table cells are block-level so ellipsis works and text cannot overlap the next cell', () => {
    const css = read('src/styles.css')
    expect(css).toMatch(/\.cell-main \.truncate\s*\{[^}]*display:\s*block/)
  })
})
