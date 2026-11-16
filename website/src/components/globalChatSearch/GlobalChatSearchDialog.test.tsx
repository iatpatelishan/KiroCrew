import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { renderWithProviders } from '../../test/helpers'

const mocks = vi.hoisted(() => ({
  sessionsSearchMessages: vi.fn(),
  resumeChatSlot: vi.fn(),
}))

vi.mock('../../api/client', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../api/client')>()
  return {
    ...real,
    api: new Proxy(real.api, {
      get(target, prop, receiver) {
        if (prop === 'sessionsSearchMessages') return mocks.sessionsSearchMessages
        if (prop === 'resumeChatSlot') return mocks.resumeChatSlot
        return Reflect.get(target, prop, receiver)
      },
    }),
  }
})

import GlobalChatSearchDialog, { sinceFor } from './GlobalChatSearchDialog'
import { closeGlobalChatSearch, openGlobalChatSearch } from './globalChatSearchStore'
import { matchRanges } from './highlight'
import { clearMessageJump, usePendingMessageJump } from '../../lib/pendingMessageJump'

const RESPONSE = {
  results: [
    {
      key: 'chat-1-abc',
      title: 'Retry budget',
      agent: 'research',
      modified: 1_700_000_000,
      hit_count: 3,
      hits: [
        { index: 4, role: 'user', ts: '2026-11-01T10:00:00Z', mid: 'm-4', snippet: 'where is the retry budget' },
        { index: 5, role: 'assistant', ts: '2026-11-01T10:00:05Z', mid: 'm-5', snippet: 'the retry budget is 3' },
      ],
    },
  ],
}

function JumpProbe() {
  const jump = usePendingMessageJump()
  return <output data-testid="jump">{jump ? `${jump.slotKey}|${jump.ts}|${jump.mid}` : ''}</output>
}

beforeEach(() => {
  mocks.sessionsSearchMessages.mockResolvedValue(RESPONSE)
  mocks.resumeChatSlot.mockResolvedValue({ ok: true, key: 'chat-1-abc', messages: [], next_before: 0, total: 0 })
})

afterEach(() => {
  act(() => {
    closeGlobalChatSearch()
    clearMessageJump(null)
  })
  vi.clearAllMocks()
})

async function openAndType(text: string) {
  renderWithProviders(<><GlobalChatSearchDialog /><JumpProbe /></>, { route: '/settings' })
  act(() => openGlobalChatSearch())
  const input = await screen.findByRole('textbox', { name: 'Search all chats' })
  fireEvent.change(input, { target: { value: text } })
  return input
}

describe('GlobalChatSearchDialog', () => {
  it('lists every matching message grouped by chat, with the leftover count', async () => {
    await openAndType('retry budget')

    expect(await screen.findByText('Retry budget')).toBeInTheDocument()
    const options = await screen.findAllByRole('option')
    expect(options).toHaveLength(2)
    expect(screen.getByText('1 more match in this chat')).toBeInTheDocument()
    expect(mocks.sessionsSearchMessages).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: 'retry budget' }),
      expect.anything(),
    )
  })

  it('does not search below the minimum length', async () => {
    await openAndType('r')

    expect(await screen.findByText('Type at least 2 characters to search')).toBeInTheDocument()
    expect(mocks.sessionsSearchMessages).not.toHaveBeenCalled()
  })

  it('opens the chosen chat and asks the chat page to jump to that message', async () => {
    const input = await openAndType('retry budget')
    await screen.findAllByRole('option')

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await mocks.resumeChatSlot.mock.results[0]?.value
    })

    await waitFor(() => expect(screen.getByTestId('jump').textContent).toBe('chat-1-abc|2026-11-01T10:00:05Z|m-5'))
    expect(mocks.resumeChatSlot).toHaveBeenCalledWith('chat-1-abc', 'Retry budget')
    expect(screen.queryByRole('textbox', { name: 'Search all chats' })).not.toBeInTheDocument()
  })

  it('says so when nothing matches', async () => {
    mocks.sessionsSearchMessages.mockResolvedValue({ results: [] })
    await openAndType('nothing here')

    expect(await screen.findByText('No messages match')).toBeInTheDocument()
  })
})

describe('sinceFor', () => {
  const now = Date.UTC(2026, 10, 16, 15, 0, 0)

  it('maps each date preset to a lower bound in seconds', () => {
    expect(sinceFor('any', now)).toBeUndefined()
    expect(sinceFor('week', now)).toBe(now / 1000 - 7 * 86_400)
    expect(sinceFor('month', now)).toBe(now / 1000 - 30 * 86_400)
    const today = sinceFor('today', now)!
    expect(today).toBeLessThanOrEqual(now / 1000)
    expect(now / 1000 - today).toBeLessThan(86_400)
  })
})

describe('matchRanges', () => {
  it('marks each query word, merges overlaps and skips excluded and one-letter words', () => {
    expect(matchRanges('Retry the retry budget', 'retry -the a')).toEqual([[0, 5], [10, 15]])
    expect(matchRanges('abcabc', 'abc bca')).toEqual([[0, 6]])
  })
})
