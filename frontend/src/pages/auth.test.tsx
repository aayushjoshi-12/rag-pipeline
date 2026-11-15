import '@testing-library/jest-dom/vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { clearTokens, getTokens } from '../auth/session'
import { useAuth } from '../store/auth'
import { ALL_PERMISSIONS, doc, renderAt, resetStore, signInAs } from '../test/helpers'
import type { ManagedUser, Role } from '../types'

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
    login: vi.fn(),
    me: vi.fn(),
    listPermissions: vi.fn(),
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
    listRoles: vi.fn(),
    createRole: vi.fn(),
    updateRole: vi.fn(),
    deleteRole: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

const m = vi.mocked(api)

const role = (over: Partial<Role> = {}): Role => ({
  id: 'r-user',
  name: 'user',
  description: 'Upload documents and chat',
  is_builtin: true,
  permissions: ['documents:read', 'chat:use'],
  ...over,
})
const user = (over: Partial<ManagedUser> = {}): ManagedUser => ({
  id: 'u2',
  email: 'bob@example.test',
  is_active: true,
  roles: ['user'],
  permissions: ['documents:read'],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  clearTokens()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
  m.listPermissions.mockResolvedValue(
    ALL_PERMISSIONS.map((name) => ({ name, description: `${name} desc` })),
  )
})

describe('sign in', () => {
  beforeEach(() => useAuth.setState({ status: 'loading', me: null, error: null }))

  it('shows the login form when the server rejects an anonymous session', async () => {
    m.me.mockRejectedValue(new Error('not authenticated'))
    renderAt('/')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Documents' })).not.toBeInTheDocument()
  })

  it('logs in, stores the tokens and shows the app for that user', async () => {
    m.me.mockRejectedValueOnce(new Error('not authenticated'))
    m.login.mockResolvedValue({
      access_token: 'acc',
      refresh_token: 'ref',
      token_type: 'bearer',
      expires_in: 60,
    })
    m.me.mockResolvedValue({
      id: 'u1',
      email: 'alice@example.test',
      permissions: ['documents:read'],
      auth_enabled: true,
    })
    renderAt('/')
    await userEvent.type(await screen.findByLabelText('Email'), 'alice@example.test')
    await userEvent.type(screen.getByLabelText('Password'), 'hunter2-hunter2')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByTestId('whoami')).toHaveTextContent('alice@example.test')
    expect(m.login).toHaveBeenCalledWith('alice@example.test', 'hunter2-hunter2')
    expect(getTokens()).toEqual({ access: 'acc', refresh: 'ref' })
  })

  it('shows the server error and stays on the form after a failed login', async () => {
    m.me.mockRejectedValue(new Error('not authenticated'))
    m.login.mockRejectedValue(new Error('invalid credentials'))
    renderAt('/')
    await userEvent.type(await screen.findByLabelText('Email'), 'a@b.test')
    await userEvent.type(screen.getByLabelText('Password'), 'wrong-wrong')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid credentials')
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(getTokens()).toBeNull()
  })

  it('skips the login form when the server has auth disabled', async () => {
    m.me.mockResolvedValue({
      id: null,
      email: 'anonymous',
      permissions: ALL_PERMISSIONS,
      auth_enabled: false,
    })
    renderAt('/')
    expect(await screen.findByText('auth disabled')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument()
  })

  it('signing out returns to the login form and clears tokens', async () => {
    signInAs(['documents:read'], 'carol@example.test')
    renderAt('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(getTokens()).toBeNull()
  })
})

describe('permission-aware UI', () => {
  it('hides navigation links the user has no permission for', async () => {
    signInAs(['documents:read'])
    renderAt('/')
    expect(await screen.findByRole('link', { name: 'Documents' })).toBeInTheDocument()
    for (const hidden of ['Chat', 'Users', 'Roles'])
      expect(screen.queryByRole('link', { name: hidden })).not.toBeInTheDocument()
  })

  it('shows an explicit notice on a forbidden deep link instead of the page', async () => {
    signInAs(['documents:read'])
    renderAt('/users')
    expect(await screen.findByRole('alert')).toHaveTextContent('users:manage')
    expect(m.listUsers).not.toHaveBeenCalled()
  })

  it('read-only users see documents but no upload form and no delete buttons', async () => {
    signInAs(['documents:read'])
    m.listDocuments.mockResolvedValue([doc()])
    renderAt('/documents')
    expect(await screen.findByRole('link', { name: 'report.pdf' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
  })

  it('writers get upload but not delete; deleters get delete', async () => {
    m.listDocuments.mockResolvedValue([doc()])
    signInAs(['documents:read', 'documents:write'])
    const first = renderAt('/documents')
    await screen.findByRole('link', { name: 'report.pdf' })
    expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
    first.unmount()

    signInAs(['documents:read', 'documents:delete'])
    renderAt('/documents')
    expect(await screen.findByRole('button', { name: 'delete report.pdf' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument()
  })
})

describe('admin: roles', () => {
  it('lists roles with checked permissions and the built-in admin locked', async () => {
    m.listRoles.mockResolvedValue([
      role({ id: 'r-admin', name: 'admin', permissions: ALL_PERMISSIONS }),
      role(),
    ])
    renderAt('/roles')
    const userBox = await screen.findByRole('checkbox', { name: 'user chat:use' })
    expect(userBox).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'user users:manage' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'admin users:manage' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'delete role user' })).not.toBeInTheDocument()
  })

  it('creates a role with the chosen permissions', async () => {
    m.listRoles.mockResolvedValue([])
    m.createRole.mockResolvedValue(role({ id: 'r9', name: 'auditor', is_builtin: false }))
    renderAt('/roles')
    await userEvent.type(await screen.findByLabelText('Role name'), 'auditor')
    const newRole = screen.getByRole('group', { name: 'Permissions' })
    await userEvent.click(within(newRole).getByRole('checkbox', { name: /documents:read/ }))
    await userEvent.click(within(newRole).getByRole('checkbox', { name: /jobs:read/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Create role' }))
    await waitFor(() =>
      expect(m.createRole).toHaveBeenCalledWith({
        name: 'auditor',
        description: '',
        permissions: ['documents:read', 'jobs:read'],
      }),
    )
  })

  it('toggling a permission sends the full new permission list', async () => {
    m.listRoles.mockResolvedValue([role()])
    m.updateRole.mockResolvedValue(role())
    renderAt('/roles')
    await userEvent.click(await screen.findByRole('checkbox', { name: 'user chat:use' }))
    await waitFor(() =>
      expect(m.updateRole).toHaveBeenCalledWith('r-user', { permissions: ['documents:read'] }),
    )
  })

  it('surfaces a server refusal (e.g. role still assigned)', async () => {
    m.listRoles.mockResolvedValue([role({ id: 'r2', name: 'ops', is_builtin: false })])
    m.deleteRole.mockRejectedValue(new Error('role is assigned to 2 user(s)'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderAt('/roles')
    await userEvent.click(await screen.findByRole('button', { name: 'delete role ops' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('assigned to 2 user')
  })
})

describe('admin: users', () => {
  beforeEach(() => {
    m.listRoles.mockResolvedValue([
      role({ id: 'r-admin', name: 'admin', permissions: ALL_PERMISSIONS }),
      role(),
    ])
    m.listUsers.mockResolvedValue([
      user({ id: 'u1', email: 'admin@example.test', roles: ['admin'] }),
      user(),
    ])
  })

  it('lists users and never offers to delete yourself', async () => {
    renderAt('/users')
    expect(await screen.findByText('bob@example.test')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'delete bob@example.test' })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'delete admin@example.test' }),
    ).not.toBeInTheDocument()
  })

  it('creates a user with the selected roles', async () => {
    m.createUser.mockResolvedValue(user({ id: 'u3', email: 'new@example.test' }))
    renderAt('/users')
    await userEvent.type(await screen.findByLabelText('Email'), 'new@example.test')
    await userEvent.type(screen.getByLabelText('Password'), 'long-enough-pw')
    const roles = screen.getByRole('group', { name: 'Roles' })
    await userEvent.click(within(roles).getByRole('checkbox', { name: 'user' }))
    await userEvent.click(screen.getByRole('button', { name: 'Create user' }))
    await waitFor(() =>
      expect(m.createUser).toHaveBeenCalledWith({
        email: 'new@example.test',
        password: 'long-enough-pw',
        role_ids: ['r-user'],
      }),
    )
  })

  it('deactivating sends is_active=false and shows the last-admin refusal', async () => {
    m.updateUser.mockRejectedValue(new Error('cannot remove or deactivate the last active admin'))
    renderAt('/users')
    await userEvent.click(
      await screen.findByRole('checkbox', { name: 'admin@example.test active' }),
    )
    expect(m.updateUser).toHaveBeenCalledWith('u1', { is_active: false })
    expect(await screen.findByRole('alert')).toHaveTextContent('last active admin')
  })

  it('changing a role checkbox sends the resulting role ids', async () => {
    m.updateUser.mockResolvedValue(user())
    renderAt('/users')
    await userEvent.click(await screen.findByRole('checkbox', { name: 'bob@example.test admin' }))
    await waitFor(() =>
      expect(m.updateUser).toHaveBeenCalledWith('u2', { role_ids: ['r-user', 'r-admin'] }),
    )
  })
})
