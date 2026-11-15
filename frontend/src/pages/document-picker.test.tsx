import '@testing-library/jest-dom/vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { RENDER_CAP } from '../components/DocumentPicker'
import { doc, renderAt, resetStore } from '../test/helpers'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: { health: vi.fn(), listDocuments: vi.fn(), chat: vi.fn(), chatStream: vi.fn() },
  ApiError: class ApiError extends Error {},
}))
const m = vi.mocked(api)

const id = (i: number) => `d${String(i).padStart(7, '0')}-0000-0000-0000-000000000000`
const many = [
  ...Array.from({ length: 300 }, (_, i) =>
    doc({ id: id(i), filename: `doc-${String(i).padStart(3, '0')}.md` }),
  ),
  doc({ id: id(900), filename: 'pending-one.md', status: 'pending' }),
  doc({ id: id(901), filename: 'failed-one.md', status: 'failed' }),
]

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  resetStore()
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
  m.listDocuments.mockResolvedValue(many)
  m.chat.mockResolvedValue({ answer: 'ok', sources: [] })
  Element.prototype.scrollIntoView = vi.fn()
})

const open = async () => {
  renderAt('/chat')
  const box = await screen.findByRole('combobox', { name: /Search in/ })
  await waitFor(() => expect(m.listDocuments).toHaveBeenCalled())
  await userEvent.click(box)
  return box
}

describe('document picker (combobox + listbox)', () => {
  it('defaults to All documents, wires ARIA and only lists ready documents', async () => {
    const box = await open()
    const list = await screen.findByRole('listbox')
    expect(list).toHaveAttribute('aria-multiselectable', 'true')
    expect(box).toHaveAttribute('aria-expanded', 'true')
    expect(box).toHaveAttribute('aria-controls', list.id)
    expect(screen.getByRole('option', { name: /All documents/ })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.queryByRole('option', { name: /pending-one|failed-one/ })).toBeNull()
    expect(screen.getByText(/2 documents are not ready yet/)).toBeInTheDocument()
    expect(screen.getAllByText('All documents').length).toBeGreaterThan(0)
  })

  it('caps rendered options for hundreds of documents and filters by name', async () => {
    const box = await open()
    expect(screen.getAllByRole('option').length).toBe(RENDER_CAP + 1) // +All documents
    expect(screen.getByText(/Showing 200 of 300/)).toBeInTheDocument()
    await userEvent.type(box, 'doc-29')
    const opts = screen.getAllByRole('option')
    expect(opts).toHaveLength(10) // doc-290..doc-299, "All documents" hidden while filtering
    expect(opts[0]).toHaveTextContent('doc-290.md')
    await userEvent.clear(box)
    await userEvent.type(box, 'nonexistent')
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByText(/No ready document matches/)).toBeInTheDocument()
  })

  it('selects by click, shows chips collapsed to +N, removes chips, and sends document_ids', async () => {
    const box = await open()
    for (const n of ['doc-005.md', 'doc-006.md', 'doc-007.md', 'doc-008.md']) {
      await userEvent.click(screen.getByRole('option', { name: new RegExp(n) }))
    }
    expect(screen.getByRole('option', { name: /doc-005/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('option', { name: /All documents/ })).toHaveAttribute(
      'aria-selected',
      'false',
    )
    expect(screen.getByRole('button', { name: 'Remove doc-005.md' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove doc-007.md' })).toBeNull() // collapsed
    await userEvent.click(screen.getByRole('button', { name: 'Show 2 more selected documents' }))
    expect(screen.getByRole('button', { name: 'Remove doc-008.md' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Remove doc-008.md' }))
    expect(screen.queryByRole('button', { name: 'Remove doc-008.md' })).toBeNull()
    expect(box).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Question'), 'q')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await waitFor(() =>
      expect(m.chat).toHaveBeenCalledWith({ question: 'q', document_ids: [id(5), id(6), id(7)] }),
    )
  })

  it('is operable from the keyboard: arrows, Enter, Home/End, Backspace, Escape', async () => {
    const box = await open()
    await userEvent.keyboard('{ArrowDown}{ArrowDown}') // All documents -> doc-000 -> doc-001
    const active = () => document.getElementById(box.getAttribute('aria-activedescendant') ?? '')
    expect(active()).toHaveTextContent('doc-001.md')
    await userEvent.keyboard('{Enter}')
    expect(screen.getByRole('option', { name: /doc-001/ })).toHaveAttribute('aria-selected', 'true')
    expect(box).toHaveFocus() // DOM focus never leaves the input
    await userEvent.keyboard('{End}')
    expect(active()).toHaveTextContent('doc-199.md')
    await userEvent.keyboard('{Home}{Enter}') // All documents clears the selection
    expect(screen.getByRole('option', { name: /All documents/ })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await userEvent.keyboard('{ArrowDown}{Enter}{Backspace}') // select doc-000, then Backspace removes it
    expect(screen.getByRole('option', { name: /All documents/ })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(box).toHaveAttribute('aria-expanded', 'false')
    expect(box).toHaveFocus()
  })

  it('closes on an outside pointer press and keeps the selection as chips', async () => {
    await open()
    await userEvent.click(screen.getByRole('option', { name: /doc-001/ }))
    await userEvent.click(document.body)
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(
      within(document.querySelector('.picker-field')!).getByText('doc-001.md'),
    ).toBeInTheDocument()
  })
})
