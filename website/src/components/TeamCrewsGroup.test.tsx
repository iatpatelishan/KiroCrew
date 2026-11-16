/**
 * The desktop app's relay Crew list as the Crew switcher's Team Crews group:
 * hidden unless the desktop bridge exists, the crews route answers and it lists
 * a Crew or a refusal (sign-in and load failures stay on screen), offline
 * Crews shown greyed (not hidden), each typed refusal shown as its plain text,
 * and Open handing the shell the endpoint ID only.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, screen, waitFor, cleanup, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, ApiError, type InstanceView, type RelayCrewEntry, type SsoStatus } from '../api/client'
import TeamCrewsSwitcher from './TeamCrewsGroup'
import { setCrewPins, setStableOrder } from './InstanceTabBar'
import { RELAY_VIEWER_CREWS_KEY, relayRefusalText, relayRetryable } from '../hooks/useRelayViewerCrews'
import { timeAgo } from '../utils/timeAgo'
import { renderWithProviders, createTestStore } from '../test/helpers'

const crew = (over: Partial<RelayCrewEntry>): RelayCrewEntry => ({
  endpointId: 'ep-1',
  name: 'Build box',
  crewType: 'PERSONAL',
  presence: 'CONNECTED',
  relayConnectionStatus: 'CONNECTED',
  crewUrl: 'https://ep-1.relay.example',
  ...over,
})

const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const listResp = (instances: InstanceView[]) => ({ active: true, instances, warm_set_cap: 5, sso: okSso })
const sshCrew = (over: Partial<InstanceView> = {}): InstanceView => ({
  id: 'cd-1',
  name: 'Cloud One',
  ssh_host: 'cd-1-alias',
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
  ...over,
})

function mount(instances: InstanceView[] = [], unread: Record<string, number> = {}) {
  vi.spyOn(api, 'listInstances').mockResolvedValue(listResp(instances))
  const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread } })
  const r = renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
  return r.queryClient
}

function withBridge(open = vi.fn(async (_id: string) => ({ ok: true }))) {
  ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open }
  return open
}

/** Open the switcher's menu and return the Team Crews group. */
async function openGroup(u: ReturnType<typeof userEvent.setup>) {
  await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
  return screen.findByTestId('team-crews-group')
}

const row = (name: string) => screen.getByRole('menuitem', { name: `Open ${name}` })

beforeEach(() => {
  localStorage.clear()
  setCrewPins([])
  setStableOrder(false)
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  vi.restoreAllMocks()
  delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
})

describe('TeamCrewsGroup', () => {
  it('reads nothing and leaves the bar as today (no remote Crews: no bar) without the desktop bridge', async () => {
    const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('leaves the bar as today while the crews route answers 404', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(404, 'off', JSON.stringify({ error: 'off', code: 'RELAY_VIEWER_OFF' })),
    )
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
  })

  it('with the viewer off, a menu of Local and SSH Crews has no group names and no Team Crews', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(404, 'off', '{}'))
    mount([sshCrew({ was_connected: true })])
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
    await screen.findByRole('menuitemradio', { name: /Cloud One/ })
    expect(screen.queryByText('This computer')).toBeNull()
    expect(screen.queryByText('My Crews')).toBeNull()
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
  })

  it('leaves the bar as today when the viewer lists no Team Crews and there are no SSH Crews', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('success'))
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
  })

  it('with no Team Crews listed, a menu of Local and SSH Crews has no group names and no Team Crews', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    const client = mount([sshCrew({ was_connected: true })])
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('success'))
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
    await screen.findByRole('menuitemradio', { name: /Cloud One/ })
    expect(screen.queryByText('This computer')).toBeNull()
    expect(screen.queryByText('My Crews')).toBeNull()
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
    expect(screen.queryByText('No remote Crews to open.')).toBeNull()
  })

  it('shows the bar with no SSH Crews once the viewer lists Team Crews, under group names', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    const u = userEvent.setup()
    const group = await openGroup(u)
    expect(screen.getByText('This computer')).toBeTruthy()
    expect(within(group).getByText('Team Crews')).toBeTruthy()
    // The Local row keeps its name; "This computer" is the group's name only.
    expect(screen.getByRole('menuitemradio', { name: /Local/ })).toBeTruthy()
    // Team rows are plain items, not destinations: no radio, no pin.
    expect(within(group).queryAllByRole('menuitemradio')).toHaveLength(0)
    expect(within(group).queryAllByRole('menuitemcheckbox')).toHaveLength(0)
    expect(within(group).getByTitle('Opens in its own window')).toBeTruthy()
  })

  it('names My Crews only when SSH Crews are listed', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount([sshCrew({ was_connected: true })])
    const u = userEvent.setup()
    await openGroup(u)
    expect(screen.getByText('My Crews')).toBeTruthy()
  })

  it('shows the bar with the Team Crews group while the instances feature is off', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    vi.spyOn(api, 'listInstances').mockRejectedValue(
      new ApiError(403, 'Instances feature is disabled', JSON.stringify({ error: 'Instances feature is disabled', code: 'instances_disabled' })),
    )
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    const client = renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store }).queryClient
    await waitFor(() => expect(client.getQueryState(['instances'])?.status).toBe('error'))
    const u = userEvent.setup()
    const group = await openGroup(u)
    expect(within(group).getByRole('menuitem', { name: 'Open Build box' })).toBeTruthy()
  })

  it("leaves Team Crews out of the caret's unread count", async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })] })
    mount([sshCrew({ was_connected: true })], { 'cd-1': 2 })
    const u = userEvent.setup()
    // The Team rows must be on screen first, or the count could pass before they exist.
    const group = await openGroup(u)
    expect(within(group).getAllByRole('menuitem', { name: /^Open / })).toHaveLength(2)
    await u.keyboard('{Escape}')
    expect(await screen.findByRole('button', { name: 'Switch crew, 2 unread elsewhere' })).toBeTruthy()
  })

  it('shows offline Crews greyed with "offline" rather than hiding them', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({}),
        crew({
          endpointId: 'ep-2', name: 'Team box', crewType: 'TEAM',
          presence: 'NOT_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED',
        }),
      ],
    })
    mount()
    const group = await openGroup(userEvent.setup())
    const rows = within(group).getAllByTestId('relay-crew-row')
    expect(rows.map(r => r.dataset.online)).toEqual(['true', 'false'])
    expect(within(rows[1]).getByText(/Team · offline/)).toBeTruthy()
    expect(rows[1].className).toContain('opacity-40')
    expect(rows[0].className).not.toContain('opacity-40')
  })

  it('lets a Crew be opened only while its relay connection is CONNECTED, whatever its presence', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({ endpointId: 'ep-1', name: 'Up', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED' }),
        crew({ endpointId: 'ep-2', name: 'Down', presence: 'NOT_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-3', name: 'Tunnel down', presence: 'CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-4', name: 'Late', presence: 'HEARTBEAT_LATE', relayConnectionStatus: 'CONNECTED' }),
      ],
    })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    expect(row('Up').getAttribute('aria-disabled')).toBeNull()
    expect(row('Down').getAttribute('aria-disabled')).toBe('true')
    expect(row('Tunnel down').getAttribute('aria-disabled')).toBe('true')
    expect(row('Late').getAttribute('aria-disabled')).toBeNull()
    expect(row('Down').title).toBe('This Crew is offline.')
    await u.click(row('Tunnel down'))
    await u.click(row('Down'))
    expect(open).not.toHaveBeenCalled()
    // An offline row stays reachable, so it is said, not skipped.
    expect(screen.getByTestId('team-crews-group')).toBeTruthy()
  })

  it('words a row that cannot open from its presence, with last seen, and a connected row as online', async () => {
    withBridge()
    // Only Date is faked, so react-query's timers stay real.
    vi.useFakeTimers({ toFake: ['Date'] })
    const pinned = new Date('2026-01-01T12:00:00Z')
    vi.setSystemTime(pinned)
    const seen = Math.floor(pinned.getTime() / 1000) - 300
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({ endpointId: 'ep-1', name: 'Up', lastSeenAt: seen }),
        crew({
          endpointId: 'ep-2', name: 'Late', presence: 'HEARTBEAT_LATE',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: seen,
        }),
        crew({ endpointId: 'ep-3', name: 'Tunnel down', relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: null }),
        crew({
          endpointId: 'ep-4', name: 'Down', presence: 'NOT_CONNECTED',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: seen,
        }),
        crew({ endpointId: 'ep-5', name: 'New', presence: 'NEVER_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-6', name: 'Odd', presence: 'UNKNOWN', relayConnectionStatus: 'UNKNOWN' }),
      ],
    })
    mount()
    const group = await openGroup(userEvent.setup())
    const status = within(group).getAllByTestId('relay-crew-row')
      .map(r => r.querySelector('.text-\\[12px\\]')?.textContent)
    const ago = `last seen ${timeAgo(seen)}`
    expect(status).toEqual([
      'Personal · online',
      `Personal · reconnecting · ${ago}`,
      'Personal · reconnecting',
      `Personal · offline · ${ago}`,
      'Personal · offline',
      'Personal · offline',
    ])
  })

  it('Open sends only the endpoint ID to the desktop bridge, then closes the menu', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' })] })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Lab'))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    expect(open.mock.calls[0]).toEqual(['ep-9'])
    await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
  })

  it.each([
    ['CREW_ACCESS_NOT_GRANTED', 'You no longer have access to this Crew.'],
    ['ACCESS_DENIED', "Couldn't open this Crew."],
    ['REMOTE_ACCESS_DISABLED', 'Remote access is turned off for this account.'],
    ['CREW_OFFLINE', 'This Crew is offline.'],
    ['THROTTLED', 'Too many requests. Try again in a minute.'],
    ['SIGNED_OUT', 'Sign in to Kiro to see your Team Crews.'],
    ['NOT_SET_UP', "Team Crews over the relay aren't set up."],
    ['INVALID_REQUEST', "Couldn't open this Crew."],
    ['SERVICE_ERROR', "Couldn't open this Crew."],
    ['UNREACHABLE', "Couldn't open this Crew."],
    ['BAD_ANSWER', "Couldn't open this Crew."],
    ['INVALID_ENDPOINT', "Couldn't open this Crew."],
    ['UNAVAILABLE', "Couldn't open this Crew."],
  ])('keeps the menu open and shows the plain text for an Open refused with %s', async (code, text) => {
    withBridge(vi.fn(async () => ({ ok: false, error: code })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Build box'))
    expect((await screen.findByTestId('relay-crew-refusal')).textContent).toContain(text)
    expect(screen.getByTestId('team-crews-group')).toBeTruthy()
  })

  it('offers Ask the agent as a menu item tied to a row refusal, one per refused row', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'SERVICE_ERROR' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })],
    })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Build box'))
    await u.click(row('Two'))
    const items = await screen.findAllByRole('menuitem', { name: 'Ask the agent' })
    expect(items.map(i => i.getAttribute('aria-describedby'))).toEqual(['relay-crew-refusal-ep-1', 'relay-crew-refusal-ep-2'])
    for (const id of ['relay-crew-refusal-ep-1', 'relay-crew-refusal-ep-2']) {
      expect(document.getElementById(id)?.textContent).toContain("Couldn't open this Crew.")
    }
  })

  it('offers Ask the agent as a menu item tied to the list refusal', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    await openGroup(userEvent.setup())
    const item = await screen.findByRole('menuitem', { name: 'Ask the agent' })
    expect(item.getAttribute('aria-describedby')).toBe('relay-crews-list-refusal')
    expect(document.getElementById('relay-crews-list-refusal')?.textContent).toContain("Couldn't load your Team Crews.")
  })

  it('arrow keys skip the refusal notice under a row and reach its Ask the agent item', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'SERVICE_ERROR' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })],
    })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Build box'))
    const handoff = await screen.findByRole('menuitem', { name: 'Ask the agent' })
    expect(document.activeElement).toBe(row('Build box'))
    await u.keyboard('{ArrowDown}')
    expect(document.activeElement).toBe(handoff)
    await u.keyboard('{ArrowDown}')
    expect(document.activeElement).toBe(row('Two'))
    await u.keyboard('{ArrowUp}{ArrowUp}')
    expect(document.activeElement).toBe(row('Build box'))
  })

  it('shows a list-level refusal in place of the rows', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(401, 'signed out', JSON.stringify({ error: 'signed out', code: 'SIGNED_OUT' })),
    )
    mount()
    await openGroup(userEvent.setup())
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain('Sign in to Kiro to see your Team Crews.')
    expect(screen.queryByTestId('relay-crew-row')).toBeNull()
  })

  it('shows a load failure, not an Open failure, when the list read fails with no known code', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    await openGroup(userEvent.setup())
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain("Couldn't load your Team Crews.")
  })

  const refused = (status: number, body: Record<string, unknown>) =>
    new ApiError(status, 'refused', JSON.stringify({ error: 'refused', ...body }))

  it('shows the request ID and a Try again that reads the list again', async () => {
    withBridge()
    const spy = vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-0000-example' }))
      .mockResolvedValueOnce({ crews: [crew({})] })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    const details = await screen.findByTestId('relay-crews-list-details')
    expect(details.textContent).toContain('Request ID: req-0000-example')
    await u.click(screen.getByRole('menuitem', { name: 'Try again' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('relay-crew-row')).toBeTruthy()
  })

  it('shows no request ID when the refusal has none', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(502, { code: 'SERVICE_ERROR' }))
    mount()
    await openGroup(userEvent.setup())
    await screen.findByTestId('relay-crews-list-refusal')
    expect(screen.queryByTestId('relay-crews-list-details')).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Try again' })).toBeTruthy()
  })

  it.each(['CREW_ACCESS_NOT_GRANTED', 'REMOTE_ACCESS_DISABLED', 'ACCESS_DENIED', 'SIGNED_OUT', 'NOT_SET_UP', 'INVALID_REQUEST'])(
    'offers no Try again for %s, where the same request fails again',
    async code => {
      withBridge()
      vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(403, { code, requestId: 'req-0000-example' }))
      mount()
      await openGroup(userEvent.setup())
      await screen.findByTestId('relay-crews-list-details')
      expect(screen.queryByTestId('relay-crews-list-retry')).toBeNull()
    },
  )

  it('keeps Try again off until a throttle\'s Retry-After passes', async () => {
    // A fake clock: the 30 s wait passes only when the test moves it, so a slow
    // runner cannot reach the deadline first. shouldAdvanceTime keeps findBy and
    // react-query moving between the steps.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(429, { code: 'THROTTLED', retryAfter: 30 }))
    mount()
    await openGroup(userEvent.setup({ advanceTimers: vi.advanceTimersByTime }))
    const retry = await screen.findByTestId('relay-crews-list-retry')
    expect(retry.getAttribute('aria-disabled')).toBe('true')
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(retry.getAttribute('aria-disabled')).toBeNull()
  })

  it('keeps Try again off while the list is being read again', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR' }))
      .mockReturnValueOnce(new Promise<never>(() => {}))
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    const retry = await screen.findByTestId('relay-crews-list-retry')
    expect(retry.getAttribute('aria-disabled')).toBeNull()
    await u.click(retry)
    await waitFor(() => expect(screen.getByTestId('relay-crews-list-retry').getAttribute('aria-disabled')).toBe('true'))
    // The refusal Try again was clicked from stays up during its own read.
    expect(screen.getByTestId('relay-crews-list-refusal').textContent).toContain("Couldn't load your Team Crews.")
  })

  it('does not bring back an older request ID on a later read once a retry failed again', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-old' }))
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-new' }))
      .mockReturnValueOnce(new Promise<never>(() => {}))
    const client = mount()
    const u = userEvent.setup()
    await openGroup(u)
    expect((await screen.findByTestId('relay-crews-list-details')).textContent).toContain('req-old')
    await u.click(screen.getByTestId('relay-crews-list-retry'))
    await waitFor(() => expect(screen.getByTestId('relay-crews-list-details').textContent).toContain('req-new'))
    // A later read with no list loaded (the 60 s re-read, here started by hand).
    act(() => { void client.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY }) })
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.fetchStatus).toBe('fetching'))
    expect(screen.queryByText(/req-old/)).toBeNull()
  })

  it('marks only the resend-safe codes retryable', () => {
    expect(['SERVICE_ERROR', 'THROTTLED', 'UNREACHABLE', 'BAD_ANSWER', 'CREW_OFFLINE', 'UNAVAILABLE', 'service_error']
      .every(relayRetryable)).toBe(true)
    expect(['CREW_ACCESS_NOT_GRANTED', 'SIGN_IN_CHANGED', 'INVALID_ENDPOINT', 'something_new', '']
      .some(c => relayRetryable(c))).toBe(false)
    expect(relayRetryable(undefined)).toBe(false)
  })

  it('maps an unknown code to the generic text', () => {
    expect(relayRefusalText('something_new')).toBe("Couldn't open this Crew.")
    expect(relayRefusalText(undefined)).toBe("Couldn't open this Crew.")
    expect(relayRefusalText('crew_offline')).toBe('This Crew is offline.')
  })
})

describe('TeamCrewsGroup: a Team Crew the person has not seen yet', () => {
  const NEW_ID = 'n'.repeat(24)
  const fresh = () => crew({ endpointId: NEW_ID, name: 'Docs box', crewType: 'TEAM', unseen: true })

  function mountAt(route: string) {
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    return renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store, route })
  }

  it('dots the caret and the row, tells the gateway once the menu shows it, and drops the dot on close', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt('/')
    const caret = await screen.findByRole('button', { name: 'Switch crew, new Team Crew' })
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
    const u = userEvent.setup()
    await u.click(caret)
    const group = await screen.findByTestId('team-crews-group')
    expect(within(group).getByTestId('relay-crew-new').textContent).toBe('New')
    await waitFor(() => expect(seen).toHaveBeenCalledWith([NEW_ID]))
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it("opens the Crew a note's Open action names, then clears its dot", async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect(open).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(seen).toHaveBeenCalledWith([NEW_ID]))
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
  })

  it('keeps the Open through a failed list read and opens the Crew once a read succeeds', async () => {
    const open = withBridge()
    const read = vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(new ApiError(502, 'down', JSON.stringify({ error: 'down', code: 'UNREACHABLE' })))
      .mockResolvedValue({ crews: [fresh()] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    const { queryClient } = mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
    expect(open).not.toHaveBeenCalled()
    await act(async () => { await queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY }) })
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect(read).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
  })

  it("re-reads a stale list before calling a note's Crew gone, and opens it when the fresh read has it", async () => {
    const open = withBridge()
    const read = vi.spyOn(api, 'relayViewerCrews')
      .mockResolvedValueOnce({ crews: [] })
      .mockResolvedValue({ crews: [fresh()] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect(read).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
  })

  it.each([
    ['fresh', 'listed'],
    ['fresh', 'missing'],
    ['fresh', 'failed'],
    ['fallback', 'listed'],
    ['fallback', 'missing'],
    ['fallback', 'failed'],
  ])('ignores an obsolete note Open when its %s read resolves %s after navigation', async (source, outcome) => {
    const open = withBridge()
    const down = new ApiError(502, 'down', JSON.stringify({ error: 'down', code: 'UNREACHABLE' }))
    let answer!: (v: { crews: RelayCrewEntry[] }) => void
    let reject!: (reason: unknown) => void
    const pending = new Promise<{ crews: RelayCrewEntry[] }>((resolve, fail) => {
      answer = resolve
      reject = fail
    })
    const read = vi.spyOn(api, 'relayViewerCrews').mockResolvedValueOnce({ crews: [] })
    if (source === 'fallback') read.mockRejectedValueOnce(down)
    read.mockReturnValueOnce(pending)
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    function Navigation() {
      const location = useLocation()
      const navigate = useNavigate()
      return <>
        <button onClick={() => navigate('/settings?tab=general')}>Settings</button>
        <output data-testid="location">{location.key}:{location.pathname}{location.search}</output>
      </>
    }
    // The desktop switcher remains mounted across route changes.
    const { queryClient } = renderWithProviders(<>
      <Navigation />
      <TeamCrewsSwitcher variant="inline" />
    </>, { route: `/chat?relay_crew=${NEW_ID}` })
    await waitFor(() => expect(read).toHaveBeenCalledTimes(source === 'fallback' ? 3 : 2))
    const refetch = vi.spyOn(queryClient, 'refetchQueries')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Settings' }))
    const location = screen.getByTestId('location').textContent
    expect(location).toContain('/settings?tab=general')
    await act(async () => {
      if (outcome === 'failed') reject(down)
      else answer({ crews: outcome === 'listed' ? [fresh()] : [] })
      await pending.catch(() => {})
    })
    expect(screen.getByTestId('location').textContent).toBe(location)
    expect(open).not.toHaveBeenCalled()
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
    expect(refetch).not.toHaveBeenCalled()
  })

  it('does not let a list read that started before a seen save bring the dot back', async () => {
    withBridge()
    let release: (v: { crews: RelayCrewEntry[] }) => void = () => {}
    const read = vi.spyOn(api, 'relayViewerCrews').mockResolvedValueOnce({ crews: [fresh()] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    const { queryClient } = mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Team Crew' }))
    await screen.findByTestId('team-crews-group')
    // A poll goes out while the menu is open, answering with the pre-save list.
    read.mockReturnValueOnce(new Promise(r => { release = r }))
    void queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY })
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
    await act(async () => { release({ crews: [fresh()] }) })
    // The late answer has been taken in or dropped once the read is no longer running.
    await waitFor(() => expect(queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)?.fetchStatus).toBe('idle'))
    expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull()
  })

  it("decides a note's Open only from its fresh read, not from a cache change while that read is out", async () => {
    const open = withBridge()
    let answer: (v: { crews: RelayCrewEntry[] }) => void = () => {}
    const other = crew({ endpointId: 'o'.repeat(24), name: 'Other box', crewType: 'TEAM', unseen: true })
    const read = vi.spyOn(api, 'relayViewerCrews')
      .mockResolvedValueOnce({ crews: [other] })
      .mockReturnValueOnce(new Promise(r => { answer = r }))
      .mockResolvedValueOnce({ crews: [{ ...other, unseen: false }] })
      .mockResolvedValue({ crews: [fresh(), other] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    const { queryClient } = mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    // The cache changes while the fresh read is out (a poll, or a seen save for
    // another Crew); the old list must not decide the Open.
    await act(async () => { await queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY }) })
    expect(read).toHaveBeenCalledTimes(3)
    // That changed list has rendered (its one Crew is seen now, so the dot is gone).
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
    expect(open).not.toHaveBeenCalled()
    await act(async () => { answer({ crews: [fresh()] }) })
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
  })

  it("settles a note's Open from the list re-read when its fresh read fails, even if the list is unchanged", async () => {
    const open = withBridge()
    const other = crew({ endpointId: 'o'.repeat(24), name: 'Other box', crewType: 'TEAM' })
    vi.spyOn(api, 'relayViewerCrews')
      .mockResolvedValueOnce({ crews: [other] })
      .mockRejectedValueOnce(new ApiError(502, 'down', JSON.stringify({ error: 'down', code: 'UNREACHABLE' })))
      // The same list again: react-query keeps the same array, so only the re-read itself can settle the Open.
      .mockResolvedValue({ crews: [other] })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent)
      .toContain("The Team Crew this notification named isn't in your list any more.")
    expect(open).not.toHaveBeenCalled()
  })

  it("says the list couldn't load when a note's fresh read and the list re-read both fail", async () => {
    const open = withBridge()
    const other = crew({ endpointId: 'o'.repeat(24), name: 'Other box', crewType: 'TEAM' })
    const down = () => new ApiError(502, 'down', JSON.stringify({ error: 'down', code: 'UNREACHABLE' }))
    vi.spyOn(api, 'relayViewerCrews')
      .mockResolvedValueOnce({ crews: [other] })
      .mockRejectedValueOnce(down())
      .mockRejectedValue(down())
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent)
      .toContain("Couldn't load your Team Crews.")
    expect(open).not.toHaveBeenCalled()
  })

  it('opens nothing for a Crew that is not on the list, and says it is gone', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    mountAt(`/chat?relay_crew=${'z'.repeat(24)}`)
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent)
      .toContain("The Team Crew this notification named isn't in your list any more.")
    expect(open).not.toHaveBeenCalled()
  })

  it('keeps the dot when the gateway could not save that the Crew was seen', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockRejectedValue(new ApiError(503, 'not saved', '{}'))
    mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Team Crew' }))
    await screen.findByTestId('team-crews-group')
    await waitFor(() => expect(seen).toHaveBeenCalled())
    expect((await screen.findByTestId('relay-crews-seen-failed')).textContent)
      .toContain("Couldn't save that you've seen this.")
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
  })

  it('clears the dot when the seen reply lands after the menu closed', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    let reply: (v: { ok: boolean }) => void = () => {}
    vi.spyOn(api, 'relayViewerCrewsSeen').mockReturnValue(new Promise(r => { reply = r }))
    mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Team Crew' }))
    await screen.findByTestId('team-crews-group')
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
    await act(async () => { reply({ ok: true }) })
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
  })

  it('sends a long unseen list in batches the route accepts', async () => {
    withBridge()
    const many = Array.from({ length: 450 }, (_, i) =>
      crew({ endpointId: `m${String(i).padStart(23, '0')}`, name: `Crew ${i}`, crewType: 'TEAM', unseen: true }))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: many })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Team Crew' }))
    await screen.findByTestId('team-crews-group')
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(3))
    expect(seen.mock.calls.map(c => c[0].length)).toEqual([200, 200, 50])
  })

  it('says why beside the switcher when that open fails, and keeps the dot', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'CREW_OFFLINE' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent).toContain("Couldn't open Docs box. This Crew is offline.")
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
    expect(seen).not.toHaveBeenCalled()
  })
})
