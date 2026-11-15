import '@testing-library/jest-dom/vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { clearTokens } from '../auth/session'
import { renderAt, resetStore, signInAs } from '../test/helpers'
import type { ApiKey, ApiKeyCreated, Role } from '../types'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: {
    health: vi.fn(),
    listDocuments: vi.fn(),
    me: vi.fn(),
    listRoles: vi.fn(),
    listApiKeys: vi.fn(),
    createApiKey: vi.fn(),
    revokeApiKey: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

const m = vi.mocked(api)

const role = (over: Partial<Role> = {}): Role => ({
  id: 'r-search',
  name: 'retrieval-only',
  description: '',
  is_builtin: false,
  permissions: ['search:use'],
  ...over,
})
const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: 'k1',
  name: 'billing-service',
  prefix: 'abcd1234',
  role_id: 'r-search',
  role: 'retrieval-only',
  owner_email: 'admin@example.test',
  created_at: '2026-11-14T10:00:00',
  expires_at: '2027-02-12T10:00:00',
  last_used_at: null,
  revoked_at: null,
  status: 'active',
  ...over,
})
const SECRET = 'rag_abcd1234_SuperSecretValueThatIsShownOnce0123456789'
const createdKey = (over: Partial<ApiKeyCreated> = {}): ApiKeyCreated => ({
  ...key(),
  key: SECRET,
  warnings: [],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  clearTokens()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
  m.listRoles.mockResolvedValue([
    role(),
    role({ id: 'r-admin', name: 'admin', permissions: ['users:manage', 'documents:delete'] }),
  ])
  m.listApiKeys.mockResolvedValue([key()])
})

describe('API keys page', () => {
  it('shows the nav item only with apikeys:manage', async () => {
    signInAs(['documents:read'])
    renderAt('/')
    expect(await screen.findByRole('link', { name: 'Documents' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'API keys' })).not.toBeInTheDocument()
  })

  it('explains a forbidden deep link', async () => {
    signInAs(['documents:read'])
    renderAt('/api-keys')
    expect(await screen.findByRole('alert')).toHaveTextContent('apikeys:manage')
  })

  it('lists keys with prefix, role and status but never a secret', async () => {
    signInAs()
    renderAt('/api-keys')
    expect(await screen.findByRole('heading', { name: 'API keys' })).toBeInTheDocument()
    const row = (await screen.findByText('billing-service')).closest('tr') as HTMLElement
    expect(within(row).getByText('rag_abcd1234_…')).toBeInTheDocument()
    expect(within(row).getByText('retrieval-only')).toBeInTheDocument()
    expect(within(row).getByTestId('key-status')).toHaveTextContent('active')
    expect(within(row).getByText('never')).toBeInTheDocument() // last used
    expect(document.body).not.toHaveTextContent('SuperSecret')
  })

  it('creates a key, reveals the secret once in a dialog with a warning and a copy button', async () => {
    signInAs()
    m.createApiKey.mockResolvedValue(createdKey())
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderAt('/api-keys')
    await userEvent.type(await screen.findByLabelText('Name'), 'billing-service')
    await userEvent.selectOptions(screen.getByLabelText('Role'), 'r-search')
    await userEvent.selectOptions(screen.getByLabelText('Expires'), '30 days')
    await userEvent.click(screen.getByRole('button', { name: 'Create key' }))

    expect(m.createApiKey).toHaveBeenCalledWith({
      name: 'billing-service',
      role_id: 'r-search',
      expires_in_days: 30,
    })
    const dialog = await screen.findByRole('dialog', { name: 'Copy your API key' })
    expect(within(dialog).getByText(/You will not see this key again/)).toBeInTheDocument()
    expect(within(dialog).getByTestId('new-key-value')).toHaveTextContent(SECRET)

    await userEvent.click(within(dialog).getByRole('button', { name: 'Copy API key' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SECRET))
    expect(await within(dialog).findByText('Copied to the clipboard.')).toBeInTheDocument()

    // closing discards the secret: it is nowhere on the page afterwards
    await userEvent.click(within(dialog).getByRole('button', { name: 'I have stored the key' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(SECRET)
  })

  it('does not close the reveal dialog on Escape and shows the admin-role warning', async () => {
    signInAs()
    m.createApiKey.mockResolvedValue(
      createdKey({ warnings: ['This key is bound to the admin role'] }),
    )
    renderAt('/api-keys')
    await userEvent.type(await screen.findByLabelText('Name'), 'root-ish')
    await userEvent.selectOptions(screen.getByLabelText('Role'), 'r-admin')
    expect(
      screen.getByText(/admin role lets this key read, write and delete everything/),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Create key' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('This key is bound to the admin role')).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('shows a copy fallback message when the clipboard is unavailable', async () => {
    signInAs()
    m.createApiKey.mockResolvedValue(createdKey())
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    renderAt('/api-keys')
    await userEvent.type(await screen.findByLabelText('Name'), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Create key' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Copy API key' }))
    expect(await screen.findByText(/Copy failed/)).toBeInTheDocument()
  })

  it('revokes only after confirmation', async () => {
    signInAs()
    m.revokeApiKey.mockResolvedValue(key({ status: 'revoked', revoked_at: '2026-11-14T11:00:00' }))
    const confirm = vi.spyOn(window, 'confirm')
    renderAt('/api-keys')
    const btn = await screen.findByRole('button', { name: 'revoke billing-service' })
    confirm.mockReturnValueOnce(false)
    await userEvent.click(btn)
    expect(m.revokeApiKey).not.toHaveBeenCalled()
    confirm.mockReturnValueOnce(true)
    m.listApiKeys.mockResolvedValue([key({ status: 'revoked', revoked_at: '2026-11-14T11:00:00' })])
    await userEvent.click(btn)
    await waitFor(() => expect(m.revokeApiKey).toHaveBeenCalledWith('k1'))
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'revoke billing-service' }),
      ).not.toBeInTheDocument(),
    )
    expect(screen.getByTestId('key-status')).toHaveTextContent('revoked')
    confirm.mockRestore()
  })

  it('shows the server error and an empty state', async () => {
    signInAs()
    m.listApiKeys.mockResolvedValue([])
    m.createApiKey.mockRejectedValue(
      new Error('role has permissions you do not hold: documents:delete'),
    )
    renderAt('/api-keys')
    expect(await screen.findByText('No API keys yet')).toBeInTheDocument()
    await userEvent.type(await screen.findByLabelText('Name'), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Create key' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('you do not hold')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('tells a user without roles:manage why they cannot pick a role', async () => {
    signInAs(['apikeys:manage'])
    renderAt('/api-keys')
    expect(await screen.findByText(/needs the roles:manage permission/)).toBeInTheDocument()
    expect(m.listRoles).not.toHaveBeenCalled()
  })
})
