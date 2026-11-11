import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React, { useEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useComposerState } from '../app/useComposerState.js'
import { useInputHandlers } from '../app/useInputHandlers.js'
import { TextInput } from '../components/textInput.js'
import { imageToken } from '../domain/attachments.js'
import * as history from '../lib/history.js'
import { parseVoiceRecordKey } from '../lib/platform.js'
import { pasteTokenLabel } from '../lib/text.js'

class FakeInput extends EventEmitter {
  chunks: string[] = []
  isRaw = false
  isTTY = true
  readableLength = 0
  ref = vi.fn()
  setEncoding = vi.fn()
  unref = vi.fn()

  read() {
    const next = this.chunks.shift() ?? null
    this.readableLength = this.chunks.length

    return next
  }

  send(chunk: string) {
    this.chunks.push(chunk)
    this.readableLength = this.chunks.length
    this.emit('readable')
  }

  setRawMode(enabled: boolean) {
    this.isRaw = enabled
  }
}

async function mount(busy = false) {
  history.load().splice(0)
  history.append('older message')
  history.append('latest message')
  resetUiState()
  resetOverlayState()
  patchUiState({ busy, sid: randomUUID() })
  const stdin = new FakeInput()
  const stdout = Object.assign(new PassThrough(), { columns: 80, isTTY: false, rows: 24 })
  const request = vi.fn().mockResolvedValue({})
  const die = vi.fn()
  let composer!: ReturnType<typeof useComposerState>
  let ready = false

  function Harness() {
    const gw = { isCanonical: false, request }
    composer = useComposerState({ gw, submitRef: { current: vi.fn() }, sys: vi.fn() } as any)
    useInputHandlers({
      actions: {
        appendMessage: vi.fn(),
        cancelClarify: vi.fn(),
        die,
        dispatchSubmission: vi.fn(),
        newSession: vi.fn(),
        sys: vi.fn()
      },
      composer,
      gateway: { gw, rpc: request },
      terminal: {
        hasSelection: false,
        scrollRef: { current: null },
        scrollWithSelection: vi.fn(),
        selection: { clearSelection: vi.fn(), copySelection: vi.fn() },
        stdout
      },
      voice: {
        enabled: false,
        recording: false,
        recordKey: parseVoiceRecordKey('ctrl+b'),
        setProcessing: vi.fn(),
        setRecording: vi.fn(),
        setVoiceEnabled: vi.fn(),
        setVoiceTts: vi.fn()
      },
      wheelStep: 3
    } as any)
    useEffect(() => {
      ready = true
    }, [])

    return (
      <TextInput columns={80} onChange={composer.actions.setInput} onSubmit={vi.fn()} value={composer.state.input} />
    )
  }

  const instance = renderSync(<Harness />, {
    exitOnCtrlC: false,
    patchConsole: false,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream,
    stderr: new PassThrough() as NodeJS.WriteStream
  })

  await vi.waitFor(() => expect(ready).toBe(true))

  return {
    composer: () => composer,
    die,
    request,
    async input(value: string) {
      composer.actions.setInput(value)
      await vi.waitFor(() => expect(composer.state.input).toBe(value))
    },
    async key(key: string, expected: string) {
      stdin.send(key)
      await vi.waitFor(() => expect(composer.state.input).toBe(expected))
    },
    close() {
      instance.unmount()
      instance.cleanup()
      resetOverlayState()
      resetUiState()
    }
  }
}

describe('cancelled composer history', () => {
  it.each([false, true])('Ctrl+C recalls the draft before older messages, busy=%s', async busy => {
    const ui = await mount(busy)

    try {
      await ui.input('unfinished follow-up')
      await ui.key('\x03', '')
      await ui.key('\x1b[A', 'unfinished follow-up')
      await ui.key('\x03', '')
      await ui.key('\x1b[A', 'unfinished follow-up')
      await ui.key('\x1b[A', 'latest message')
      await ui.key('\x1b[A', 'older message')
      await ui.key('\x1b[B', 'latest message')
      expect(history.load()).toEqual(['older message', 'latest message', 'unfinished follow-up'])
      expect(ui.die).not.toHaveBeenCalled()
      expect(ui.request).not.toHaveBeenCalled()
    } finally {
      ui.close()
    }
  })

  it('keeps the current draft when navigating back down after a recall', async () => {
    const ui = await mount()

    try {
      await ui.input('cancelled draft')
      await ui.key('\x03', '')
      await ui.input('new draft')
      await ui.key('\x1b[A', 'cancelled draft')
      await ui.key('\x1b[B', 'new draft')
    } finally {
      ui.close()
    }
  })

  it('captures current multiline and paste content before a React rerender', async () => {
    const ui = await mount()

    try {
      const text = '첫째 줄\n둘째 줄'
      const label = pasteTokenLabel(text, 2)
      await ui.input('initial draft')
      ui.composer().actions.setInputBuf(['prefix'])
      ui.composer().actions.setComposerTokens([{ kind: 'paste', label, text }])
      ui.composer().actions.setInput(`review ${label}`)
      ui.composer().actions.discardIn()
      await vi.waitFor(() => expect(ui.composer().state.input).toBe(''))
      await ui.key('\x1b[A', `prefix\nreview ${text}`)
      expect(ui.composer().refs.tokensRef.current).toEqual([])
    } finally {
      ui.close()
    }
  })

  it('keeps ordinary clears and image-only cancellations out of text history', async () => {
    const ui = await mount()

    try {
      await ui.input('already handled elsewhere')
      ui.composer().actions.clearIn()
      await vi.waitFor(() => expect(ui.composer().state.input).toBe(''))
      const label = imageToken(1)
      ui.composer().actions.setComposerTokens([{ kind: 'image', index: 1, label, path: '/tmp/example.png' }])
      await ui.input(label)
      await ui.key('\x03', '')
      expect(history.load()).toEqual(['older message', 'latest message'])
      expect(ui.composer().refs.tokensRef.current).toEqual([])
    } finally {
      ui.close()
    }
  })

  it('preserves queue-edit priority over history navigation', async () => {
    const ui = await mount(true)

    try {
      ui.composer().actions.enqueue('queued message')
      await vi.waitFor(() => expect(ui.composer().state.queuedDisplay).toEqual(['queued message']))
      await ui.input('unfinished draft')
      await ui.key('\x03', '')
      await ui.key('\x1b[A', 'queued message')
      expect(history.load()).toEqual(['older message', 'latest message', 'unfinished draft'])
      expect(ui.composer().state.queuedDisplay).toEqual(['queued message'])
    } finally {
      ui.close()
    }
  })

  it('does not append blank drafts or change the empty-prompt exit gesture', async () => {
    const ui = await mount()

    try {
      await ui.input('  ')
      await ui.key('\x03', '')
      expect(history.load()).toEqual(['older message', 'latest message'])
      await ui.key('\x03', '')
      await vi.waitFor(() => expect(ui.die).toHaveBeenCalledOnce())
    } finally {
      ui.close()
    }
  })
})
