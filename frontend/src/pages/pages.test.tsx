import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { doc, job, renderAt, resetStore } from '../test/helpers'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: {
    health: vi.fn(),
    listDocuments: vi.fn(),
    getDocument: vi.fn(),
    listChunks: vi.fn(),
    deleteDocument: vi.fn(),
    getJob: vi.fn(),
    upload: vi.fn(),
    replace: vi.fn(),
    chat: vi.fn(),
    chatStream: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

const m = vi.mocked(api)

beforeEach(() => {
  vi.clearAllMocks()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
})

describe('Documents page', () => {
  it('shows the empty state', async () => {
    renderAt('/documents')
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument()
  })

  it('lists documents with status', async () => {
    m.listDocuments.mockResolvedValue([
      doc(),
      doc({ id: 'x2', filename: 'b.md', status: 'failed' }),
    ])
    renderAt('/documents')
    expect(await screen.findByRole('link', { name: 'report.pdf' })).toBeInTheDocument()
    expect(screen.getAllByTestId('status-badge').map((b) => b.textContent)).toEqual([
      'ready',
      'failed',
    ])
  })

  it('uploads a file and reflects pending → running → done via polling', async () => {
    m.upload.mockResolvedValue({ document: doc({ status: 'pending' }), job: job() })
    m.getJob
      .mockResolvedValueOnce(job({ status: 'pending' }))
      .mockResolvedValueOnce(job({ status: 'running' }))
      .mockResolvedValue(job({ status: 'done' }))
    renderAt('/documents')
    await screen.findByText(/No documents yet/)

    const file = new File(['hello'], 'report.pdf', { type: 'application/pdf' })
    await userEvent.upload(screen.getByLabelText('Document'), file)
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))

    const tracker = await screen.findByLabelText('job report.pdf')
    expect(m.upload).toHaveBeenCalledWith(file)
    await waitFor(() =>
      expect(within(tracker).getByTestId('status-badge')).toHaveTextContent('running'),
    )
    await waitFor(() =>
      expect(within(tracker).getByTestId('status-badge')).toHaveTextContent('done'),
    )
    expect(m.getJob).toHaveBeenCalledWith(job().id)
    // documents list refreshed after the job settled
    await waitFor(() => expect(m.listDocuments.mock.calls.length).toBeGreaterThan(2))
  })

  it('shows the job error when ingest fails', async () => {
    m.upload.mockResolvedValue({ document: doc({ status: 'pending' }), job: job() })
    m.getJob.mockResolvedValue(job({ status: 'failed', error: 'RuntimeError: parser exploded' }))
    renderAt('/documents')
    await screen.findByText(/No documents yet/)
    await userEvent.upload(screen.getByLabelText('Document'), new File(['x'], 'report.pdf'))
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(await screen.findByText(/parser exploded/)).toBeInTheDocument()
  })

  it('surfaces upload API errors without crashing', async () => {
    m.upload.mockRejectedValue(new Error('File exceeds the limit.'))
    renderAt('/documents')
    await screen.findByText(/No documents yet/)
    await userEvent.upload(screen.getByLabelText('Document'), new File(['x'], 'big.txt'))
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('big.txt: File exceeds the limit.')
  })

  it('requires a file before uploading', async () => {
    renderAt('/documents')
    await screen.findByText(/No documents yet/)
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a file first.')
    expect(m.upload).not.toHaveBeenCalled()
  })

  it('deletes a document after confirmation and removes the row', async () => {
    m.listDocuments.mockResolvedValue([doc()])
    m.deleteDocument.mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderAt('/documents')
    await userEvent.click(await screen.findByRole('button', { name: 'delete report.pdf' }))
    expect(m.deleteDocument).toHaveBeenCalledWith(doc().id)
    await waitFor(() => expect(screen.queryByRole('link', { name: 'report.pdf' })).toBeNull())
  })

  it('does not delete when confirmation is declined', async () => {
    m.listDocuments.mockResolvedValue([doc()])
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderAt('/documents')
    await userEvent.click(await screen.findByRole('button', { name: 'delete report.pdf' }))
    expect(m.deleteDocument).not.toHaveBeenCalled()
  })
})

describe('Document detail page', () => {
  it('renders chunks and jobs', async () => {
    m.getDocument.mockResolvedValue({ ...doc(), jobs: [job({ status: 'done' })] })
    m.listChunks.mockResolvedValue([{ id: 'c1', index: 0, text: 'first chunk text', metadata: {} }])
    renderAt(`/documents/${doc().id}`)
    expect(await screen.findByRole('heading', { name: 'report.pdf' })).toBeInTheDocument()
    expect(screen.getByText('first chunk text')).toBeInTheDocument()
    expect(screen.getByText('Chunks (1)')).toBeInTheDocument()
  })

  it('shows an error for a missing document', async () => {
    m.getDocument.mockRejectedValue(new Error('Document not found.'))
    m.listChunks.mockRejectedValue(new Error('Document not found.'))
    renderAt('/documents/nope')
    expect(await screen.findByRole('alert')).toHaveTextContent('Document not found.')
  })
})

describe('Document replace (incremental update)', () => {
  it('uploads a new version and shows how little was re-embedded', async () => {
    m.getDocument.mockResolvedValue({ ...doc(), jobs: [] })
    m.listChunks.mockResolvedValue([])
    m.replace.mockResolvedValue({ document: doc({ status: 'pending' }), job: job() })
    m.getJob.mockResolvedValue(
      job({
        status: 'done',
        stats: {
          mode: 'incremental',
          reason: 'unchanged chunks reused',
          chunks_total: 40,
          kept: 38,
          added: 2,
          removed: 1,
          embedded: 2,
        },
      }),
    )
    renderAt(`/documents/${doc().id}`)
    await screen.findByRole('heading', { name: 'report.pdf' })
    const file = new File(['v2'], 'report.pdf')
    await userEvent.upload(screen.getByLabelText('New version'), file)
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }))
    expect(m.replace).toHaveBeenCalledWith(doc().id, file)
    expect(await screen.findByTestId('job-stats')).toHaveTextContent(
      'Updated: re-embedded 2 of 40 chunks (38 unchanged, 1 removed)',
    )
    // detail data is reloaded once the job settles
    await waitFor(() => expect(m.getDocument.mock.calls.length).toBeGreaterThan(1))
  })

  it('shows replace errors and requires a file', async () => {
    m.getDocument.mockResolvedValue({ ...doc(), jobs: [] })
    m.listChunks.mockResolvedValue([])
    m.replace.mockRejectedValue(new Error('File exceeds the limit.'))
    renderAt(`/documents/${doc().id}`)
    await screen.findByRole('heading', { name: 'report.pdf' })
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a file first.')
    await userEvent.upload(screen.getByLabelText('New version'), new File(['x'], 'a.md'))
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }))
    expect(await screen.findByText('File exceeds the limit.')).toBeInTheDocument()
  })
})

describe('Chat page', () => {
  const answer = {
    answer: 'The battery lasts 14 hours [1].',
    sources: [
      {
        text: 'battery life of 14 hours',
        document_id: 'd1000000-0000-0000-0000-000000000001',
        chunk_index: 3,
        filename: 'zephyr.md',
        score: 0.1,
      },
    ],
  }

  it('asks a question and renders the answer with citations', async () => {
    m.chat.mockResolvedValue(answer)
    renderAt('/chat')
    await userEvent.type(screen.getByLabelText('Question'), 'How long is the battery?')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    const section = await screen.findByLabelText('answer')
    expect(within(section).getByText(/The battery lasts 14 hours/)).toBeInTheDocument()
    expect(
      within(section).getByRole('button', { name: /^Source 1: zephyr\.md, chunk 3/ }),
    ).toBeInTheDocument()
    expect(within(section).getByRole('region', { name: 'zephyr.md' })).toBeInTheDocument()
    expect(m.chat).toHaveBeenCalledWith({ question: 'How long is the battery?' })
  })

  it('sends document_ids when documents are selected', async () => {
    m.listDocuments.mockResolvedValue([
      doc({ filename: 'zephyr.md' }),
      doc({ id: 'p', status: 'pending', filename: 'wip.md' }),
    ])
    m.chat.mockResolvedValue(answer)
    renderAt('/chat')
    await userEvent.click(await screen.findByRole('combobox', { name: /Search in/ }))
    await userEvent.click(await screen.findByRole('option', { name: /zephyr\.md/ }))
    expect(screen.queryByRole('option', { name: /wip\.md/ })).toBeNull() // only ready docs are selectable
    await userEvent.type(screen.getByLabelText('Question'), 'q')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await waitFor(() =>
      expect(m.chat).toHaveBeenCalledWith({ question: 'q', document_ids: [doc().id] }),
    )
  })

  it('shows chat errors', async () => {
    m.chat.mockRejectedValue(new Error('LLM provider returned 503'))
    renderAt('/chat')
    await userEvent.type(screen.getByLabelText('Question'), 'q')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('LLM provider returned 503')
  })

  it('disables Ask for a blank question', () => {
    renderAt('/chat')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  })
})

describe('Overview page', () => {
  it('shows health checks and status counts', async () => {
    m.health.mockResolvedValue({ status: 'ok', checks: { db: { ok: true, detail: 'connected' } } })
    m.listDocuments.mockResolvedValue([doc(), doc({ id: 'b', status: 'failed' })])
    renderAt('/')
    expect(await screen.findByText('connected')).toBeInTheDocument()
    expect(await screen.findByText(/Total 2 · ready 1/)).toBeInTheDocument()
  })

  it('reports an unreachable API', async () => {
    m.health.mockRejectedValue(new Error('Network error'))
    renderAt('/')
    expect(await screen.findByText(/API unreachable/)).toBeInTheDocument()
  })
})
