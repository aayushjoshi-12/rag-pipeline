import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useLoadAct } from './hooks'

describe('useLoadAct', () => {
  it('loads, shows a load error, and act clears the error then reloads', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('down')).mockResolvedValue('ok')
    const apply = vi.fn()
    const { result } = renderHook(() => useLoadAct(load, apply))
    await waitFor(() => expect(result.current.error).toBe('down'))
    await act(() => result.current.act(() => Promise.resolve()))
    await waitFor(() => expect(apply).toHaveBeenCalledWith('ok'))
    expect(result.current.error).toBeNull()
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('act surfaces a failed mutation without reloading, and key changes reload', async () => {
    const load = vi.fn().mockResolvedValue(1)
    const { result, rerender } = renderHook(({ k }) => useLoadAct(load, vi.fn(), k), {
      initialProps: { k: 'a' },
    })
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1))
    await act(() => result.current.act(() => Promise.reject(new Error('nope'))))
    expect(result.current.error).toBe('nope')
    expect(load).toHaveBeenCalledTimes(1)
    rerender({ k: 'b' })
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
  })
})
