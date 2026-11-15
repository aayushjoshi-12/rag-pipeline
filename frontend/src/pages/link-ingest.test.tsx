import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { doc, job, renderAt, resetApp } from '../test/helpers'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: {
    health: vi.fn(),
    listDocuments: vi.fn(),
    getJob: vi.fn(),
    ingestUrl: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

const m = vi.mocked(api)

beforeEach(() => {
  resetApp(m)
})

describe('Add from link', () => {
  it('submits the link and shows the job with source and bytes', async () => {
    const url = 'https://example.com/rfc.txt'
    m.ingestUrl.mockResolvedValue({
      document: doc({ filename: 'rfc.txt', status: 'pending', source_url: url }),
      job: job({ status: 'pending' }),
    })
    m.getJob.mockResolvedValue(
      job({
        status: 'done',
        stats: {
          mode: 'full',
          reason: 'new',
          chunks_total: 7,
          kept: 0,
          added: 7,
          removed: 0,
          embedded: 7,
          source_url: url,
          bytes_downloaded: 2048,
        },
      }),
    )
    renderAt('/documents')
    await userEvent.type(await screen.findByLabelText('Document URL'), `  ${url} `)
    await userEvent.click(screen.getByRole('button', { name: 'Add link' }))
    expect(m.ingestUrl).toHaveBeenCalledWith(url)
    const tracker = await screen.findByLabelText('job rfc.txt')
    await waitFor(() =>
      expect(within(tracker).getByTestId('status-badge')).toHaveTextContent('done'),
    )
    expect(within(tracker).getByTestId('job-source')).toHaveTextContent(`From ${url} · 2 KB`)
    expect(screen.getByLabelText('Document URL')).toHaveValue('')
  })

  it('shows the API error text and keeps the link', async () => {
    m.ingestUrl.mockRejectedValue(new Error('That URL points at a private or internal address.'))
    renderAt('/documents')
    await userEvent.type(await screen.findByLabelText('Document URL'), 'http://127.0.0.1/a.pdf')
    await userEvent.click(screen.getByRole('button', { name: 'Add link' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('private or internal')
    expect(screen.getByLabelText('Document URL')).toHaveValue('http://127.0.0.1/a.pdf')
  })

  it('asks for a link before calling the API', async () => {
    renderAt('/documents')
    await userEvent.click(await screen.findByRole('button', { name: 'Add link' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a link first')
    expect(m.ingestUrl).not.toHaveBeenCalled()
  })
})
