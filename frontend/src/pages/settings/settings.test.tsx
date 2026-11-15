import '@testing-library/jest-dom/vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../../api/client'
import { clearTokens } from '../../auth/session'
import { renderAt, resetStore, signInAs } from '../../test/helpers'
import type { SettingItem, SettingsView } from '../../types'

vi.mock('../../config', () => ({ POLL_MS: 10 }))
vi.mock('../../api/client', () => {
  class ApiError extends Error {
    status: number
    detail: unknown
    constructor(status: number, message: string, detail?: unknown) {
      super(message)
      this.status = status
      this.detail = detail
    }
  }
  return {
    ApiError,
    api: {
      health: vi.fn(),
      listDocuments: vi.fn(),
      me: vi.fn(),
      getSettings: vi.fn(),
      patchSettings: vi.fn(),
      settingsAudit: vi.fn(),
      reindexStatus: vi.fn(),
      queueReindex: vi.fn(),
      testConnection: vi.fn(),
    },
  }
})

const m = vi.mocked(api)
const CANARY = 'sk-CANARY-do-not-render-9981'

const item = (key: string, over: Partial<SettingItem> = {}): SettingItem => ({
  key,
  group: 'Retrieval',
  description: `About ${key}`,
  kind: 'integer',
  nullable: false,
  choices: null,
  unit: null,
  note: null,
  apply: 'live',
  secret: false,
  reindex_action: null,
  value: 5,
  is_set: null,
  default: 5,
  source: 'default',
  locked: false,
  locked_reason: null,
  disabled_reason: null,
  shadowed: false,
  shadowed_value: null,
  updated_at: null,
  updated_by: null,
  restart_pending: false,
  undecryptable: false,
  ...over,
})

const view = (over: Partial<SettingsView['summary']> = {}): SettingsView => ({
  groups: [
    { id: 'Retrieval', description: 'How passages are found.' },
    { id: 'Ingestion', description: 'Chunking.' },
  ],
  settings: [
    item('default_top_k'),
    item('chunk_size', {
      group: 'Ingestion',
      value: 700,
      default: 1000,
      source: 'env',
      locked: true,
      locked_reason: 'set by environment variable CHUNK_SIZE',
    }),
    item('llm_provider', {
      group: 'LLM',
      kind: 'enum',
      choices: ['openrouter', 'openai', 'anthropic'],
      value: 'openrouter',
      default: 'openrouter',
    }),
    item('default_llm_model', { group: 'LLM', kind: 'string', value: 'm1', default: 'm0' }),
    item('openrouter_api_key', {
      group: 'LLM',
      kind: 'string',
      secret: true,
      value: null,
      default: null,
      is_set: true,
      source: 'ui',
      updated_by: 'admin@example.test',
      updated_at: '2026-11-15T10:00:00',
    }),
    item('embedding_provider', {
      group: 'Embeddings',
      kind: 'enum',
      choices: ['ollama', 'openai'],
      value: 'ollama',
      default: 'ollama',
      apply: 'reindex',
    }),
    item('embedding_model', {
      group: 'Embeddings',
      kind: 'string',
      value: 'mxbai',
      default: 'mxbai',
      apply: 'reindex',
      note: 'Every stored chunk is re-embedded.',
    }),
  ],
  summary: {
    secrets_enabled: true,
    restart_pending: [],
    overrides_error: null,
    undecryptable: [],
    refresh_seconds: 5,
    missing: [],
    ...over,
  },
})

const patched = (over: object = {}) => ({
  changed: ['default_top_k'],
  reset: [],
  unchanged: [],
  restart_required: [],
  reindex_required: [],
  dry_run: false,
  reindex: null,
  settings: [],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  clearTokens()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.getSettings.mockResolvedValue(view())
  m.reindexStatus.mockResolvedValue({
    state: 'idle',
    needed: false,
    problems: [],
    queued_at: null,
    queued_by: null,
    kind: null,
    task_id: null,
    result: null,
    error: null,
  })
  m.settingsAudit.mockResolvedValue([])
})

describe('Settings page', () => {
  it('shows sources, defaults and the lock reason of an environment-pinned row', async () => {
    renderAt('/settings')
    const row = await screen.findByTestId('row-chunk_size')
    expect(within(row).getByText('environment')).toBeInTheDocument()
    expect(within(row).getByText('set by environment variable CHUNK_SIZE')).toBeInTheDocument()
    expect(within(row).getByRole('spinbutton')).toBeDisabled()
    expect(screen.getByTestId('source-default_top_k')).toHaveTextContent('default')
  })

  it('edits a live setting through the pending bar and saves it without a confirm step', async () => {
    m.patchSettings.mockResolvedValue(patched())
    const user = userEvent.setup()
    renderAt('/settings')
    const input = await screen.findByLabelText('default_top_k')
    await user.clear(input)
    await user.type(input, '9')
    expect(screen.getByTestId('pending-count')).toHaveTextContent('1 pending change')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(m.patchSettings).toHaveBeenCalledWith({
        set: { default_top_k: 9 },
        reset: [],
        confirm_reindex: false,
      }),
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(await screen.findByTestId('notice')).toHaveTextContent('Saved 1 setting')
    expect(screen.queryByTestId('pending-count')).not.toBeInTheDocument()
  })

  it('discards pending changes', async () => {
    const user = userEvent.setup()
    renderAt('/settings')
    const input = await screen.findByLabelText('default_top_k')
    await user.type(input, '7')
    await user.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.queryByTestId('pending-count')).not.toBeInTheDocument()
    expect(m.patchSettings).not.toHaveBeenCalled()
  })

  it('never renders a secret value: write-only replace flow', async () => {
    m.patchSettings.mockResolvedValue(patched({ changed: ['openrouter_api_key'] }))
    const user = userEvent.setup()
    renderAt('/settings')
    expect(await screen.findByTestId('secret-openrouter_api_key')).toHaveTextContent('set')
    await user.click(screen.getByRole('button', { name: 'Replace openrouter_api_key' }))
    const box = screen.getByPlaceholderText('Paste the new value')
    expect(box).toHaveAttribute('type', 'password')
    await user.type(box, CANARY)
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(m.patchSettings).toHaveBeenCalledWith({
        set: { openrouter_api_key: CANARY },
        reset: [],
        confirm_reindex: false,
      }),
    )
    await screen.findByTestId('notice')
    expect(document.body.innerHTML).not.toContain(CANARY)
    expect(screen.queryByPlaceholderText('Paste the new value')).not.toBeInTheDocument()
  })

  it('clearing a secret is a pending reset that can be undone', async () => {
    m.patchSettings.mockResolvedValue(patched({ changed: [], reset: ['openrouter_api_key'] }))
    const user = userEvent.setup()
    renderAt('/settings')
    await user.click(await screen.findByRole('button', { name: 'Clear openrouter_api_key' }))
    expect(screen.getByTestId('secret-openrouter_api_key')).toHaveTextContent('will be cleared')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(m.patchSettings).toHaveBeenCalledWith({
        set: {},
        reset: ['openrouter_api_key'],
        confirm_reindex: false,
      }),
    )
  })

  it('a reindex-requiring change needs the confirm step and sends confirm_reindex', async () => {
    m.patchSettings.mockResolvedValue(
      patched({
        changed: ['embedding_model'],
        reindex_required: ['embedding_model'],
        reindex: { queued: true, kinds: ['reindex'], task_ids: ['t1'], error: null },
      }),
    )
    const user = userEvent.setup()
    renderAt('/settings')
    const input = await screen.findByLabelText('embedding_model')
    await user.clear(input)
    await user.type(input, 'other-model')
    expect(screen.getByTestId('pending-count')).toHaveTextContent('reindex needed')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    const dialog = await screen.findByRole('dialog', { name: 'Confirm reindex' })
    expect(within(dialog).getByText(/Every stored chunk is re-embedded/)).toBeInTheDocument()
    expect(m.patchSettings).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Save and reindex' }))
    await waitFor(() =>
      expect(m.patchSettings).toHaveBeenCalledWith({
        set: { embedding_model: 'other-model' },
        reset: [],
        confirm_reindex: true,
      }),
    )
    expect(await screen.findByTestId('notice')).toHaveTextContent('reindex was queued')
  })

  it('cancelling the reindex confirm keeps the pending change and saves nothing', async () => {
    const user = userEvent.setup()
    renderAt('/settings')
    const input = await screen.findByLabelText('embedding_model')
    await user.type(input, 'x')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByTestId('pending-count')).toBeInTheDocument()
    expect(m.patchSettings).not.toHaveBeenCalled()
  })

  it('shows per-field validation errors from the server and keeps the pending change', async () => {
    m.patchSettings.mockRejectedValue(
      new ApiError(422, 'settings.default_top_k: must be within 1..50', [
        { loc: ['settings', 'default_top_k'], msg: 'must be within 1..50', type: 'value_error' },
      ]),
    )
    const user = userEvent.setup()
    renderAt('/settings')
    const input = await screen.findByLabelText('default_top_k')
    await user.clear(input)
    await user.type(input, '999')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    const row = screen.getByTestId('row-default_top_k')
    expect(await within(row).findByText('must be within 1..50')).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByTestId('pending-count')).toBeInTheDocument()
  })

  it('first-run: tells the admin what is missing', async () => {
    m.getSettings.mockResolvedValue(
      view({
        missing: [{ area: 'llm', message: "No API key for the LLM provider 'openrouter'." }],
      }),
    )
    renderAt('/settings')
    expect(await screen.findByText('Finish setting up')).toBeInTheDocument()
    expect(screen.getByText(/No API key for the LLM provider/)).toBeInTheDocument()
  })

  it('warns when secrets are disabled', async () => {
    m.getSettings.mockResolvedValue(view({ secrets_enabled: false }))
    renderAt('/settings')
    expect(await screen.findByText(/Secrets are disabled/)).toBeInTheDocument()
  })

  it('test connection reports the result', async () => {
    m.testConnection.mockResolvedValue({
      service: 'llm',
      ok: true,
      detail: 'API key accepted',
      provider: 'openrouter',
      latency_ms: 120,
    })
    const user = userEvent.setup()
    renderAt('/settings')
    await user.click(
      await screen.findByRole('button', { name: 'Test connection: Chat model (LLM)' }),
    )
    expect(await screen.findByTestId('test-llm')).toHaveTextContent('Works: API key accepted')
    expect(m.testConnection).toHaveBeenCalledWith('llm')
  })

  it("switching the provider shows that provider's fields", async () => {
    m.getSettings.mockResolvedValue({
      ...view(),
      settings: [
        ...view().settings,
        item('openai_api_key', {
          group: 'LLM',
          kind: 'string',
          secret: true,
          value: null,
          default: null,
          is_set: false,
        }),
      ],
    })
    const user = userEvent.setup()
    renderAt('/settings')
    expect(await screen.findByTestId('row-openrouter_api_key')).toBeInTheDocument()
    expect(screen.queryByTestId('row-openai_api_key')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('llm_provider'), 'openai')
    expect(screen.getByTestId('row-openai_api_key')).toBeInTheDocument()
    expect(screen.queryByTestId('row-openrouter_api_key')).not.toBeInTheDocument()
  })

  it('banners: restart needed and invalid stored settings', async () => {
    m.getSettings.mockResolvedValue(
      view({ restart_pending: ['cors_allow_origins'], overrides_error: 'chunk_overlap: bad' }),
    )
    renderAt('/settings')
    expect(await screen.findByText('cors_allow_origins')).toBeInTheDocument()
    expect(screen.getByText(/stored settings are invalid/)).toBeInTheDocument()
  })

  it('search filters the tunables', async () => {
    const user = userEvent.setup()
    renderAt('/settings')
    await screen.findByTestId('row-default_top_k')
    await user.type(screen.getByRole('searchbox'), 'chunk')
    expect(screen.getByTestId('row-chunk_size')).toBeInTheDocument()
    expect(screen.queryByTestId('row-default_top_k')).not.toBeInTheDocument()
  })

  it('reindex panel shows progress and problems', async () => {
    m.reindexStatus.mockResolvedValue({
      state: 'running',
      needed: true,
      problems: ['embedding dimension: stored 3, configured 1024'],
      queued_at: '2026-11-15T10:00:00',
      queued_by: 'admin@example.test',
      kind: 'reindex',
      task_id: 't1',
      result: null,
      error: null,
    })
    renderAt('/settings')
    const panel = await screen.findByTestId('reindex-status')
    expect(panel).toHaveTextContent('running')
    expect(panel).toHaveTextContent('embedding dimension: stored 3')
  })
})

describe('Settings audit tab', () => {
  it('lists changes and never shows a secret value', async () => {
    m.settingsAudit.mockResolvedValue([
      {
        id: 'a1',
        at: '2026-11-15T10:00:00',
        actor: 'admin@example.test',
        action: 'set',
        key: 'default_top_k',
        old_value: 5,
        new_value: 9,
        secret: false,
        detail: null,
      },
      {
        id: 'a2',
        at: '2026-11-15T10:01:00',
        actor: 'admin@example.test',
        action: 'set_secret',
        key: 'openrouter_api_key',
        old_value: null,
        new_value: null,
        secret: true,
        detail: null,
      },
    ])
    const user = userEvent.setup()
    renderAt('/settings')
    await user.click(await screen.findByRole('tab', { name: 'Audit log' }))
    const table = await screen.findByTestId('audit-table')
    expect(table).toHaveTextContent('5 → 9')
    expect(table).toHaveTextContent('secret replaced')
    expect(table).toHaveTextContent('(secret value not recorded)')
    expect(document.body.innerHTML).not.toContain(CANARY)
  })

  it('tabs are keyboard operable', async () => {
    const user = userEvent.setup()
    renderAt('/settings')
    const tab = await screen.findByRole('tab', { name: 'Settings' })
    tab.focus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Audit log' })).toHaveAttribute('aria-selected', 'true')
  })
})

describe('Settings permissions', () => {
  it('read-only users see values but cannot edit, save or open the audit log', async () => {
    signInAs(['settings:read'])
    renderAt('/settings')
    const input = await screen.findByLabelText('default_top_k')
    expect(input).toBeDisabled()
    expect(screen.queryByRole('tab', { name: 'Audit log' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Test connection/ })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Replace openrouter_api_key' }),
    ).not.toBeInTheDocument()
  })

  it('users without either permission get an explanation, and no nav entry', async () => {
    signInAs(['documents:read'])
    renderAt('/settings')
    expect(await screen.findByText(/do not have permission/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
    expect(m.getSettings).not.toHaveBeenCalled()
  })

  it('admins get the nav entry', async () => {
    renderAt('/settings')
    expect(await screen.findByRole('link', { name: 'Settings' })).toBeInTheDocument()
  })
})
