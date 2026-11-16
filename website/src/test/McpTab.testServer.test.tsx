import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer } from '../types'

/* The per-row Test button: one server is probed, its row is replaced, and a
 * failed test says so on that row only. */

const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpProbeOne: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpResetProbeFailures: vi.fn(),
  kirocrewConfig: vi.fn(),
  acpBackends: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const ROW_WAIT = { timeout: 5000 }

const alpha: McpServer = {
  name: 'alpha',
  command: 'alpha-cmd',
  status: 'ok',
  source: 'user',
  enabled: true,
  tools: ['search'],
}

let qc: QueryClient

function renderTab() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.mcpServers.mockResolvedValue([alpha])
  mockApi.acpBackends.mockResolvedValue({ backends: [] })
  mockApi.kirocrewConfig.mockResolvedValue({})
  mockApi.mcpDiscover.mockResolvedValue({ servers: [] })
})

describe('McpTab per-server Test button', () => {
  it('probes only that server and shows its fresh row', async () => {
    mockApi.mcpProbeOne.mockResolvedValue({ ...alpha, status: 'ok', probedAt: 1700000000 })
    renderTab()
    const row = (await screen.findByText('alpha', { selector: 'code' }, ROW_WAIT)).closest('tr')!
    fireEvent.click(within(row).getByRole('button', { name: 'Test' }))

    await waitFor(() => expect(mockApi.mcpProbeOne).toHaveBeenCalledWith('alpha'), ROW_WAIT)
    expect(mockApi.mcpProbe).not.toHaveBeenCalled()
    expect(await within(row).findByRole('button', { name: 'Test' }, ROW_WAIT)).toBeTruthy()
  })

  it('says so on the row when the test request fails', async () => {
    mockApi.mcpProbeOne.mockRejectedValue(new Error('network down'))
    renderTab()
    const row = (await screen.findByText('alpha', { selector: 'code' }, ROW_WAIT)).closest('tr')!
    fireEvent.click(within(row).getByRole('button', { name: 'Test' }))

    expect(await within(row).findByRole('alert', {}, ROW_WAIT)).toHaveTextContent('Test failed. Try again.')
  })
})
